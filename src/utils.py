"""Shared helpers with no platform of their own: the localization runtime,
role checks, the rate limiter and the plumbing followed sources need.

The localization runtime is the bulk of it. The six i18n/<lang>.json files are
read once at import into three shapes — `_LOCALE` (nested, for the legacy
localized_* accessors), `_LOCALE_FLAT` (per language, for the localization
commands) and `_LOCALE_STATUS` (translation status per key) — so a lookup at
relay time is a dict access. A consequence worth knowing: edits to the JSON
files take effect on restart, not immediately.

Not this module's zone: anything that sends (the two bot packages), the
database (db/), or message rendering (message_relay.py).
"""
import re
import time
from config import ADMINS, SERVICE_CHATS
import db
import itertools

def is_admin(platform, user_id):
    """Whether the user is a Bot Admin — the top role, hard-coded in
    config.ADMINS rather than stored, so it survives any database mishap."""
    return user_id in ADMINS.get(platform, set())

_rate_buckets = {}

def rate_limit_ok(key, limit, window_seconds):
    """Sliding-window rate limiter. Returns True if the action is allowed,
    False if `limit` actions already happened within `window_seconds`."""
    now = time.monotonic()
    if len(_rate_buckets) > 10000:
        stale = [k for k, v in _rate_buckets.items() if not v or v[-1] < now - 3600]
        for k in stale:
            _rate_buckets.pop(k, None)
    bucket = _rate_buckets.setdefault(key, [])
    cutoff = now - window_seconds
    while bucket and bucket[0] <= cutoff:
        bucket.pop(0)
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True

