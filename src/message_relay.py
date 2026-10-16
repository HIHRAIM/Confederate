"""The platform-neutral core of the relay.

Two jobs live here. First, markup translation between the platforms:
Telegram sends formatting as entity offsets into the UTF-16 encoding of the
text, Discord expects markdown, and Telegram accepts a small subset of HTML
back — plus Discord's self-localizing timestamps, which Telegram cannot
render at all and are spelled out in the target chat's language instead.

Second, `relay_message`: the fan-out every inbound handler ends in. It
records the origin message, walks the target chats, renders per-target
language and per-target form, and hands each one to a `send_to_chat`
callback the platform half supplies — this module never talks to Discord or
Telegram itself.
"""
import db
import time
import html
import json
import random
import re

def _utf16_index_map(text: str):
    """Map UTF-16 code-unit offsets to Python string indexes.

    Telegram's entity offsets count UTF-16 code units, so anything outside
    the basic plane — an emoji, most notably — makes them disagree with
    Python's per-character indexing. Slicing on a raw offset would cut a
    surrogate pair in half."""
    pos_map = {0: 0}
    utf16_pos = 0
    for i, ch in enumerate(text):
        utf16_pos += len(ch.encode("utf-16-le")) // 2
        pos_map[utf16_pos] = i + 1
    return pos_map

def telegram_entities_to_discord(text: str, entities):
    """Render Telegram formatting entities as Discord markdown.

    Entities are ranges, possibly overlapping, so the markers are collected
    per index and emitted in one pass — closing markers before opening ones
    at the same position, so nested spans do not cross. Quote prefixes are
    emitted against the original indexes too: changing the text before reading
    later entities would shift their UTF-16 offsets."""
    if not text:
        return ""
    if not entities:
        return text

    pos_map = _utf16_index_map(text)
    opens = {}
    closes = {}
    quotes = set()
    quote_breaks = set()

    def add_open(i, token):
        """Queue an opening marker at a string index."""
        opens.setdefault(i, []).append(token)

    def add_close(i, token):
        """Queue a closing marker at a string index."""
        closes.setdefault(i, []).append(token)

    for e in sorted(entities, key=lambda e: (e.offset, -e.length)):
        start = pos_map.get(getattr(e, "offset", 0))
        end = pos_map.get(getattr(e, "offset", 0) + getattr(e, "length", 0))
        if start is None or end is None or start >= end:
            continue

        et = getattr(e, "type", "")
        if et == "bold":
            add_open(start, "**"); add_close(end, "**")
        elif et == "italic":
            add_open(start, "*"); add_close(end, "*")
        elif et == "underline":
            add_open(start, "__"); add_close(end, "__")
        elif et == "strikethrough":
            add_open(start, "~~"); add_close(end, "~~")
        elif et == "code":
            add_open(start, "`"); add_close(end, "`")
        elif et == "pre":
            lang = getattr(e, "language", "") or ""
            add_open(start, f"```{lang}\n"); add_close(end, "\n```")
        elif et == "spoiler":
            add_open(start, "||"); add_close(end, "||")
        elif et == "text_link":
            url = getattr(e, "url", "") or ""
            if url:
                add_open(start, "[")
                add_close(end, f"]({url})")
        elif et in ("blockquote", "expandable_blockquote"):
            quotes.add(start)
            quotes.update(i + 1 for i in range(start, end - 1) if text[i] == "\n")
            if start and text[start - 1] != "\n":
                quote_breaks.add(start)
            if end < len(text) and text[end] != "\n" and text[end - 1] != "\n":
                quote_breaks.add(end)

    out = []
    for i, ch in enumerate(text):
        if i in closes:
            out.append("".join(reversed(closes[i])))
        if i in quote_breaks:
            out.append("\n")
        if i in quotes:
            out.append("> ")
        if i in opens:
            out.append("".join(opens[i]))
        out.append(ch)
    end_idx = len(text)
    if end_idx in closes:
        out.append("".join(reversed(closes[end_idx])))
    return "".join(out)

def telegram_message_html(message):
    """Render Telegram text/captions without requiring premium emoji rights.

    Custom emoji keep their ordinary Unicode fallback. Unsupported or malformed
    decoration must never remove the message's readable text.
    """
    from aiogram.utils.text_decorations import html_decoration

    text = getattr(message, "text", None)
    entities = getattr(message, "entities", None)
    if text is None:
        text = getattr(message, "caption", None)
        entities = getattr(message, "caption_entities", None)
    if text is None:
        return None
    entities = [e for e in (entities or ()) if e.type != "custom_emoji"]
    try:
        return html_decoration.unparse(text, entities)
    except Exception:
        return escape_html(text)

def discord_to_telegram_html(text: str):
    """Render Discord markdown as the HTML subset Telegram accepts.

    The text is escaped first and the tags built on the escaped form, so a
    literal '<' in someone's message can never become markup. Order matters:
    links and code blocks are converted before the inline emphasis rules, so
    that markdown inside a URL or a code span is left alone. Italic is last
    and guarded against matching the '**' of bold."""
    if not text:
        return ""

    text = re.sub(r"<(https?://[^\s>]+)>", r"\1", text)

    escaped = html.escape(text)

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )

    escaped = re.sub(
        r"```([a-zA-Z0-9_-]*)\n([\s\S]*?)```",
        lambda m: f"<pre><code>{m.group(2)}</code></pre>",
        escaped,
    )
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<u>\1</u>", escaped)
    escaped = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"\|\|([^|]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)

    lines = escaped.splitlines()
    converted = []
    for ln in lines:
        if ln.startswith("&gt; "):
            converted.append(f"<blockquote>{ln[5:]}</blockquote>")
        else:
            converted.append(ln)
    return "\n".join(converted)

