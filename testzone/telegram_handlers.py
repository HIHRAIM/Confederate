from telegram.ext import MessageHandler, filters
from relaybot.utils import format_message
from relaybot.config import RELAY_GROUPS, EXTRA_BRIDGES

def get_telegram_group_title(msg):
    return msg.chat.title if hasattr(msg.chat, 'title') and msg.chat.title else str(msg.chat.id)

def get_plain_telegram_name(user):
    if hasattr(user, "full_name") and user.full_name:
        return user.full_name
    elif hasattr(user, "username") and user.username:
        return user.username
    return "unknown"

def is_repost(msg):
    return any([
        getattr(msg, "forward_sender_name", None),
        getattr(msg, "forward_from_chat", None),
        getattr(msg, "forward_origin", None),
        isinstance(msg, dict) and (
            msg.get("forward_sender_name")
            or msg.get("forward_from_chat")
            or msg.get("forward_origin")
        )
    ])

def get_repost_text(msg):
    sender_name = getattr(msg, "forward_sender_name", None) or (isinstance(msg, dict) and msg.get("forward_sender_name"))
    if sender_name: return f"(переслано от {sender_name})"
    
    fwd_from = getattr(msg, "forward_from", None) or (isinstance(msg, dict) and msg.get("forward_from"))
    if fwd_from:
        name = (fwd_from.get("first_name") or fwd_from.get("username")) if isinstance(fwd_from, dict) else (getattr(fwd_from, "first_name", None) or getattr(fwd_from, "username", None))
        if name: return f"(переслано от {name})"

    fwd_from_chat = getattr(msg, "forward_from_chat", None) or (isinstance(msg, dict) and msg.get("forward_from_chat"))
    if fwd_from_chat:
        title = fwd_from_chat.get("title") if isinstance(fwd_from_chat, dict) else getattr(fwd_from_chat, "title", None)
        if title: return f"(переслано из {title})"

    origin = getattr(msg, "forward_origin", None) or (isinstance(msg, dict) and msg.get("forward_origin"))
    if origin:
        if isinstance(origin, dict):
            chat = origin.get("chat")
            if chat and chat.get("title"): return f"(переслано из {chat['title']})"
            sender_user = origin.get("sender_user")
            if sender_user:
                name = sender_user.get("first_name") or sender_user.get("username")
                if name: return f"(переслано от {name})"
            if origin.get("sender_user_name"): return f"(переслано от {origin['sender_user_name']})"
            if origin.get("sender_name"): return f"(переслано от {origin['sender_name']})"
        else:
            chat = getattr(origin, "chat", None)
            if chat and getattr(chat, "title", None): return f"(переслано из {chat.title})"
            sender_user = getattr(origin, "sender_user", None)
            if sender_user:
                name = getattr(sender_user, "first_name", None) or getattr(sender_user, "username", None)
                if name: return f"(переслано от {name})"
            name = getattr(origin, "sender_user_name", None) or getattr(origin, "sender_name", None)
            if name: return f"(переслано от {name})"
    return None

def extract_reply_text_from_bot_message(message_text):
    if not message_text: return None
    first_paragraph = message_text.split('\n', 1)[0]
    try:
        idx1 = first_paragraph.index(']')
        idx2 = first_paragraph.rindex(':')
        if idx2 > idx1: return first_paragraph[idx1+1:idx2].strip()
    except ValueError: return None
    return None

def find_relay_group_for_telegram(chat_id, topic_id):
    """Находит группу ретрансляции для данного ID чата и темы Telegram."""
    for i, group in enumerate(RELAY_GROUPS):
        for target in group["telegram_targets"]:
            if target["chat_id"] == chat_id and (target.get("topic_id") is None or target.get("topic_id") == topic_id):
                return i, group
    return None, None

