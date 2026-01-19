import db
import time

async def relay_message(
    *,
    bridge_id,
    origin_platform,
    origin_chat_id,
    origin_message_id,
    messenger_name,
    place_name,
    sender_name,
    text,
    reply_to_name=None,
    send_to_chat_func,
):
    msg_id = db.cur.execute(
        """
        INSERT INTO messages
        (bridge_id, origin_platform, origin_chat_id, origin_message_id, created_at)
        VALUES (?,?,?,?,?)
        """,
        (bridge_id,origin_platform,origin_chat_id,origin_message_id,int(time.time())
)
    ).lastrowid
    db.conn.commit()

    header = f"[{messenger_name} | {place_name}] {sender_name}:"
    if reply_to_name:
        body = f"(отвечая {reply_to_name}\n{text})"
    else:
        body = text

    full_text = f"{header}\n{body}".strip()

    targets = db.get_bridge_chats(bridge_id)

    for chat in targets:
        if chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id:
            continue

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