def escape_html(text: str):
    """Escape text for Telegram's HTML parse mode (None becomes empty)."""
    return html.escape(text or "")

_DISCORD_TS_RE = re.compile(r"(?:<|&lt;)t:(-?\d+)(?::([tTdDfFsSR]))?(?:>|&gt;)")

def _ts_ordinal(n):
    """English ordinal of a day number: 1 → 1st, 2 → 2nd, 13 → 13th."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def convert_discord_timestamps(text, lang="en"):
    """Replace Discord <t:unix:style> markup with readable, localized text (Telegram
    can't render it). Rendered in the bot host's local timezone (set the host TZ to
    the community's). Date order and month/weekday names follow `lang`."""
    if not text or ("<t:" not in text and "&lt;t:" not in text):
        return text
    from datetime import datetime
    from utils import localized, plural_ru, plural_pl, plural_en

    months = localized("month_names", lang)
    weekdays = localized("weekday_names", lang)
    plural = plural_ru if lang in ("ru", "uk") else plural_pl if lang == "pl" else plural_en

    def fmt_time(dt, secs):
        """Clock time in the target language's convention (12-hour with
        AM/PM for English, 24-hour elsewhere)."""
        if lang == "en":
            hour = dt.hour % 12 or 12
            ampm = "AM" if dt.hour < 12 else "PM"
            return f"{hour}:{dt.minute:02d}:{dt.second:02d} {ampm}" if secs else f"{hour}:{dt.minute:02d} {ampm}"
        return f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}" if secs else f"{dt.hour:02d}:{dt.minute:02d}"

    def fmt_num(dt):
        """All-numeric date in the target language's field order."""
        if lang == "en":
            return f"{dt.month:02d}/{dt.day:02d}/{dt.year}"
        if lang in ("es", "pt"):
            return f"{dt.day:02d}/{dt.month:02d}/{dt.year}"
        return f"{dt.day:02d}.{dt.month:02d}.{dt.year}"

    def fmt_long(dt):
        """Spelled-out date with the localized month name and whatever
        connectives the language wants ('de', 'г.', …)."""
        month = months[dt.month - 1] if isinstance(months, (list, tuple)) and len(months) >= 12 else str(dt.month)
        if lang == "en":
            return f"{month} {_ts_ordinal(dt.day)}, {dt.year}"
        if lang in ("es", "pt"):
            return f"{dt.day} de {month} de {dt.year}"
        if lang == "ru":
            return f"{dt.day} {month} {dt.year} г."
        if lang == "uk":
            return f"{dt.day} {month} {dt.year} р."
        return f"{dt.day} {month} {dt.year}"

    def fmt_relative(unix):
        """"in 3 hours" / "5 days ago", with the plural form the target
        language needs for that number. Frozen at render time — unlike
        Discord's live <t:…:R>, a relayed copy cannot keep counting."""
        delta = unix - int(datetime.now().timestamp())
        past = delta < 0
        s = abs(delta)
        if s < 60:
            val, key = s, "ts_unit_seconds"
        elif s < 3600:
            val, key = s // 60, "ts_unit_minutes"
        elif s < 86400:
            val, key = s // 3600, "ts_unit_hours"
        elif s < 2592000:
            val, key = s // 86400, "ts_unit_days"
        elif s < 31536000:
            val, key = s // 2592000, "ts_unit_months"
        else:
            val, key = s // 31536000, "ts_unit_years"
        forms = localized(key, lang)
        unit = plural(val, forms) if isinstance(forms, (list, tuple)) else str(forms)
        tmpl = localized("ts_ago" if past else "ts_in", lang)
        try:
            return tmpl.format(value=val, unit=unit)
        except Exception:
            return f"{val} {unit}"

    def repl(m):
        """Replace one <t:unix:style> match; an unparsable timestamp is left
        exactly as it was rather than dropped."""
        try:
            unix = int(m.group(1))
            dt = datetime.fromtimestamp(unix)
        except Exception:
            return m.group(0)
        style = m.group(2) or "f"
        if style == "d":
            return fmt_num(dt)
        if style == "D":
            return fmt_long(dt)
        if style == "t":
            return fmt_time(dt, False)
        if style == "T":
            return fmt_time(dt, True)
        if style == "F":
            wd = weekdays[dt.weekday()] if isinstance(weekdays, (list, tuple)) and len(weekdays) >= 7 else ""
            return f"{wd}, {fmt_long(dt)} {fmt_time(dt, False)}"
        if style == "s":
            return f"{fmt_num(dt)} {fmt_time(dt, False)}"
        if style == "S":
            return f"{fmt_num(dt)} {fmt_time(dt, True)}"
        if style == "R":
            return fmt_relative(unix)
        return f"{fmt_long(dt)} {fmt_time(dt, False)}"

    return _DISCORD_TS_RE.sub(repl, text)

DISCORD_MSG_LIMIT = 2000
TELEGRAM_MSG_LIMIT = 4096

def clip_text(text, limit):
    """Cut text to a platform limit, marking the cut with an ellipsis."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

def _clip_escaped_html(escaped, limit):
    """Cut already-escaped text without leaving a half-written entity —
    a trailing '&amp' fragment would render as literal garbage."""
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: max(limit - 1, 0)]
    cut = re.sub(r"&[#a-zA-Z0-9]{0,9}$", "", cut)
    return cut.rstrip() + "…"

def build_telegram_text(header, body_html, body_plain, *, spoiler=False):
    """Собирает header+body для Telegram с учётом лимита 4096 символов.
    Если форматированный body слишком длинный, откатывается на экранированный
    plain-текст, чтобы обрезка не ломала HTML-теги."""
    header_html = escape_html(header)
    if spoiler:
        opening, closing = "<tg-spoiler>", "</tg-spoiler>"
        budget = max(TELEGRAM_MSG_LIMIT - len(header_html) - len(opening)
                     - len(closing) - 1, 0)
        body = _clip_escaped_html(escape_html(body_plain or ""), budget)
        return f"{header_html}\n{opening}{body}{closing}".strip()
    text = f"{header_html}\n{body_html}".strip()
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return text
    budget = max(TELEGRAM_MSG_LIMIT - len(header_html) - 1, 0)
    body = _clip_escaped_html(escape_html(body_plain or ""), budget)
    return f"{header_html}\n{body}".strip()

def relay_header_prefix(messenger_name, place_name):
    """``[Messenger | Community]`` — or just ``[Messenger]`` for a source with no
    community behind it, such as a Bluesky feed attached with `/setbskyfeed`."""
    if place_name:
        return f"[{messenger_name} | {place_name}]"
    return f"[{messenger_name}]"

def clean_display_name(value, max_len=64):
    """Имена пользователей/чатов попадают в заголовок relay-сообщения:
    убираем переводы строк (защита от подделки заголовка) и ограничиваем длину."""
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return cleaned[:max_len] or "Unknown"

from utils import (
    get_chat_lang,
    localized_reply_unknown,
    localized_reply_external,
    localized_reply_webhook,
    localized_file_count_text,
    localized_forward_from_chat,
    localized_forward_from_user,
    localized_forward_unknown,
    localized_sticker,
    localized_voice_message,
    localized_video_message,
    localized,
)

_telegram_usernames = {}

def note_telegram_chat(chat):
    """Remember the public @username of a Telegram group the bot has seen.

    A link to a message of a public group opens for anyone as
    ``t.me/<username>/<id>``; the ``t.me/c/<id>/<id>`` form, which is all a
    bare chat id allows, opens only for the group's members. The username is
    taken from every inbound message and from the chat a cross-chat reply
    names, and held in memory only: after a restart a group gets the
    members-only form until it is seen again."""
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return
    username = getattr(chat, "username", None)
    if username:
        _telegram_usernames[str(chat_id)] = username
    else:
        _telegram_usernames.pop(str(chat_id), None)

def telegram_message_link(chat_key, message_id):
    """A t.me link to one message of a supergroup or channel, or None.

    `chat_key` is the bridge's ``<chat id>:<topic>`` form (a bare chat id is
    accepted too). Basic groups and private chats have no message links,
    which is why only ``-100…`` ids get one."""
    group, _, thread = str(chat_key).partition(":")
    message_id = str(message_id)
    if not (group.startswith("-100") and group[4:].isdigit() and message_id.isdigit()):
        return None
    place = _telegram_usernames.get(group) or f"c/{group[4:]}"
    topic = f"{thread}/" if thread.isdigit() and thread != "0" else ""
    return f"https://t.me/{place}/{topic}{message_id}"

def discord_message_link(chat_key, message_id):
    """A discord.com link to one message of a ``<guild>:<channel>`` chat, or
    None for anything else (the ``dm:<user>`` chats of appeals included)."""
    guild, _, channel = str(chat_key).partition(":")
    if not (guild.isdigit() and channel.isdigit() and str(message_id).isdigit()):
        return None
    return f"https://discord.com/channels/{guild}/{channel}/{message_id}"

def message_link(platform, chat_key, message_id):
    """The link to a message wherever the bridge keeps one: a Discord or a
    Telegram message. Feed posts, inbox chats and Telegram chats without
    message links have none."""
    if platform == "discord":
        return discord_message_link(chat_key, message_id)
    if platform == "telegram":
        return telegram_message_link(chat_key, message_id)
    return None

class ReplyLine(str):
    """A "(replying to …)" line that links to the replied-to message.

    The string itself is the Discord rendering — a markdown link, which
    Discord shows in bot and webhook messages alike, with the address in
    angle brackets so that it does not grow a preview. `html` is the
    Telegram rendering of the same line, `plain` the text one for the
    fallback `build_telegram_text` takes at the length limit, and `url` the
    address itself: a Telegram copy previews the first link it holds, and
    the Telegram deliverers need it to keep that preview on the message's
    own links (`telegram_link_preview_kwargs`).

    Being a str, it travels through every `send_to_chat` callback unchanged;
    a reply line that is not a ReplyLine is plain text, as it always was."""

    def __new__(cls, text, *, html_text, plain, url):
        line = super().__new__(cls, text)
        line.html = html_text
        line.plain = plain
        line.url = url
        return line

_MARKDOWN_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^()\s]+)\)")

def _reply_link_line(name, url, lang):
    """The "(replying to [a message by …](link))" line in all three forms.

    Worded by the `reply_webhook` key, the line a webhook copy has always
    used for a reply it cannot reference natively."""
    template_line = localized_reply_webhook(name, url, lang)
    return ReplyLine(
        localized_reply_webhook(name, f"<{url}>", lang),
        html_text=_MARKDOWN_LINK_RE.sub(r'<a href="\2">\1</a>', escape_html(template_line)),
        plain=_MARKDOWN_LINK_RE.sub(r"\1: \2", template_line),
        url=url,
    )

def reply_line_html(line):
    """A reply line in Telegram HTML: a ReplyLine brings its own, a plain line
    is escaped."""
    html_text = getattr(line, "html", None)
    return html_text if html_text is not None else escape_html(line)

def reply_line_plain(line):
    """A reply line as plain text: a ReplyLine spells its link out."""
    plain = getattr(line, "plain", None)
    return plain if plain is not None else str(line)

_FIRST_WEB_LINK_RE = re.compile(r'href="(https?://[^"]+)"|(https?://[^\s<>"]+)')

def telegram_link_preview_kwargs(reply_line, body_html):
    """Link-preview arguments for a Telegram copy whose reply line is a link.

    Telegram previews the first link of a message, and a reply line stands
    above the body — left alone, every such copy would preview the replied-to
    message instead of the link its sender actually posted (or the GALLERY
    file). So the preview is pinned to the first web link of the body, or
    switched off when the body has none. `body_html` may already carry the
    reply line on top; it is looked past. Empty when the line is not a link,
    which leaves Telegram's own choice alone."""
    url = getattr(reply_line, "url", None)
    if not url:
        return {}
    from aiogram.types import LinkPreviewOptions

    body_html = body_html or ""
    if body_html.startswith(reply_line.html):
        body_html = body_html[len(reply_line.html):]
    match = _FIRST_WEB_LINK_RE.search(body_html)
    if not match:
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    found = html.unescape(match.group(1) or match.group(2)).rstrip(".,;:!?")
    while found.endswith(")") and found.count(")") > found.count("("):
        found = found[:-1]
    return {"link_preview_options": LinkPreviewOptions(url=found)}

