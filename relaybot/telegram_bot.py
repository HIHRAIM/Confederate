from telegram.ext import MessageHandler, filters
from relaybot.utils import format_message
from relaybot.config import TELEGRAM_TARGETS, EXTRA_BRIDGES

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
    # 1. Anonymous forward (top-level)
    sender_name = getattr(msg, "forward_sender_name", None)
    if not sender_name and isinstance(msg, dict):
        sender_name = msg.get("forward_sender_name")
    if sender_name:
        return f"(переслано от {sender_name})"

    # 2. User forward (legacy)
    fwd_from = getattr(msg, "forward_from", None)
    if not fwd_from and isinstance(msg, dict):
        fwd_from = msg.get("forward_from")
    if fwd_from:
        name = None
        if isinstance(fwd_from, dict):
            name = fwd_from.get("first_name") or fwd_from.get("username")
        else:
            name = getattr(fwd_from, "first_name", None) or getattr(fwd_from, "username", None)
        if name:
            return f"(переслано от {name})"

    # 3. Channel forward (legacy)
    fwd_from_chat = getattr(msg, "forward_from_chat", None)
    if not fwd_from_chat and isinstance(msg, dict):
        fwd_from_chat = msg.get("forward_from_chat")
    if fwd_from_chat:
        title = fwd_from_chat.get("title") if isinstance(fwd_from_chat, dict) else getattr(fwd_from_chat, "title", None)
        if title:
            return f"(переслано из {title})"

    # 4. New API: forward_origin
    origin = getattr(msg, "forward_origin", None)
    if not origin and isinstance(msg, dict):
        origin = msg.get("forward_origin")
    if origin:
        if isinstance(origin, dict):
            # Channel forward
            chat = origin.get("chat")
            if chat and chat.get("title"):
                return f"(переслано из {chat['title']})"
            # User forward (new API)
            sender_user = origin.get("sender_user")
            if sender_user:
                name = sender_user.get("first_name") or sender_user.get("username")
                if name:
                    return f"(переслано от {name})"
            # User forward (hidden user)
            if origin.get("sender_user_name"):
                return f"(переслано от {origin['sender_user_name']})"
            if origin.get("sender_name"):
                return f"(переслано от {origin['sender_name']})"
        else:
            chat = getattr(origin, "chat", None)
            if chat and getattr(chat, "title", None):
                return f"(переслано из {chat.title})"
            sender_user = getattr(origin, "sender_user", None)
            if sender_user:
                name = getattr(sender_user, "first_name", None) or getattr(sender_user, "username", None)
                if name:
                    return f"(переслано от {name})"
            name = getattr(origin, "sender_user_name", None)
            if name:
                return f"(переслано от {name})"
            name = getattr(origin, "sender_name", None)
            if name:
                return f"(переслано от {name})"

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