def extract_username_from_bot_message(text: str):
    """Recover the sender name from a relay copy's ``[Messenger | Place]
    Sender:`` header line. Returns None when the text is not a relay copy."""
    if not text:
        return None

    try:
        for raw_line in str(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue

            m = re.match(r"^\[[^\]]+\]\s*(.+?)\s*:\s*$", line)
            if m:
                name = m.group(1).strip()
                return name or None

        return text.split("]", 1)[1].split(":", 1)[0].strip()
    except Exception:
        return None

def is_chat_admin(platform, chat_id, user_id):
    """Whether the user administers this chat, by any of the paths that grant
    it: an explicit chat_admins row, a server-wide Local Admin or Bridge Admin
    grant for the community, or a legacy group-wide '<prefix>:0' row.

    This is the check nearly every chat-level command uses; Bot Admins are
    checked separately with is_admin, since they are not chat-scoped."""
    row = db.cur.execute(
        """
        SELECT 1 FROM chat_admins
        WHERE platform=? AND chat_id=? AND user_id=?
        """,
        (platform, chat_id, str(user_id))
    ).fetchone()
    if row:
        return True

    prefix = chat_id.split(":", 1)[0] if ":" in chat_id else chat_id

    if db.is_server_admin(platform, prefix, user_id):
        return True

    if db.is_server_bridge_admin(platform, prefix, user_id):
        return True

    if ":" in chat_id:
        group_key = f"{prefix}:0"
        row = db.cur.execute(
            """
            SELECT 1 FROM chat_admins
            WHERE platform=? AND chat_id=? AND user_id=?
            """,
            (platform, group_key, str(user_id))
        ).fetchone()
        return row is not None

    return False

def bridge_feed_permission(platform, chat_id, user_id):
    """Whether the user may configure a bridge-wide feed here, and whether
    'here' is a bridge at all. Returns ``(allowed, in_bridge)``.

    Deliberately stricter than the older feed commands, which accept any chat
    admin: a wiki subscription pours into every chat of the bridge, so the
    people who answer for the bridge as a whole are the ones who should be
    able to open it — Bot Admins, and the Bridge Admins of this chat's bridge
    (grants made for the bridge itself and grants made server-wide both count,
    see db.get_bridge_admins).

    A chat that has not joined a bridge yet has no Bridge Admins to ask, and
    refusing everyone there would make the feature unusable before the bridge
    exists. So in that case the chat's own admins may set it up, and the
    caller is expected to say — through ``in_bridge`` — that the setting will
    become the bridge's business once the chat joins one."""
    if is_admin(platform, user_id):
        return True, _chat_bridge_id(chat_id) is not None

    bridge_id = _chat_bridge_id(chat_id)
    if bridge_id is None:
        return is_chat_admin(platform, chat_id, user_id), False

    if str(user_id) in db.get_bridge_admins(bridge_id):
        return True, True

    server_id = db.chat_server_id(platform, chat_id)
    if server_id and db.is_server_bridge_admin(platform, server_id, user_id):
        return True, True
    return False, True

def _chat_bridge_id(chat_id):
    """The bridge a chat belongs to, or None when it is in none."""
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    return row["bridge_id"] if row and row["bridge_id"] is not None else None

async def log_error(text):
    """Report an error into the Discord service chats, localized per chat.
    Everything is wrapped in try/except: logging a failure must never raise a
    second one."""
    try:
        from discord_bot import bot
        for chat_key in SERVICE_CHATS.get("discord", set()):
            try:
                key = str(chat_key)
                guild_id = None
                channel_id = int(key.split(":", 1)[1]) if ":" in key else int(key)
                if ":" in key:
                    try:
                        guild_id = int(key.split(":", 1)[0])
                    except Exception:
                        guild_id = None
                channel = bot.get_channel(channel_id)
                if not channel:
                    channel = await bot.fetch_channel(channel_id)
                if guild_id is None and channel and getattr(channel, "guild", None):
                    guild_id = channel.guild.id
                lang_key = f"{guild_id}:{channel_id}" if guild_id is not None else str(channel_id)
                lang = get_chat_lang(lang_key)
                localized_text = localized_service_event("daily_loop_error", lang, error=text)
                if channel:
                    await channel.send(f"⚠️ {localized_text}")
            except Exception:
                pass
    except Exception:
        pass

_status_lang_cycle = itertools.cycle(['ru', 'uk', 'pl', 'en', 'es', 'pt'])

def _status_loc(lang_code, key):
    """Read a status-localization value (template / plural-form list) from i18n."""
    return _LOCALE_FLAT.get(lang_code, {}).get(key) or _LOCALE_FLAT.get(DEFAULT_LANG, {}).get(key)

def get_next_status_text(total_members, total_servers):
    """Status text on the next language in the cycle (localizations live in i18n)."""
    lang_code = next(_status_lang_cycle)
    template = _status_loc(lang_code, "status_template")
    members_forms = _status_loc(lang_code, "status_members_forms")
    servers_forms = _status_loc(lang_code, "status_servers_forms")

    if not template:
        return f"{total_members} members / {total_servers} communities"
    if not members_forms:
        members_forms = ["member", "members", "members"]
    if not servers_forms:
        servers_forms = ["community", "communities", "communities"]

    if lang_code in ('ru', 'uk'):
        plural_func = plural_ru
    elif lang_code == 'pl':
        plural_func = plural_pl
    else:
        plural_func = plural_en

    m_word = plural_func(total_members, members_forms)
    s_word = plural_func(total_servers, servers_forms)
    return template.format(
        members=total_members, members_word=m_word,
        servers=total_servers, servers_word=s_word,
    )

SUPPORTED_LANGS = {"ru", "uk", "pl", "en", "es", "pt"}
DEFAULT_LANG = "en"

import os as _i18n_os
import json as _i18n_json
import logging as _i18n_logging

_I18N_DIR = _i18n_os.path.join(_i18n_os.path.dirname(__file__), "i18n")

LOCALE_STATUS_EMOJI = {"verified": "\U0001F7E9", "unverified": "\U0001F7E7", "untranslated": "\U0001F7E5"}

def _load_i18n():
    """Build the runtime localization structures from the i18n/<lang>.json files.

    Returns (locale, status, flat):
      locale[key][lang] = text, with dotted keys 'group.sub' rebuilt into
        locale[group][sub][lang] so the legacy localized_* helpers keep working.
      status[flat_key][lang] = 'verified' | 'unverified' | 'untranslated'
      flat[lang][flat_key] = text
    """
    locale, status, flat = {}, {}, {}
    if _i18n_os.path.isdir(_I18N_DIR):
        for _fname in sorted(_i18n_os.listdir(_I18N_DIR)):
            if not _fname.endswith(".json"):
                continue
            _lang = _fname[:-5]
            with open(_i18n_os.path.join(_I18N_DIR, _fname), encoding="utf-8") as _f:
                _entries = _i18n_json.load(_f)
            flat[_lang] = {}
            for _k, _entry in _entries.items():
                _text = _entry["text"]
                flat[_lang][_k] = _text
                status.setdefault(_k, {})[_lang] = _entry.get("status", "unverified")
                if "." in _k:
                    _g, _s = _k.split(".", 1)
                    locale.setdefault(_g, {}).setdefault(_s, {})[_lang] = _text
                else:
                    locale.setdefault(_k, {})[_lang] = _text
    return locale, status, flat

_LOCALE, _LOCALE_STATUS, _LOCALE_FLAT = _load_i18n()

LANGUAGE_NAMES = {
    "ru": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
    "uk": "\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430",
    "pl": "Polski",
    "en": "English",
    "es": "Espa\u00f1ol",
    "pt": "Portugu\u00eas",
}

LANG_ORDER = ["ru", "uk", "pl", "en", "es", "pt"]

def language_name(code):
    """The language's name in itself ('Русский'), or the bare code if
    unknown."""
    return LANGUAGE_NAMES.get(code, code)

def available_locales():
    """Languages that have an i18n file, in display order."""
    return [L for L in LANG_ORDER if L in _LOCALE_FLAT]

def reply_keys():
    """All reply codes, taken from the reference (DEFAULT_LANG) localization."""
    return sorted(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())

def get_reply(lang, key):
    """One reply string in one language, or None when untranslated."""
    return _LOCALE_FLAT.get(lang, {}).get(key)

def reply_status(lang, key):
    """'verified' | 'unverified' | 'untranslated', or None if the key is unknown."""
    known = any(key in _LOCALE_FLAT.get(L, {}) for L in _LOCALE_FLAT)
    if not known:
        return None
    if key not in _LOCALE_FLAT.get(lang, {}):
        return "untranslated"
    return _LOCALE_STATUS.get(key, {}).get(lang, "unverified")

def locale_stats(lang):
    """Counts relative to the DEFAULT_LANG key set, plus the verified percentage."""
    ref = list(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())
    total = len(ref)
    have = _LOCALE_FLAT.get(lang, {})
    verified = unverified = untranslated = 0
    for k in ref:
        if k not in have:
            untranslated += 1
            continue
        st = _LOCALE_STATUS.get(k, {}).get(lang, "unverified")
        if st == "verified":
            verified += 1
        elif st == "untranslated":
            untranslated += 1
        else:
            unverified += 1
    percent = round(verified / total * 100) if total else 0
    return {"total": total, "verified": verified, "unverified": unverified,
            "untranslated": untranslated, "percent": percent}

def locale_bar(lang, width=12):
    """A 12-square progress bar of a language's translation status: verified,
    unverified and untranslated shares in that order."""
    s = locale_stats(lang)
    total = s["total"] or 1
    v = round(s["verified"] / total * width)
    u = round(s["unverified"] / total * width)
    v = min(v, width)
    u = min(u, width - v)
    t = width - v - u
    return LOCALE_STATUS_EMOJI["verified"] * v + LOCALE_STATUS_EMOJI["unverified"] * u + LOCALE_STATUS_EMOJI["untranslated"] * t

def compare_reply(key):
    """Return {lang: (status, text|None)} across all languages, or None if unknown."""
    known = any(key in _LOCALE_FLAT.get(L, {}) for L in _LOCALE_FLAT)
    if not known:
        return None
    out = {}
    for L in LANG_ORDER:
        text = _LOCALE_FLAT.get(L, {}).get(key)
        if text is None:
            out[L] = ("untranslated", None)
        else:
            out[L] = (_LOCALE_STATUS.get(key, {}).get(L, "unverified"), text)
    return out

def localized(_key, locale, **kwargs):
    """Generic flat-key accessor (used by the localization commands)."""
    table = _LOCALE.get(_key)
    if table is None:
        _i18n_logging.getLogger("bridge.i18n").warning(
            "Missing localization key %r — i18n files are older than the code?", _key
        )
        table = {}
    template = table.get(locale, table.get(DEFAULT_LANG, _key))
    if isinstance(template, (list, tuple)):
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template

FEED_REQUEST_TIMEOUT = 30

FEED_REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_FEED_MEDIA_NAME_RE = re.compile(
    r"^[\w.-]{1,64}\.(jpg|jpeg|png|gif|webp|mp4|mov|webm|m4a|mp3|ogg)$", re.IGNORECASE
)

class FeedError(Exception):
    """A followed source could not be read (network, layout change, no such
    account or channel). Carries a short reason for the service-chat report, and
    ``throttled`` when the source asked us to slow down.

    ``reason`` names a refusal that retrying cannot fix and that the admin
    should hear about in its own words — a wiki wanting an account
    (``'private'``), an address that is not a wiki at all
    (``'not_mediawiki'``). `attach_feed` turns it into the reply key of the
    same name, falling back to the generic 'unreachable' for kinds that never
    set one."""

    throttled = False
    reason = None

def feed_media_name(url, index, kind, prefix="media"):
    """A filename for the GALLERY upload, taken from the media URL when it looks
    usable and synthesized otherwise."""
    tail = (url or "").split("?", 1)[0].rsplit("/", 1)[-1]
    if _FEED_MEDIA_NAME_RE.match(tail):
        return tail
    ext = "mp4" if kind in ("video", "animated_gif", "gif") else "jpg"
    return f"{prefix}-{index + 1}.{ext}"

def feed_scope_name(chat_id, lang):
    """Where a feed attached in this chat will deliver, for the confirmation
    reply: the whole bridge when the chat has one — chats that join it later
    included — and otherwise this chat alone, until it joins a bridge."""
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if row and row["bridge_id"] is not None:
        return localized("feed_scope_bridge", lang, bridge_id=row["bridge_id"])
    return localized("feed_scope_chat", lang)

POLL_MAX_SECONDS = 30 * 86400

def parse_poll_duration(text):
    """Poll duration: h=hours, d=days, w=weeks, m=months(30d). Capped at 30 days.
    Returns seconds. Raises ValueError on invalid format."""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)(h|d|w|m)", text)
    if not m:
        raise ValueError("invalid_duration")
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"h": 3600, "d": 86400, "w": 604800, "m": 30 * 86400}
    return min(n * mult[unit], POLL_MAX_SECONDS)

