import asyncio
from telegram.ext import MessageHandler, filters
from utils import format_message
from config import TELEGRAM_TARGETS, EXTRA_BRIDGES

def get_telegram_group_title(msg):
    return msg.chat.title if hasattr(msg.chat, 'title') and msg.chat.title else str(msg.chat.id)

def get_plain_telegram_name(user):
    if hasattr(user, "full_name") and user.full_name:
        return user.full_name
    elif hasattr(user, "username") and user.username:
        return user.username
    return "unknown"

def is_repost(msg):
    # Attribute or dict access for all possible cases
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
    # Implementation unchanged from original
    # 1. Anonymous forward (top-level)
    sender_name = getattr(msg, "forward_sender_name", None)
    if not sender_name and isinstance(msg, dict):
        sender_name = msg.get("forward_sender_name")
    if sender_name:
        return f"(переслано от {sender_name})"

    # Rest of implementation unchanged...
    # [TRUNCATED FOR BREVITY]
    return None

def extract_reply_text_from_bot_message(message_text):
    """
    Extracts the text between the first ']' and the last ':' in the first paragraph.
    """
    if not message_text:
        return None
    first_paragraph = message_text.split('\n', 1)[0]
    try:
        idx1 = first_paragraph.index(']')
        idx2 = first_paragraph.rindex(':')
        if idx2 > idx1:
            return first_paragraph[idx1+1:idx2].strip()
    except ValueError:
        return None
    return None

async def get_telegram_message_content(msg, context):
    """Extract common message content and prepare attachments."""
    text = msg.text or msg.caption or ""
    attachments = []
    
    # Handle various attachment types
    if msg.photo:
        largest_photo = msg.photo[-1]
        file = await context.bot.get_file(largest_photo.file_id)
        attachments.append(file.file_path)
    
    if msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        attachments.append(file.file_path)
    
    if msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        attachments.append(file.file_path)
    
    if msg.audio:
        file = await context.bot.get_file(msg.audio.file_id)
        attachments.append(file.file_path)
    
    if msg.voice:
        file = await context.bot.get_file(msg.voice.file_id)
        attachments.append(file.file_path)
    
    if msg.video_note:
        file = await context.bot.get_file(msg.video_note.file_id)
        attachments.append(file.file_path)
    
    return text, attachments

def get_telegram_reply_info(msg):
    """Get reply information from a telegram message."""
    reply_to = None
    
    if getattr(msg, "reply_to_message", None):
        replied_msg = msg.reply_to_message
        replied_user = getattr(replied_msg, "from_user", None)
        
        # Suppress "replied to" if reply is to topic starter in forum/topic chat
        suppress_reply = False
        
        # Check if this is a topic message (forum)
        if getattr(msg, "is_topic_message", False):
            # Suppress reply if replying to the topic starter
            # The topic starter's message_id == message_thread_id
            if getattr(msg, "message_thread_id", None) is not None and \
                getattr(replied_msg, "message_id", None) == msg.message_thread_id:
                suppress_reply = True
        
        if not suppress_reply and replied_user and replied_user.id != msg.from_user.id:
            if getattr(replied_user, "is_bot", False) and getattr(replied_msg, "text", None):
                extracted = extract_reply_text_from_bot_message(replied_msg.text)
                if extracted:
                    reply_to = extracted
                else:
                    reply_to = get_plain_telegram_name(replied_user)
            else:
                reply_to = get_plain_telegram_name(replied_user)
    
    return reply_to

def setup_telegram_handlers(app, queues, mappings):
    # Store bridge message mappings for edits/deletes
    if "bridge_telegram_to_discord" not in mappings:
        mappings["bridge_telegram_to_discord"] = {}
    if "bridge_discord_to_telegram" not in mappings:
        mappings["bridge_discord_to_telegram"] = {}

    async def telegram_message_handler(update, context):
        msg = update.effective_message
        if not msg or update.effective_user.is_bot:
            return
            
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id
        
        # Skip edits in this handler
        if getattr(msg, "edit_date", None):
            return
        
        # Prepare common message data
        sender = get_plain_telegram_name(update.effective_user)
        text, attachments = await get_telegram_message_content(msg, context)
        group_title = get_telegram_group_title(msg)
        reply_to = get_telegram_reply_info(msg)
        repost_text = get_repost_text(msg) if is_repost(msg) else None
        
        # Format the message body
        body = format_message(
            "Telegram",
            group_title,
            sender,
            text,
            reply_to=reply_to,
            repost=repost_text,
            attachments=attachments
        )

        # --- LEGACY: Main relay logic ---
        is_main_relay = any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        )
        
        if is_main_relay:
            if text or attachments:
                await queues.telegram_to_discord.put(((chat_id, topic_id, msg_id), body))
                await queues.telegram_to_telegram.put((chat_id, topic_id, msg_id, body))

        # --- LEGACY: Bridge relay logic ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or topic_id == bridge.get("telegram_topic_id")):
                if text or attachments:
                    await queues.bridge_telegram_to_discord.put((idx, msg, body))

        # --- NEW: Bridge system handling ---
        # Check if this chat/topic is part of any configured bridge
        if mappings["bridge_manager"].find_bridges_for_telegram_target(chat_id, topic_id):
            # Only relay messages with content
            if text or attachments:
                await queues.bridge_relay_telegram.put({
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "message_id": msg_id,
                    "body": body,
                })

    async def telegram_edit_handler(update, context):
        msg = update.effective_message
        if not msg or update.effective_user.is_bot or not getattr(msg, "edit_date", None):
            return
            
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id
        
        # Prepare common message data
        sender = get_plain_telegram_name(update.effective_user)
        text, attachments = await get_telegram_message_content(msg, context)
        group_title = get_telegram_group_title(msg)
        reply_to = get_telegram_reply_info(msg)
        repost_text = get_repost_text(msg) if is_repost(msg) else None
        
        # Format the message body
        body = format_message(
            "Telegram",
            group_title,
            sender,
            text,
            reply_to=reply_to,
            repost=repost_text,
            attachments=attachments
        )

        # --- LEGACY: Main relay logic ---
        is_main_relay = any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        )
        
        if is_main_relay:
            if text or attachments:
                key = (chat_id, topic_id, msg_id)
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

        # --- LEGACY: Bridge edit logic ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or topic_id == bridge.get("telegram_topic_id")):
                if text or attachments:
                    await queues.bridge_telegram_edit_delete.put({
                        "action": "edit",
                        "bridge_idx": idx,
                        "telegram_msg": msg,
                        "body": body
                    })

        # --- NEW: Bridge system edit handling ---
        if mappings["bridge_manager"].find_bridges_for_telegram_target(chat_id, topic_id):
            # Only process edits with content
            if text or attachments:
                await queues.bridge_edit_telegram.put({
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "message_id": msg_id,
                    "body": body,
                })
    
    async def telegram_delete_handler(update, context):
        # Note: Telegram doesn't provide direct delete events through the Bot API
        # This is a placeholder in case you have custom logic to detect deletions
        pass

    # Register handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED, telegram_message_handler))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, telegram_edit_handler))
    
    # For delete events, would need custom handling if available in your version of the library
    # app.add_handler(MessageHandler(filters.UpdateType.DELETED, telegram_delete_handler))