def setup_telegram_handlers(app, queues, mappings):
    # Store bridge message mappings for edits/deletes: {(bridge_idx, tg_msg_id): discord_msg_id}
    if "bridge_telegram_to_discord" not in mappings:
        mappings["bridge_telegram_to_discord"] = {}  # (bridge_idx, tg_msg_id) -> (discord_channel_id, discord_msg_id)
    if "bridge_discord_to_telegram" not in mappings:
        mappings["bridge_discord_to_telegram"] = {}  # (bridge_idx, discord_msg_id) -> (tg_chat_id, tg_topic_id, tg_msg_id)

    async def telegram_message_handler(update, context):
        msg = update.effective_message
        print("DEBUG: msg dict:", msg.to_dict() if hasattr(msg, "to_dict") else vars(msg))
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id

        # --- Main relay logic (untouched) ---
        if not any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        ):
            pass  # don't return here! allow bridge relay too
        else:
            if not msg.edit_date:
                sender = get_plain_telegram_name(update.effective_user)
                text = msg.text or msg.caption or ""
                attachments = []
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
                group_title = get_telegram_group_title(msg)
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
                        # Optionally: suppress if replied message is the thread starter
                        # (You can add more conditions here if needed)
                    if not suppress_reply and replied_user and replied_user.id != msg.from_user.id:
                        if getattr(replied_user, "is_bot", False) and getattr(replied_msg, "text", None):
                            extracted = extract_reply_text_from_bot_message(replied_msg.text)
                            if extracted:
                                reply_to = extracted
                            else:
                                reply_to = get_plain_telegram_name(replied_user)
                        else:
                            reply_to = get_plain_telegram_name(replied_user)
                repost_text = None
                if is_repost(msg):
                    print("DEBUG: msg type:", type(msg))
                    print("DEBUG: msg as dict:", msg if isinstance(msg, dict) else (msg.to_dict() if hasattr(msg, "to_dict") else vars(msg)))
                    repost_text = get_repost_text(msg)
                if text or attachments:
                    body = format_message(
                        "Telegram",
                        group_title,
                        sender,
                        text,
                        reply_to=reply_to,
                        repost=repost_text,
                        attachments=attachments
                    )
                    await queues.telegram_to_discord.put(((chat_id, topic_id, msg_id), body))
                    await queues.telegram_to_telegram.put((chat_id, topic_id, msg_id, body))

        # --- Bridge relay logic (additive, for each bridge) ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or topic_id == bridge.get("telegram_topic_id")):
                discord_channel = mappings["discord_bot"].get_channel(bridge["discord_channel_id"])
                if discord_channel:
                    sender = get_plain_telegram_name(update.effective_user)
                    text = msg.text or msg.caption or ""
                    attachments = []
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
                    group_title = get_telegram_group_title(msg)
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
                        # Optionally: suppress if replied message is the thread starter
                        # (You can add more conditions here if needed)
                    if not suppress_reply and replied_user and replied_user.id != msg.from_user.id:
                        if getattr(replied_user, "is_bot", False) and getattr(replied_msg, "text", None):
                            extracted = extract_reply_text_from_bot_message(replied_msg.text)
                            if extracted:
                                reply_to = extracted
                            else:
                                reply_to = get_plain_telegram_name(replied_user)
                        else:
                            reply_to = get_plain_telegram_name(replied_user)
                    repost_text = None
                    if is_repost(msg):
                        print("DEBUG: msg type:", type(msg))
                        print("DEBUG: msg as dict:", msg if isinstance(msg, dict) else (msg.to_dict() if hasattr(msg, "to_dict") else vars(msg)))
                        repost_text = get_repost_text(msg)
                    if text or attachments:
                        body = format_message(
                            "Telegram",
                            group_title,
                            sender,
                            text,
                            reply_to=reply_to,
                            repost=repost_text,
                            attachments=attachments
                        )
                        sent = await discord_channel.send(body)
                        # Store mapping for future edit/delete
                        mappings["bridge_telegram_to_discord"][(idx, msg_id)] = (bridge["discord_channel_id"], sent.id)

    async def telegram_edit_handler(update, context):
        msg = update.effective_message
        print("DEBUG: msg dict:", msg.to_dict() if hasattr(msg, "to_dict") else vars(msg))
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id

        # --- Main relay logic (untouched) ---
        if any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        ) and msg.edit_date:
            sender = get_plain_telegram_name(update.effective_user)
            text = msg.text or msg.caption or ""
            attachments = []
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
            group_title = get_telegram_group_title(msg)
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
            repost_text = None
            if is_repost(msg):
                print("DEBUG: msg type:", type(msg))
                print("DEBUG: msg as dict:", msg if isinstance(msg, dict) else (msg.to_dict() if hasattr(msg, "to_dict") else vars(msg)))
                repost_text = get_repost_text(msg)
            if text or attachments:
                body = format_message(
                    "Telegram",
                    group_title,
                    sender,
                    text,
                    reply_to=reply_to,
                    repost=repost_text,
                    attachments=attachments
                )
                key = (chat_id, topic_id, msg_id)
                iscord_mapping = mappings["telegram_to_discord_map"].get(key)
                if discord_mapping:
                    for chan_id, disc_msg_id in discord_mapping:
                        channel = mappings["discord_bot"].get_channel(chan_id)
                        if channel:
                            try:
                                discord_msg = await channel.fetch_message(disc_msg_id)
                                await discord_msg.edit(content=body)
                            except Exception as e:
                                print(f"[TG->Discord Edit] {e}")

        # --- Bridge relay logic (additive, for each bridge) ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or topic_id == bridge.get("telegram_topic_id")):
                mapping = mappings["bridge_telegram_to_discord"].get((idx, msg_id))
                if mapping:
                    discord_channel_id, discord_msg_id = mapping
                    discord_channel = mappings["discord_bot"].get_channel(discord_channel_id)
                    if discord_channel:
                        sender = get_plain_telegram_name(update.effective_user)
                        text = msg.text or msg.caption or ""
                        attachments = []
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
                        group_title = get_telegram_group_title(msg)
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
                                # Optionally: suppress if replied message is the thread starter
                                # (You can add more conditions here if needed)
                            if not suppress_reply and replied_user and replied_user.id != msg.from_user.id:
                                if getattr(replied_user, "is_bot", False) and getattr(replied_msg, "text", None):
                                    extracted = extract_reply_text_from_bot_message(replied_msg.text)
                                    if extracted:
                                        reply_to = extracted
                                    else:
                                        reply_to = get_plain_telegram_name(replied_user)
                                else:
                                    reply_to = get_plain_telegram_name(replied_user)
                        repost_text = None
                        if is_repost(msg):
                            print("DEBUG: msg type:", type(msg))
                            print("DEBUG: msg as dict:", msg if isinstance(msg, dict) else (msg.to_dict() if hasattr(msg, "to_dict") else vars(msg)))
                            repost_text = get_repost_text(msg)
                        if text or attachments:
                            body = format_message(
                                "Telegram",
                                group_title,
                                sender,
                                text,
                                reply_to=reply_to,
                                repost=repost_text,
                                attachments=attachments
                            )
                            try:
                                discord_msg = await discord_channel.fetch_message(discord_msg_id)
                                await discord_msg.edit(content=body)
                            except Exception as e:
                                print(f"[TG->Discord Bridge Edit] {e}")

    async def telegram_delete_handler(update, context):
        msg = update.effective_message
        print("DEBUG: msg dict:", msg.to_dict() if hasattr(msg, "to_dict") else vars(msg))
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id

        # --- Main relay logic (untouched) ---
        # No deletion for main relay here, handled elsewhere if needed

        # --- Bridge relay logic (additive, for each bridge) ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if chat_id == bridge["telegram_chat_id"] and (bridge.get("telegram_topic_id") is None or topic_id == bridge.get("telegram_topic_id")):
                mapping = mappings["bridge_telegram_to_discord"].get((idx, msg_id))
                if mapping:
                    discord_channel_id, discord_msg_id = mapping
                    discord_channel = mappings["discord_bot"].get_channel(discord_channel_id)
                    if discord_channel:
                        try:
                            discord_msg = await discord_channel.fetch_message(discord_msg_id)
                            await discord_msg.delete()
                        except Exception as e:
                            print(f"[TG->Discord Bridge Delete] {e}")

    app.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED, telegram_message_handler))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, telegram_edit_handler))
    # Telegram delete event: handled via update.message if available (may depend on library and bot permissions)
    # For aiogram/pyTelegramBotAPI, etc., would need to add custom handler for deletes if possible
    # For python-telegram-bot v20+, MessageHandler does not support message deletes directly.
    # If you use a dispatcher that supports it, add here:
    # app.add_handler(MessageHandler(filters.UpdateType.DELETED, telegram_delete_handler))