async def process_telegram_message(update, context, queues):
    msg = update.effective_message
    chat_id = msg.chat_id
    topic_id = getattr(msg, "message_thread_id", None)
    
    group_idx, group = find_relay_group_for_telegram(chat_id, topic_id)

    if group:
        sender = get_plain_telegram_name(update.effective_user)
        text = msg.text or msg.caption or ""
        attachments = []
        if msg.photo: attachments.append((await context.bot.get_file(msg.photo[-1].file_id)).file_path)
        if msg.document: attachments.append((await context.bot.get_file(msg.document.file_id)).file_path)
        if msg.video: attachments.append((await context.bot.get_file(msg.video.file_id)).file_path)
        if msg.audio: attachments.append((await context.bot.get_file(msg.audio.file_id)).file_path)
        if msg.voice: attachments.append((await context.bot.get_file(msg.voice.file_id)).file_path)
        if msg.video_note: attachments.append((await context.bot.get_file(msg.video_note.file_id)).file_path)
        
        group_title = get_telegram_group_title(msg)
        reply_to = None
        if getattr(msg, "reply_to_message", None):
            replied_msg = msg.reply_to_message
            if (not getattr(msg, "is_topic_message", False) or getattr(replied_msg, "message_id", None) != msg.message_thread_id) and getattr(replied_msg, "from_user", None) and replied_msg.from_user.id != msg.from_user.id:
                if getattr(replied_msg.from_user, "is_bot", False) and getattr(replied_msg, "text", None):
                    reply_to = extract_reply_text_from_bot_message(replied_msg.text) or get_plain_telegram_name(replied_msg.from_user)
                else:
                    reply_to = get_plain_telegram_name(replied_msg.from_user)
        
        repost_text = get_repost_text(msg) if is_repost(msg) else None
        
        if text or attachments:
            body = format_message("Telegram", group_title, sender, text, reply_to=reply_to, repost=repost_text, attachments=attachments)
            return group_idx, body
    return None, None

def setup_telegram_handlers(app, queues, mappings):
    if "bridge_telegram_to_discord" not in mappings: mappings["bridge_telegram_to_discord"] = {}
    if "bridge_discord_to_telegram" not in mappings: mappings["bridge_discord_to_telegram"] = {}

    async def telegram_message_handler(update, context):
        msg = update.effective_message
        
        group_idx, body = await process_telegram_message(update, context, queues)
        if group_idx is not None and body:
            await queues.telegram_to_discord.put(((group_idx, msg.chat_id, getattr(msg, "message_thread_id", None), msg.message_id), body))
            await queues.telegram_to_telegram.put((group_idx, msg.chat_id, getattr(msg, "message_thread_id", None), msg.message_id, body))

        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if msg.chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or getattr(msg, "message_thread_id", None) == bridge.get("telegram_topic_id")):
                sender = get_plain_telegram_name(update.effective_user)
                text = msg.text or msg.caption or ""
                if text or msg.photo or msg.document:
                    body = format_message("Telegram", get_telegram_group_title(msg), sender, text)
                    await queues.bridge_telegram_to_discord.put((idx, msg, body))
                break

    async def telegram_edit_handler(update, context):
        msg = update.effective_message
        group_idx, body = await process_telegram_message(update, context, queues)
        
        if group_idx is not None and body:
            key = (group_idx, msg.chat_id, getattr(msg, "message_thread_id", None), msg.message_id)
            discord_mapping = mappings["telegram_to_discord_map"].get(key)
            if discord_mapping:
                for chan_id, disc_msg_id in discord_mapping:
                    channel = mappings["discord_bot"].get_channel(chan_id)
                    if channel:
                        try:
                            discord_msg = await channel.fetch_message(disc_msg_id)
                            await discord_msg.edit(content=body)
                        except Exception as e:
                            print(f"[TG->Discord Edit] {e}")

        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if msg.chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or getattr(msg, "message_thread_id", None) == bridge.get("telegram_topic_id")):
                mapping = mappings["bridge_telegram_to_discord"].get((idx, msg.message_id))
                if mapping:
                    pass
                break
    
    app.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED_MESSAGE, telegram_message_handler))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, telegram_edit_handler))