def get_chat_lang(chat_id):
    """The language to answer this chat in, always a supported one: the
    stored setting when valid, otherwise the default. Every user-visible
    string in the bot goes through a lookup that starts here."""
    lang = db.get_chat_lang(chat_id)
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG

def set_chat_lang(chat_id, lang_code):
    """Store a chat's language, refusing codes the bot has no strings for
    (ValueError) — the validation the /lang commands rely on."""
    if lang_code not in SUPPORTED_LANGS:
        raise ValueError("unsupported_lang")
    db.set_chat_lang(chat_id, lang_code)

def plural_ru(n, forms):
    """Slavic plural selection (Russian and Ukrainian): forms are
    [one, few, many]."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_en(n, forms):
    """Two-form plural selection for English, Spanish and Portuguese:
    forms are [singular, plural]."""
    return forms[0] if n == 1 else forms[1]

def plural_pl(n, forms):
    """Polish plural selection: forms are [one, few, many]. Differs from the
    Russian rule at n == 1 only in that 21, 31, … take the many form."""
    n = abs(int(n))
    if n == 1:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_for(lang, n):
    """The word for "file" in the right plural form for `n` in `lang`."""
    file_forms = _LOCALE["file_forms"]
    if lang == "ru":
        return plural_ru(n, file_forms["ru"])
    if lang == "uk":
        return plural_ru(n, file_forms["uk"])
    if lang == "pl":
        return plural_pl(n, file_forms["pl"])
    if lang in ("es", "pt"):
        return plural_en(n, file_forms[lang])
    return plural_en(n, file_forms["en"])

def localized_file_count_text(n, lang, source="telegram"):
    """"N files from Telegram" / "N files from X" — the stand-in for attachments
    the bot could not bring over. `source` picks which one."""
    table = _LOCALE.get("file_count" if source == "telegram" else f"file_count_{source}") \
        or _LOCALE["file_count"]
    template = table.get(lang, table[DEFAULT_LANG])
    word = plural_for(lang, n)
    return template.format(count=n, files=word)

def localized_forward_from_chat(name, lang):
    """The '(forwarded from <channel>)' line above a relayed forward
    (i18n key `forward_from_chat`)."""
    return _LOCALE["forward_from_chat"].get(lang, _LOCALE["forward_from_chat"][DEFAULT_LANG]).format(name=name)

def localized_forward_from_user(name, lang):
    """The '(forwarded from <person>)' line above a relayed forward
    (i18n key `forward_from_user`)."""
    return _LOCALE["forward_from_user"].get(lang, _LOCALE["forward_from_user"][DEFAULT_LANG]).format(name=name)

def localized_forward_unknown(lang):
    """The forward line used when the original author is hidden — Telegram
    lets users forbid being named (i18n key `forward_unknown`)."""
    return _LOCALE["forward_unknown"].get(lang, _LOCALE["forward_unknown"][DEFAULT_LANG])

def localized_replying(name, lang):
    """The '(replying to <name>)' line (i18n key `replying`)."""
    return _LOCALE["replying"].get(lang, _LOCALE["replying"][DEFAULT_LANG]).format(name=name)

def localized_bridge_join(channel, server, lang):
    """The announcement other chats of the bridge get when a chat joins
    (i18n key `bridge_join`)."""
    template = _LOCALE["bridge_join"].get(lang, _LOCALE["bridge_join"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bridge_leave(channel, server, lang):
    """The announcement other chats of the bridge get when a chat leaves
    (i18n key `bridge_leave`)."""
    template = _LOCALE["bridge_leave"].get(lang, _LOCALE["bridge_leave"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bot_joined(lang):
    """The greeting posted in a chat the moment it is attached to a bridge
    (i18n key `bot_joined`)."""
    return _LOCALE["bot_joined"].get(lang, _LOCALE["bot_joined"][DEFAULT_LANG])

def localized_consent_title(lang):
    """Heading of the forwarding-consent prompt (i18n key `consent_title`)."""
    return _LOCALE["consent_title"].get(lang, _LOCALE["consent_title"][DEFAULT_LANG])

def localized_consent_body(lang):
    """Body of the forwarding-consent prompt — the text that must state what
    is relayed and where (i18n key `consent_body`)."""
    return _LOCALE["consent_body"].get(lang, _LOCALE["consent_body"][DEFAULT_LANG])

def localized_consent_button(lang):
    """Label of the consent button (i18n key `consent_button`)."""
    return _LOCALE["consent_button"].get(lang, _LOCALE["consent_button"][DEFAULT_LANG])

def localized_sticker(lang):
    """Stand-in text for a sticker, which cannot cross platforms
    (i18n key `sticker`)."""
    return _LOCALE["sticker"].get(lang, _LOCALE["sticker"][DEFAULT_LANG])

def localized_voice_message(lang):
    """Stand-in text for a voice message (i18n key `voice_message`)."""
    return _LOCALE["voice_message"].get(lang, _LOCALE["voice_message"][DEFAULT_LANG])

def localized_video_message(lang):
    """Stand-in text for a Telegram video note (i18n key `video_message`)."""
    return _LOCALE["video_message"].get(lang, _LOCALE["video_message"][DEFAULT_LANG])

def localized_reply_unknown(lang):
    """The line shown instead of a reply reference when the replied-to
    message has no copy in this chat (i18n key `reply_unknown`)."""
    return _LOCALE["reply_unknown"].get(lang, _LOCALE["reply_unknown"][DEFAULT_LANG])

def localized_reply_external(lang):
    """The line marking a reply to a message outside this chat entirely —
    Telegram's external_reply (i18n key `reply_external`)."""
    return _LOCALE["reply_external"].get(lang, _LOCALE["reply_external"][DEFAULT_LANG])

