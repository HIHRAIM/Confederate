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

def _wrap_blockquote(segment: str) -> str:
    """Prefix every line of a segment with Discord's '> ' quote marker."""
    lines = segment.splitlines() or [segment]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)

def telegram_entities_to_discord(text: str, entities):
    """Render Telegram formatting entities as Discord markdown.

    Entities are ranges, possibly overlapping, so the markers are collected
    per index and emitted in one pass — closing markers before opening ones
    at the same position, so nested spans do not cross. A blockquote is the
    exception: it rewrites the text itself and restarts the conversion,
    because its marker goes at the head of every line rather than around the
    span."""
    if not text:
        return ""
    if not entities:
        return text

    pos_map = _utf16_index_map(text)
    opens = {}
    closes = {}

    def add_open(i, token):
        """Queue an opening marker at a string index."""
        opens.setdefault(i, []).append(token)

    def add_close(i, token):
        """Queue a closing marker at a string index."""
        closes.setdefault(i, []).append(token)

    for e in entities:
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
        elif et == "blockquote":
            seg = _wrap_blockquote(text[start:end])
            text = text[:start] + seg + text[end:]
            return telegram_entities_to_discord(text, [x for x in entities if x is not e])

    out = []
    for i, ch in enumerate(text):
        if i in closes:
            out.append("".join(reversed(closes[i])))
        if i in opens:
            out.append("".join(opens[i]))
        out.append(ch)
    end_idx = len(text)
    if end_idx in closes:
        out.append("".join(reversed(closes[end_idx])))
    return "".join(out)

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

def build_telegram_text(header, body_html, body_plain):
    """Собирает header+body для Telegram с учётом лимита 4096 символов.
    Если форматированный body слишком длинный, откатывается на экранированный
    plain-текст, чтобы обрезка не ломала HTML-теги."""
    header_html = escape_html(header)
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
)

def _resolve_reply_for_chat(chat, lang, reply_to_msg_db_id):
    """Resolve how a reply should be shown in one target chat.

    Returns ``(reply_line, reply_to_platform_message_id)``:
      * ``reply_to_platform_message_id`` set → the replied-to message has a copy
        in this chat, usable for a native reply reference or a link;
      * ``reply_line`` set (localized "unknown message") → no usable copy here.
    They are mutually exclusive; both are ``None`` when it isn't a reply.
    """
    reply_line = None
    reply_to_platform_message_id = None
    if reply_to_msg_db_id:
        if reply_to_msg_db_id < 0:
            reply_line = localized_reply_unknown(lang)
        else:
            copy_row = db.cur.execute(
                "SELECT message_id_platform FROM message_copies"
                " WHERE message_id=? AND platform=? AND chat_id=? AND COALESCE(kind,'main')='main'",
                (reply_to_msg_db_id, chat["platform"], chat["chat_id"])
            ).fetchone()
            if copy_row:
                reply_to_platform_message_id = copy_row["message_id_platform"]
            else:
                origin_row = db.cur.execute(
                    "SELECT origin_platform, origin_chat_id, origin_message_id FROM messages WHERE id=?",
                    (reply_to_msg_db_id,)
                ).fetchone()
                if (origin_row
                        and origin_row["origin_platform"] == chat["platform"]
                        and origin_row["origin_chat_id"] == chat["chat_id"]):
                    reply_to_platform_message_id = origin_row["origin_message_id"]
                else:
                    reply_line = localized_reply_unknown(lang)
    return reply_line, reply_to_platform_message_id

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

def apply_gallery_links(platform, text, discord_text, telegram_html, gallery_urls):
    """Attach the GALLERY links of re-uploaded Telegram files to one copy.

    Discord gets every link in the message footer (the first one expands into a
    preview, as usual). Telegram gets only the first one — the rest are sent as
    separate follow-up messages, one per file, so that every file gets its own
    preview instead of a single one standing in for all of them."""
    if not gallery_urls:
        return text, discord_text, telegram_html

    footer = "\n".join(gallery_urls) if platform == "discord" else gallery_urls[0]
    text = f"{text}\n{footer}".strip()
    discord_text = f"{discord_text}\n{footer}".strip()
    if telegram_html is not None:
        telegram_html = f"{telegram_html}\n{escape_html(footer)}".strip()
    return text, discord_text, telegram_html