def _links_allowed(bridge_id):
    """Whether a reply in this bridge may link to messages elsewhere.

    Only ordinary and sponsor bridges. An appeal or an inbox conversation
    joins a private person to a staff chat, and a link — or a third party's
    name — from one side would tell the other where the staff talk."""
    from db.ranges import bridge_kind
    return bridge_kind(bridge_id) in ("ordinary", "sponsor")

def _target_bridge_id(chat):
    """The bridge a target chat belongs to. A `chats` row says so itself; a
    `message_copies` row, which is what the edit paths pass, does not."""
    try:
        return chat["bridge_id"]
    except (IndexError, KeyError):
        pass
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (chat["chat_id"],)
    ).fetchone()
    return row["bridge_id"] if row else None

def _chat_server(chat_key):
    """The server part of a chat key: the Discord guild, or the Telegram
    group a topic belongs to."""
    return str(chat_key).split(":", 1)[0]

def _replied_message_link(chat, reply_to_msg_db_id, origin_row):
    """``(author name, link)`` for a replied-to message that has no copy in
    this chat, or None when nothing may or can be linked.

    The original comes first. When the original has no link (a feed post, a
    Telegram chat without message links), a copy does: the one on the
    target's own server — another channel of the same Discord guild, another
    topic of the same Telegram group — and failing that one of the others,
    picked at random. The pick is seeded by the message and the target, so
    an edit that rebuilds the line points where the first copy did."""
    if chat["platform"] not in ("discord", "telegram") or origin_row is None:
        return None
    if not (_links_allowed(origin_row["bridge_id"]) and _links_allowed(_target_bridge_id(chat))):
        return None
    url = message_link(origin_row["origin_platform"], origin_row["origin_chat_id"],
                       origin_row["origin_message_id"])
    if url is None:
        server = _chat_server(chat["chat_id"])
        same_server, elsewhere = [], []
        for copy in db.cur.execute(
                "SELECT platform, chat_id, message_id_platform FROM message_copies"
                " WHERE message_id=? AND COALESCE(kind,'main')='main' ORDER BY id",
                (reply_to_msg_db_id,)).fetchall():
            link = message_link(copy["platform"], copy["chat_id"], copy["message_id_platform"])
            if link is None:
                continue
            if copy["platform"] == chat["platform"] and _chat_server(copy["chat_id"]) == server:
                same_server.append(link)
            else:
                elsewhere.append(link)
        pool = same_server or elsewhere
        if pool:
            url = random.Random(f"{reply_to_msg_db_id}:{chat['chat_id']}").choice(pool)
    if url is None:
        return None
    return origin_row["origin_sender_name"], url

