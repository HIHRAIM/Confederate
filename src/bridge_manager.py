import db

def attach_chat_to_bridge(platform, chat_id, bridge_id):
    if db.chat_exists(chat_id):
        raise ValueError("chat_already_attached")

    db.attach_chat(platform, chat_id, bridge_id)


def get_targets(bridge_id, exclude_chat_id):
    chats = db.get_bridge_chats(bridge_id)
    return [
        c for c in chats
        if c["chat_id"] != exclude_chat_id
    ]