def build_discord_webhook_relay_body(message_db_id, chat, lang, body_discord):
    """Reconstruct a webhook copy's full content (reply + forward prefix lines,
    then body) so that editing the original keeps the prefixes the initial relay
    added — a webhook message stores them inline in its content rather than as a
    native reply reference."""
    row = db.cur.execute(
        "SELECT reply_to_message_id, forward_type, forward_name FROM messages WHERE id=?",
        (message_db_id,)
    ).fetchone()
    reply_to_msg_db_id = row["reply_to_message_id"] if row else None
    forward_type = row["forward_type"] if row else None
    forward_name = row["forward_name"] if row else None

    body = body_discord
    fwd_line = _forward_line(forward_type, forward_name, lang)
    if fwd_line:
        body = f"{fwd_line}\n{body}".strip()

    reply_line, reply_to_platform_message_id = _resolve_reply_for_chat(chat, lang, reply_to_msg_db_id)
    prefix = _webhook_reply_link_line(chat, lang, reply_to_msg_db_id, reply_to_platform_message_id) or reply_line
    if prefix:
        body = f"{prefix}\n{body}"
    return body

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
    localized "unknown message" line, prepends the forward line, substitutes
    the placeholder markers (`__TG_FILES_n__`, `__TG_STICKER__`,
    `__TG_VOICE__`, `__TG_VIDEO_NOTE__`) with localized text, attaches the
    GALLERY links, and appends the "[N files from …]" footer. The origin chat
    is skipped — it already has the message.

    `send_to_chat_func` does the actual sending; a target it returns nothing
    for is simply left without a copy, which is what keeps one unreachable
    chat from failing the whole relay. `targets` overrides the bridge's chat
    list, which is how feed posts reach the chats `feed_targets` resolves.
    Returns the new `messages.id`.

    `body_renderer` is for senders whose text is not one fixed string but
    something written per reader — wiki activity, which is composed in each
    chat's own language and may come with a Discord embed. It is called once
    per target as ``body_renderer(chat, lang)`` and returns a dict with any
    of ``text`` / ``discord`` / ``telegram_html`` / ``embed``; whatever it
    provides replaces the corresponding argument for that chat, and a chat it
    returns ``None`` for is skipped entirely (that is how per-chat filters
    drop an event without dropping the whole relay).
    """
    place_name = clean_display_name(place_name) if place_name else None
    sender_name = clean_display_name(sender_name)

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
         reply_to_message_id, forward_type, forward_name, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, str(origin_sender_id), sender_name,
         reply_to_msg_db_id,
         None if is_bot_sender else forward_type,
         None if is_bot_sender else forward_name,
         int(time.time()))
    )
    msg_id = inserted.lastrowid
    db.conn.commit()

    if targets is None:
        targets = db.get_bridge_chats(bridge_id)

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

        reply_line, reply_to_platform_message_id = _resolve_reply_for_chat(chat, lang, reply_to_msg_db_id)

        reply_link_line = None
        if (chat["platform"] == "discord"
                and reply_to_platform_message_id
                and db.get_webhooks_enabled(chat["chat_id"])):
            reply_link_line = _webhook_reply_link_line(
                chat, lang, reply_to_msg_db_id, reply_to_platform_message_id
            )

        current_text = text
        current_discord_text = discord_text or current_text
        current_telegram_html = telegram_html
        current_embed = None
        if rendered is not None:
            current_text = rendered.get("text", current_text)
            current_discord_text = rendered.get("discord", current_text)
            current_telegram_html = rendered.get("telegram_html", None)
            current_embed = rendered.get("embed")

        eff_forward_type = None if is_bot_sender else forward_type
        eff_forward_name = None if is_bot_sender else forward_name

        fwd_line = _forward_line(eff_forward_type, eff_forward_name, lang)
        if fwd_line:
            current_text = f"{fwd_line}\n{current_text}".strip()
            current_discord_text = f"{fwd_line}\n{current_discord_text}".strip()
            if current_telegram_html is not None:
                current_telegram_html = f"{escape_html(fwd_line)}\n{current_telegram_html}".strip()

        if external_reply:
            ext_line = localized_reply_external(lang)
            current_text = f"{ext_line}\n{current_text}".strip()
            current_discord_text = f"{ext_line}\n{current_discord_text}".strip()
            if current_telegram_html is not None:
                current_telegram_html = f"{escape_html(ext_line)}\n{current_telegram_html}".strip()
        if telegram_file_count is not None:
            marker = localized_file_count_text(telegram_file_count, lang)
            current_text = current_text.replace(
                f"__TG_FILES_{telegram_file_count}__",
                marker
            )
            current_discord_text = current_discord_text.replace(
                f"__TG_FILES_{telegram_file_count}__",
                marker
            )
            if current_telegram_html is not None:
                current_telegram_html = current_telegram_html.replace(
                    f"__TG_FILES_{telegram_file_count}__",
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
            chat["platform"], current_text, current_discord_text, current_telegram_html, gallery_urls
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
        sent_id = await send_to_chat_func(
            chat,
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

        if chat["platform"] == "telegram" and gallery_urls and len(gallery_urls) > 1:
            await _send_telegram_gallery_followups(
                chat, msg_id, gallery_urls[1:], send_to_chat_func,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )

    db.conn.commit()
    return msg_id

async def _send_telegram_gallery_followups(
    chat, msg_id, urls, send_to_chat_func, *, sender_name, place_name,
    messenger_name, avatar_url, is_bot_sender,
):
    """Post the remaining GALLERY links into a Telegram chat, one message per
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