def normalize_external_reply(external_reply):
    """The stored form of a Telegram reply to a message the bridge does not
    know — one in a chat outside every bridge, or in a topic nobody bridged.

    ``{"name", "url"}`` as `telegram_bot/relay.py: _external_reply` builds it
    (either may be None); ``{}`` for the bare flag of messages frozen for
    consent before the name was kept, which still earns the generic "another
    chat" line; None when the message is no such reply."""
    if isinstance(external_reply, dict):
        return {"name": external_reply.get("name"), "url": external_reply.get("url")}
    return {} if external_reply else None

def load_external_reply(value):
    """`messages.reply_external` back into the form
    `normalize_external_reply` produces."""
    if not value:
        return None
    try:
        return normalize_external_reply(json.loads(value) or True)
    except (TypeError, ValueError):
        return None

def _external_reply_line(chat, external, lang):
    """The reply line for a message the bridge does not know: a link to it
    when one exists, the author's name when only that is known, and the
    generic "(replying to a message from another chat)" otherwise — always
    the generic one in appeals and inbox conversations (`_links_allowed`)."""
    name = re.sub(r"[\s\[\]]+", " ", clean_display_name(external.get("name"))).strip() \
        if external.get("name") else None
    if not name or not _links_allowed(_target_bridge_id(chat)):
        return localized_reply_external(lang)
    if external.get("url") and chat["platform"] in ("discord", "telegram"):
        return _reply_link_line(name, external["url"], lang)
    return localized("reply_external_named", lang, name=name)

