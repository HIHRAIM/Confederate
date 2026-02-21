import db
import time
from utils import get_chat_lang, localized_replying

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
    reply_to_name=None,
    send_to_chat_func,
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

        if reply_to_name:
            body = f"{localized_replying(reply_to_name, lang)}\n{text}"
        else:
            body = text

        full_text = f"{header}\n{body}".strip()

        sent_id = await send_to_chat_func(chat, full_text)
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
