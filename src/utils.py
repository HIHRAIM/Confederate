import re
import time
from config import ADMINS, SERVICE_CHATS
import db
import itertools

def is_admin(platform, user_id):
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
    row = db.cur.execute(
        """
        SELECT 1 FROM chat_admins
        WHERE platform=? AND chat_id=? AND user_id=?
        """,
        (platform, chat_id, str(user_id))
    ).fetchone()
    if row:
        return True

    if ":" in chat_id:
        prefix = chat_id.split(":", 1)[0]
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

async def log_error(text):
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
    return LANGUAGE_NAMES.get(code, code)

def available_locales():
    """Languages that have an i18n file, in display order."""
    return [L for L in LANG_ORDER if L in _LOCALE_FLAT]

def reply_keys():
    """All reply codes, taken from the reference (DEFAULT_LANG) localization."""
    return sorted(_LOCALE_FLAT.get(DEFAULT_LANG, {}).keys())

def get_reply(lang, key):
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
    lang = db.get_chat_lang(chat_id)
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG

def set_chat_lang(chat_id, lang_code):
    if lang_code not in SUPPORTED_LANGS:
        raise ValueError("unsupported_lang")
    db.set_chat_lang(chat_id, lang_code)

def plural_ru(n, forms):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_en(n, forms):
    return forms[0] if n == 1 else forms[1]

def plural_pl(n, forms):
    n = abs(int(n))
    if n == 1:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_for(lang, n):
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

def localized_file_count_text(n, lang):
    template = _LOCALE["file_count"].get(lang, _LOCALE["file_count"][DEFAULT_LANG])
    word = plural_for(lang, n)
    return template.format(count=n, files=word)

def localized_forward_from_chat(name, lang):
    return _LOCALE["forward_from_chat"].get(lang, _LOCALE["forward_from_chat"][DEFAULT_LANG]).format(name=name)

def localized_forward_from_user(name, lang):
    return _LOCALE["forward_from_user"].get(lang, _LOCALE["forward_from_user"][DEFAULT_LANG]).format(name=name)

def localized_forward_unknown(lang):
    return _LOCALE["forward_unknown"].get(lang, _LOCALE["forward_unknown"][DEFAULT_LANG])

def localized_replying(name, lang):
    return _LOCALE["replying"].get(lang, _LOCALE["replying"][DEFAULT_LANG]).format(name=name)

def localized_bridge_join(channel, server, lang):
    template = _LOCALE["bridge_join"].get(lang, _LOCALE["bridge_join"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bridge_leave(channel, server, lang):
    template = _LOCALE["bridge_leave"].get(lang, _LOCALE["bridge_leave"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bot_joined(lang):
    return _LOCALE["bot_joined"].get(lang, _LOCALE["bot_joined"][DEFAULT_LANG])

def localized_consent_title(lang):
    return _LOCALE["consent_title"].get(lang, _LOCALE["consent_title"][DEFAULT_LANG])

def localized_consent_body(lang):
    return _LOCALE["consent_body"].get(lang, _LOCALE["consent_body"][DEFAULT_LANG])

def localized_consent_button(lang):
    return _LOCALE["consent_button"].get(lang, _LOCALE["consent_button"][DEFAULT_LANG])

def localized_sticker(lang):
    return _LOCALE["sticker"].get(lang, _LOCALE["sticker"][DEFAULT_LANG])

def localized_voice_message(lang):
    return _LOCALE["voice_message"].get(lang, _LOCALE["voice_message"][DEFAULT_LANG])

def localized_video_message(lang):
    return _LOCALE["video_message"].get(lang, _LOCALE["video_message"][DEFAULT_LANG])

def localized_reply_unknown(lang):
    return _LOCALE["reply_unknown"].get(lang, _LOCALE["reply_unknown"][DEFAULT_LANG])

def localized_reply_external(lang):
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
    action_table = _LOCALE.get("discord_system_event_action", {}).get(event_key, {})
    action = action_table.get(lang, action_table.get(DEFAULT_LANG, event_key))
    template = _LOCALE.get("discord_system_event", {}).get(lang, _LOCALE["discord_system_event"][DEFAULT_LANG])
    return template.format(name=name, action=action)

def localized_service_event(event_key, lang, **kwargs):
    table = _LOCALE.get("service_event", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_bridge_info(event_key, lang, **kwargs):
    table = _LOCALE.get("bridge_info", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_whois(event_key, lang, **kwargs):
    table = _LOCALE.get("whois", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_help(event_key, lang, **kwargs):
    table = _LOCALE.get("help", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_deadtopic(event_key, lang, **kwargs):
    table = _LOCALE.get("deadtopic", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