def _resolve_reply_for_chat(chat, lang, reply_to_msg_db_id, external=None):
    """Resolve how a reply should be shown in one target chat.

    Returns ``(reply_line, reply_to_platform_message_id)``, mutually
    exclusive and both ``None`` when it isn't a reply:
      * ``reply_to_platform_message_id`` set → the replied-to message has a
        copy in this chat (or is the original here): a native reply;
      * ``reply_line`` set → no copy here. A `ReplyLine` naming the author
        and linking to the message elsewhere (`_replied_message_link`), or the
        localized "unknown message" line when nothing can be linked.
    `external` is a reply to a message the bridge does not know
    (`normalize_external_reply`), used only when `reply_to_msg_db_id` is
    empty — a reply the bridge could match to its own records never needs it.
    """
    if reply_to_msg_db_id:
        if reply_to_msg_db_id < 0:
            return localized_reply_unknown(lang), None
        copy_row = db.cur.execute(
            "SELECT message_id_platform FROM message_copies"
            " WHERE message_id=? AND platform=? AND chat_id=? AND COALESCE(kind,'main')='main'",
            (reply_to_msg_db_id, chat["platform"], chat["chat_id"])
        ).fetchone()
        if copy_row:
            return None, copy_row["message_id_platform"]
        origin_row = db.cur.execute(
            "SELECT bridge_id, origin_platform, origin_chat_id, origin_message_id,"
            " origin_sender_name FROM messages WHERE id=?",
            (reply_to_msg_db_id,)
        ).fetchone()
        if (origin_row
                and origin_row["origin_platform"] == chat["platform"]
                and origin_row["origin_chat_id"] == chat["chat_id"]):
            return None, origin_row["origin_message_id"]
        link = _replied_message_link(chat, reply_to_msg_db_id, origin_row)
        if link:
            return _reply_link_line(link[0], link[1], lang), None
        return localized_reply_unknown(lang), None
    if external is not None:
        return _external_reply_line(chat, external, lang), None
    return None, None

def _webhook_reply_link_line(chat, lang, reply_to_msg_db_id, reply_to_platform_message_id):
    """Markdown-link "replying to …" first line for a Discord webhook copy (a
    webhook message can't carry a native reply reference). ``None`` if no link
    can be formed."""
    if not reply_to_platform_message_id:
        return None
    replied_name = None
    if reply_to_msg_db_id and reply_to_msg_db_id > 0:
        nrow = db.cur.execute(
            "SELECT origin_sender_name FROM messages WHERE id=?",
            (reply_to_msg_db_id,)
        ).fetchone()
        if nrow:
            replied_name = nrow["origin_sender_name"]
    try:
        guild_id, channel_id = chat["chat_id"].split(":")
    except Exception:
        return None
    link = f"https://discord.com/channels/{guild_id}/{channel_id}/{reply_to_platform_message_id}"
    return localized_reply_webhook(replied_name, link, lang)

def _forward_line(forward_type, forward_name, lang):
    """Localized "forwarded from …" line, or ``None`` when it isn't a forward."""
    if forward_type == "chat":
        return localized_forward_from_chat(forward_name or "unknown", lang)
    if forward_type == "user":
        return localized_forward_from_user(forward_name or "unknown", lang)
    if forward_type == "unknown":
        return localized_forward_unknown(lang)
    return None

def apply_gallery_links(platform, text, discord_text, telegram_html, gallery_urls, *, lang="en"):
    """Attach the GALLERY links of re-uploaded Telegram files to one copy.

    Discord gets every link in the message footer. Telegram keeps the text
    separate; every attachment follows in its own message, including the first.
    """
    if not gallery_urls:
        return text, discord_text, telegram_html
    if platform == "telegram":
        if not text.strip():
            text = f"[{localized_file_count_text(len(gallery_urls), lang)}]"
            return text, text, escape_html(text)
        return text, discord_text, telegram_html

    footer = "\n".join(gallery_urls) if platform == "discord" else gallery_urls[0]
    text = f"{text}\n{footer}".strip()
    discord_text = f"{discord_text}\n{footer}".strip()
    if telegram_html is not None:
        telegram_html = f"{telegram_html}\n{escape_html(footer)}".strip()
    return text, discord_text, telegram_html

def relay_edit_prefix_lines(message_db_id, chat, lang, *, webhook_link=False):
    """The lines a relayed copy carries above its body — "(replying to …)" and
    "(forwarded from …)" — rebuilt for an edit, outermost first.

    An edit rewrites the whole text of a copy, so anything the first delivery
    put above the body has to be put there again or the edit quietly strips
    it. What belongs there depends on the target: a chat that got a *native*
    reply reference keeps it through the edit and must not gain a line saying
    the same thing, which is why the reply line comes from
    `_resolve_reply_for_chat` rather than from the stored row alone.

    `webhook_link` is for a Discord webhook copy, which cannot hold a native
    reference at all and therefore carries a markdown link instead.

    The reply line may be a `ReplyLine`; the lines are joined as Discord
    markdown, and `prefix_relay_edit_body` renders them for Telegram."""
    row = db.cur.execute(
        "SELECT reply_to_message_id, reply_external, forward_type, forward_name"
        " FROM messages WHERE id=?",
        (message_db_id,)
    ).fetchone()
    if not row:
        return []
    reply_to_msg_db_id = row["reply_to_message_id"]

    reply_line, reply_to_platform_message_id = _resolve_reply_for_chat(
        chat, lang, reply_to_msg_db_id, load_external_reply(row["reply_external"]))
    if webhook_link:
        reply_line = _webhook_reply_link_line(
            chat, lang, reply_to_msg_db_id, reply_to_platform_message_id) or reply_line

    fwd_line = _forward_line(row["forward_type"], row["forward_name"], lang)
    return [line for line in (reply_line, fwd_line) if line]