def _reply_link_label_name(name):
    """Sanitize a sender name for use inside a Discord markdown link label:
    strip brackets/newlines that would break the [label](url) syntax."""
    return re.sub(r"[\[\]\r\n]+", " ", str(name or "")).strip()

def localized_reply_webhook(name, url, lang):
    """First line prepended to a webhook relay copy that is a reply, e.g.
    ``(replying to [Alice's message](link))`` — the bracketed part is a Discord
    markdown link to the replied-to message in the same channel."""
    safe_name = _reply_link_label_name(name)
    if not safe_name:
        fallback = _LOCALE["reply_webhook_someone"]
        safe_name = fallback.get(lang, fallback[DEFAULT_LANG])
    template = _LOCALE["reply_webhook"].get(lang, _LOCALE["reply_webhook"][DEFAULT_LANG])
    try:
        return template.format(name=safe_name, url=url)
    except Exception:
        return template

def localized_discord_system_event(name, event_key, lang):
    """A Discord system event (boost, pin, thread creation, join) as a
    sentence about its actor. Keys live under `discord_system_event` and
    `discord_system_event_action.<event>` in the i18n files."""
    action_table = _LOCALE.get("discord_system_event_action", {}).get(event_key, {})
    action = action_table.get(lang, action_table.get(DEFAULT_LANG, event_key))
    template = _LOCALE.get("discord_system_event", {}).get(lang, _LOCALE["discord_system_event"][DEFAULT_LANG])
    return template.format(name=name, action=action)

def localized_service_event(event_key, lang, **kwargs):
    """A message for the operator's service chats — start-up, shutdown, feed
    errors, unreachable chats. Keys under `service_event.<event>`."""
    table = _LOCALE.get("service_event", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_bridge_info(event_key, lang, **kwargs):
    """A field or label of the /bridge answer. Keys under `bridge_info.*`."""
    table = _LOCALE.get("bridge_info", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_whois(event_key, lang, **kwargs):
    """A field, label or refusal of the whois answer. Keys under `whois.*`."""
    table = _LOCALE.get("whois", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_help(event_key, lang, **kwargs):
    """One line of the /help listing. Keys under `help.*` — a new command
    needs its entry added there in all six languages."""
    table = _LOCALE.get("help", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_deadtopic(event_key, lang, **kwargs):
    """The /deadtopic replies and the phantom message itself. Keys under
    `deadtopic.*`."""
    table = _LOCALE.get("deadtopic", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
