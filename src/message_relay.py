import db
import time
import html
import re

def _utf16_index_map(text: str):
    pos_map = {0: 0}
    utf16_pos = 0
    for i, ch in enumerate(text):
        utf16_pos += len(ch.encode("utf-16-le")) // 2
        pos_map[utf16_pos] = i + 1
    return pos_map

def _wrap_blockquote(segment: str) -> str:
    lines = segment.splitlines() or [segment]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)

def telegram_entities_to_discord(text: str, entities):
    if not text:
        return ""
    if not entities:
        return text

    pos_map = _utf16_index_map(text)
    opens = {}
    closes = {}

    def add_open(i, token):
        opens.setdefault(i, []).append(token)

    def add_close(i, token):
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
    if not text:
        return ""

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
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<u>\1</u>", escaped)
    escaped = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"\|\|([^|\n]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)

    lines = escaped.splitlines()
    converted = []
    for ln in lines:
        if ln.startswith("&gt; "):
            converted.append(f"<blockquote>{ln[5:]}</blockquote>")
        else:
            converted.append(ln)
    return "\n".join(converted)

def escape_html(text: str):
    return html.escape(text or "")

from utils import (
    get_chat_lang,
    localized_replying,
    localized_file_count_text,
    localized_forward_from_chat,
    localized_forward_from_user,
    localized_forward_unknown,
)

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
    reply_to_name=None,
    send_to_chat_func,
    telegram_file_count=None,
    forward_type=None,
    forward_name=None,
):
    db.cur.execute(
        """
        UPDATE bridge_rules
        SET message_counter = message_counter + 1
        WHERE bridge_id=?
        """,
        (bridge_id,)
    )
    db.conn.commit()

    db.cur.execute(
        """
        INSERT INTO messages
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, origin_sender_id, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, str(origin_sender_id), int(time.time()))
    )
    msg_id = db.cur.lastrowid
    db.conn.commit()

    targets = db.get_bridge_chats(bridge_id)

    for chat in targets:
        if chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id:
            continue

        lang = get_chat_lang(chat["chat_id"])

        header = f"[{messenger_name} | {place_name}] {sender_name}:"

        reply_line = localized_replying(reply_to_name, lang) if reply_to_name else None

        current_text = text
        current_discord_text = discord_text or current_text

        if forward_type == "chat":
            fwd_line = localized_forward_from_chat(forward_name or "unknown", lang)
            current_text = f"{fwd_line}\n{current_text}".strip()
            current_discord_text = f"{fwd_line}\n{current_discord_text}".strip()
        elif forward_type == "user":
            fwd_line = localized_forward_from_user(forward_name or "unknown", lang)
            current_text = f"{fwd_line}\n{current_text}".strip()
            current_discord_text = f"{fwd_line}\n{current_discord_text}".strip()
        elif forward_type == "unknown":
            fwd_line = localized_forward_unknown(lang)
            current_text = f"{fwd_line}\n{current_text}".strip()
            current_discord_text = f"{fwd_line}\n{current_discord_text}".strip()
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

        sent_id = await send_to_chat_func(
            chat,
            header=header,
            body_plain=current_text,
            body_discord=current_discord_text,
            body_telegram_html=telegram_html,
            reply_line=reply_line,
        )
        if not sent_id:
            continue

        db.cur.execute(
            """
            INSERT INTO message_copies
            (message_id, platform, chat_id, message_id_platform)
            VALUES (?,?,?,?)
            """,
            (msg_id, chat["platform"], chat["chat_id"], sent_id)
        )

    db.conn.commit()

try:
    cur.execute("ALTER TABLE messages ADD COLUMN origin_sender_id TEXT")
    conn.commit()
except Exception:
    pass