def prefix_relay_edit_body(message_db_id, chat, lang, body_html, body_plain):
    """Put the reply/forward lines back above an edited body, in both the
    HTML and the plain form a Telegram-shaped copy needs. Returns
    ``(body_html, body_plain)`` unchanged when the message has no such lines.

    The two forms have to be prefixed together — `build_telegram_text` falls
    back from one to the other when the formatted text overruns the 4096-char
    limit, and a fallback that silently drops the prefix would make the line
    appear and disappear with the length of the message. A body without an
    HTML form gets one when a line is a link, which plain text cannot hold."""
    lines = relay_edit_prefix_lines(message_db_id, chat, lang)
    if not lines:
        return body_html, body_plain
    if body_html is None and any(getattr(line, "html", None) for line in lines):
        body_html = escape_html(body_plain)
    prefix_html = "\n".join(reply_line_html(line) for line in lines)
    prefix_plain = "\n".join(reply_line_plain(line) for line in lines)
    prefixed_html = f"{prefix_html}\n{body_html}".strip() if body_html is not None else None
    return prefixed_html, f"{prefix_plain}\n{body_plain}".strip()

def relay_edit_link_preview_kwargs(message_db_id, chat, lang, body_html):
    """`telegram_link_preview_kwargs` for an edited Telegram copy, whose
    `body_html` already went through `prefix_relay_edit_body`. An edit
    rebuilds the preview as well, so it needs the same pin the first
    delivery got."""
    lines = relay_edit_prefix_lines(message_db_id, chat, lang)
    reply_line = next((line for line in lines if getattr(line, "url", None)), None)
    return telegram_link_preview_kwargs(reply_line, body_html)

def build_discord_webhook_relay_body(message_db_id, chat, lang, body_discord):
    """Reconstruct a webhook copy's full content (reply + forward prefix lines,
    then body) so that editing the original keeps the prefixes the initial relay
    added — a webhook message stores them inline in its content rather than as a
    native reply reference."""
    lines = relay_edit_prefix_lines(message_db_id, chat, lang, webhook_link=True)
    return "\n".join(lines + [body_discord]).strip()

async def relay_message(
    *,
    bridge_id,
    origin_platform,
    origin_chat_id,
    origin_message_id,
    origin_sender_id,
    messenger_name,
    place_name,
    sender_name,
    text,
    discord_text=None,
    discord_embeds=None,
    telegram_html=None,
    reply_to_msg_db_id=None,
    send_to_chat_func,
    telegram_file_count=None,
    forward_type=None,
    forward_name=None,
    external_reply=False,
    is_bot_sender=False,
    avatar_url=None,
    gallery_urls=None,
    gallery_fallback=None,
    targets=None,
    file_count=None,
    file_count_source="telegram",
    body_renderer=None,
):
    """Record one origin message and deliver a copy of it to every target chat.

    This is the fan-out both platforms end in, and the only place that writes
    the `messages` and `message_copies` rows the edit, delete, reply and whois
    paths all depend on — the row is inserted before any send, so a copy can
    never exist without an origin to attach it to.

    Per target chat it: resolves that chat's language, builds the
    ``[Messenger | Place] Sender:`` header (bot senders get the marker their
    platform can render), resolves the reply to either a native reference or a
    line (`_resolve_reply_for_chat`: a link to the replied-to message where
    one exists, the "unknown message" line where none does), prepends the
    forward line, substitutes
    the placeholder markers (`__TG_FILES_n__`, `__TG_STICKER__`,
    `__TG_VOICE__`, `__TG_VIDEO_NOTE__`) with localized text, attaches the
    GALLERY links, and appends the "[N files from …]" footer. The origin chat
    is skipped — it already has the message.

    Ordinary sponsor copies are rendered into a durable queue and sent by
    sponsor_delivery when the owner's day/minute capacity is available.
    For other bridge kinds, `send_to_chat_func` does the actual sending; a target it returns nothing
    for is simply left without a copy, which is what keeps one unreachable
    chat from failing the whole relay. `targets` overrides the bridge's chat
    list, which is how feed posts reach the chats `feed_targets` resolves.
    Returns the new `messages.id`.

    `gallery_fallback` makes the GALLERY links a per-target matter: the
    `/allow-files` consent is each community's own, so a chat whose community
    never gave it must not be handed the links even when the chat next to it
    in the bridge asked for them. It is the body such a chat gets instead —
    ``{"text", "discord", "telegram_html", "file_count"}``, the same message
    rendered with the "[N files from Telegram]" marker standing in for the
    files — and its presence is what turns the per-target check on. Left
    ``None`` (feed posts, whose attachments need no consent at all) the links
    go to every target, as they always did.

    `body_renderer` is for senders whose text is not one fixed string but
    something written per reader — wiki activity, which is composed in each
    chat's own language and may come with a Discord embed. It is called once
    per target as ``body_renderer(chat, lang)`` and returns a dict with any
    of ``text`` / ``discord`` / ``telegram_html`` / ``embed``; whatever it
    provides replaces the corresponding argument for that chat, and a chat it
    returns ``None`` for is skipped entirely (that is how per-chat filters
    drop an event without dropping the whole relay).

    `external_reply` is a Telegram reply to a message the bridge does not
    know (`normalize_external_reply`). It is stored with the row, so that an
    edit and a copy delivered later from the sponsor queue rebuild the same
    reply line the first copies got.
    """
    place_name = clean_display_name(place_name) if place_name else None
    sender_name = clean_display_name(sender_name)
    external = normalize_external_reply(external_reply)

    if targets is None:
        targets = db.get_bridge_chats(bridge_id)
    from sponsors import sponsor_relay_owner, sponsor_quota_tier
    from utils import is_admin
    queue_owner = sponsor_relay_owner(bridge_id)
    if queue_owner is not None and sponsor_quota_tier("discord", queue_owner) == 0 and not is_admin("discord", queue_owner):
        return None
    db.cur.execute(
        """
        UPDATE bridge_rules
        SET message_counter = message_counter + 1
        WHERE bridge_id=?
        """,
        (bridge_id,)
    )
    db.conn.commit()

    inserted = db.cur.execute(
        """
        INSERT INTO messages
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, origin_sender_id, origin_sender_name,
         reply_to_message_id, reply_external, forward_type, forward_name, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, str(origin_sender_id), sender_name,
         reply_to_msg_db_id,
         json.dumps(external, ensure_ascii=False) if external is not None else None,
         None if is_bot_sender else forward_type,
         None if is_bot_sender else forward_name,
         int(time.time()))
    )
    msg_id = inserted.lastrowid
    db.conn.commit()

    for chat in targets:
        if chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id:
            continue

        lang = get_chat_lang(chat["chat_id"])

        rendered = None
        if body_renderer is not None:
            rendered = body_renderer(chat, lang)
            if rendered is None:
                continue

        prefix = relay_header_prefix(messenger_name, place_name)
        if is_bot_sender:
            if chat["platform"] == "discord":
                header = f"{prefix} {sender_name} <:bot:1513502696953352363>:"
            else:
                header = f"{prefix} {sender_name} 🤖:"
        else:
            header = f"{prefix} {sender_name}:"

        reply_line, reply_to_platform_message_id = _resolve_reply_for_chat(
            chat, lang, reply_to_msg_db_id, external)

        reply_link_line = None
        if (chat["platform"] == "discord"
                and reply_to_platform_message_id
                and db.get_webhooks_enabled(chat["chat_id"])):
            reply_link_line = _webhook_reply_link_line(
                chat, lang, reply_to_msg_db_id, reply_to_platform_message_id
            )

        current_text = text
        current_discord_text = discord_text if discord_text is not None else current_text
        current_telegram_html = telegram_html
        current_embed = None
        if rendered is not None:
            current_text = rendered.get("text", current_text)
            current_discord_text = rendered.get("discord", current_text)
            current_telegram_html = rendered.get("telegram_html", None)
            current_embed = rendered.get("embed")

        current_gallery_urls = gallery_urls
        eff_file_count = telegram_file_count
        if (gallery_urls and gallery_fallback is not None
                and not db.chat_file_consent(chat["platform"], chat["chat_id"], bridge_id)):
            current_gallery_urls = None
            current_text = gallery_fallback.get("text", current_text)
            current_discord_text = gallery_fallback.get("discord") or current_text
            current_telegram_html = gallery_fallback.get("telegram_html")
            eff_file_count = gallery_fallback.get("file_count")

        eff_forward_type = None if is_bot_sender else forward_type
        eff_forward_name = None if is_bot_sender else forward_name

        fwd_line = _forward_line(eff_forward_type, eff_forward_name, lang)
        if fwd_line:
            current_text = f"{fwd_line}\n{current_text}".strip()
            current_discord_text = f"{fwd_line}\n{current_discord_text}".strip()
            if current_telegram_html is not None:
                current_telegram_html = f"{escape_html(fwd_line)}\n{current_telegram_html}".strip()

        if eff_file_count is not None:
            marker = localized_file_count_text(eff_file_count, lang)
            current_text = current_text.replace(
                f"__TG_FILES_{eff_file_count}__",
                marker
            )
            current_discord_text = current_discord_text.replace(
                f"__TG_FILES_{eff_file_count}__",
                marker
            )
            if current_telegram_html is not None:
                current_telegram_html = current_telegram_html.replace(
                    f"__TG_FILES_{eff_file_count}__",
                    escape_html(marker)
                )

        if "__TG_STICKER__" in current_text:
            sticker_marker = localized_sticker(lang)
            current_text = current_text.replace("__TG_STICKER__", sticker_marker)
            current_discord_text = current_discord_text.replace("__TG_STICKER__", sticker_marker)
            if current_telegram_html is not None:
                current_telegram_html = current_telegram_html.replace("__TG_STICKER__", escape_html(sticker_marker))

        if "__TG_VOICE__" in current_text:
            voice_marker = localized_voice_message(lang)
            current_text = current_text.replace("__TG_VOICE__", voice_marker)
            current_discord_text = current_discord_text.replace("__TG_VOICE__", voice_marker)
            if current_telegram_html is not None:
                current_telegram_html = current_telegram_html.replace("__TG_VOICE__", escape_html(voice_marker))

        if "__TG_VIDEO_NOTE__" in current_text:
            video_marker = localized_video_message(lang)
            current_text = current_text.replace("__TG_VIDEO_NOTE__", video_marker)
            current_discord_text = current_discord_text.replace("__TG_VIDEO_NOTE__", video_marker)
            if current_telegram_html is not None:
                current_telegram_html = current_telegram_html.replace("__TG_VIDEO_NOTE__", escape_html(video_marker))

        current_text, current_discord_text, current_telegram_html = apply_gallery_links(
            chat["platform"], current_text, current_discord_text, current_telegram_html,
            current_gallery_urls, lang=lang
        )

        if file_count:
            footer = f"[{localized_file_count_text(file_count, lang, source=file_count_source)}]"
            current_text = f"{current_text}\n{footer}".strip()
            current_discord_text = f"{current_discord_text}\n{footer}".strip()
            if current_telegram_html is not None:
                current_telegram_html = f"{current_telegram_html}\n{escape_html(footer)}".strip()

        send_kwargs = {}
        if current_embed is not None:
            send_kwargs["embed"] = current_embed
        if chat["platform"] == "discord" and discord_embeds:
            send_kwargs["embeds"] = discord_embeds
        delivery_payload = dict(
            header=header,
            body_plain=current_text,
            body_discord=current_discord_text,
            body_telegram_html=current_telegram_html,
            reply_line=reply_line,
            reply_link_line=reply_link_line,
            reply_to_platform_message_id=reply_to_platform_message_id,
            sender_name=sender_name,
            place_name=place_name,
            messenger_name=messenger_name,
            avatar_url=avatar_url,
            is_bot_sender=is_bot_sender,
            **send_kwargs,
        )
        if queue_owner is not None:
            from sponsor_delivery import pack_payload
            options = getattr(send_to_chat_func, "relay_options", {})
            delivery_payload.update(options)
            if gallery_urls and gallery_fallback is not None:
                fallback_plain = gallery_fallback.get("text", text)
                fallback_discord = gallery_fallback.get("discord") or fallback_plain
                fallback_html = gallery_fallback.get("telegram_html")
                fallback_prefix = []
                if fwd_line:
                    fallback_prefix.append(fwd_line)
                fallback_count = gallery_fallback.get("file_count")
                markers = {
                    "__TG_STICKER__": localized_sticker(lang),
                    "__TG_VOICE__": localized_voice_message(lang),
                    "__TG_VIDEO_NOTE__": localized_video_message(lang),
                }
                if fallback_count is not None:
                    markers[f"__TG_FILES_{fallback_count}__"] = localized_file_count_text(fallback_count, lang)
                for marker, replacement in markers.items():
                    fallback_plain = fallback_plain.replace(marker, replacement)
                    fallback_discord = fallback_discord.replace(marker, replacement)
                    if fallback_html is not None:
                        fallback_html = fallback_html.replace(marker, escape_html(replacement))
                if fallback_prefix:
                    fallback_plain = "\n".join(fallback_prefix + [fallback_plain])
                    fallback_discord = "\n".join(fallback_prefix + [fallback_discord])
                    if fallback_html is not None:
                        fallback_html = "\n".join([escape_html(line) for line in fallback_prefix] + [fallback_html])
                if file_count:
                    fallback_plain = f"{fallback_plain}\n{footer}".strip()
                    fallback_discord = f"{fallback_discord}\n{footer}".strip()
                    if fallback_html is not None:
                        fallback_html = f"{fallback_html}\n{escape_html(footer)}".strip()
                delivery_payload["_file_consent_required"] = True
                delivery_payload["_file_fallback"] = {
                    "body_plain": fallback_plain, "body_discord": fallback_discord,
                    "body_telegram_html": fallback_html,
                }
            db.enqueue_sponsor_copy(queue_owner, bridge_id, msg_id, chat, pack_payload(delivery_payload))
            if chat["platform"] == "telegram" and current_gallery_urls:
                for url in current_gallery_urls:
                    extra = dict(delivery_payload, header="", body_plain=url, body_discord=url,
                                 body_telegram_html=escape_html(url), reply_line=None,
                                 reply_link_line=None, reply_to_platform_message_id=None)
                    db.enqueue_sponsor_copy(queue_owner, bridge_id, msg_id, chat, pack_payload(extra), "file")
            continue
        sent_id = await send_to_chat_func(chat, **delivery_payload)
        if not sent_id:
            continue

        db.cur.execute(
            """
            INSERT INTO message_copies
            (message_id, platform, chat_id, message_id_platform, kind)
            VALUES (?,?,?,?,'main')
            """,
            (msg_id, chat["platform"], chat["chat_id"], sent_id)
        )

        if chat["platform"] == "telegram" and current_gallery_urls:
            await _send_telegram_gallery_followups(
                chat, msg_id, current_gallery_urls, send_to_chat_func,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )

    db.conn.commit()
    if queue_owner is not None:
        from sponsor_delivery import drain_sponsor_copies
        await drain_sponsor_copies()
        from sponsors import claim_daily_limit_notice
        if db.queued_sponsor_copies(queue_owner) and claim_daily_limit_notice(
                bridge_id, origin_platform, origin_chat_id, targets, gallery_urls):
            from utils import localized
            for chat in targets:
                if chat["platform"] not in ("discord", "telegram"):
                    continue
                notice = localized("sponsor_relay_limit_notice", get_chat_lang(chat["chat_id"]))
                try:
                    await send_to_chat_func(chat, header="", body_plain=notice, body_discord=notice,
                                            body_telegram_html=escape_html(notice), reply_line=None,
                                            reply_to_platform_message_id=None)
                except Exception:
                    pass
    return msg_id

async def _send_telegram_gallery_followups(
    chat, msg_id, urls, send_to_chat_func, *, sender_name, place_name,
    messenger_name, avatar_url, is_bot_sender,
):
    """Post all GALLERY links into a Telegram chat, one message per
    file, right after the main copy.

    They are recorded as ``kind='file'`` copies of the same message, so deleting
    the original takes them along and a reply to any of them still resolves to
    the relayed message — while everything that renders the message body keeps
    working off the single ``kind='main'`` copy."""
    for url in urls:
        try:
            extra_id = await send_to_chat_func(
                chat,
                header="",
                body_plain=url,
                body_discord=url,
                body_telegram_html=escape_html(url),
                reply_line=None,
                reply_link_line=None,
                reply_to_platform_message_id=None,
                sender_name=sender_name,
                place_name=place_name,
                messenger_name=messenger_name,
                avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )
        except Exception:
            continue
        if not extra_id:
            continue
        db.cur.execute(
            """
            INSERT INTO message_copies
            (message_id, platform, chat_id, message_id_platform, kind)
            VALUES (?,?,?,?,'file')
            """,
            (msg_id, chat["platform"], chat["chat_id"], extra_id)
        )
