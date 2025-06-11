from telegram.ext import MessageHandler, filters
from relaybot.utils import format_message

def get_telegram_group_title(msg):
    return msg.chat.title if hasattr(msg.chat, 'title') and msg.chat.title else str(msg.chat.id)

def get_plain_telegram_name(user):
    if hasattr(user, "full_name") and user.full_name:
        return user.full_name
    elif hasattr(user, "username") and user.username:
        return user.username
    return "unknown"

def is_repost(msg):
    return (
        (hasattr(msg, "forward_from") and msg.forward_from) or
        (hasattr(msg, "forward_from_chat") and msg.forward_from_chat)
    )

def get_repost_text(msg):
    if hasattr(msg, "forward_from") and msg.forward_from:
        fwd_user = msg.forward_from
        name = get_plain_telegram_name(fwd_user)
        return f"(переслано от {name})"
    elif hasattr(msg, "forward_from_chat") and msg.forward_from_chat:
        chat = msg.forward_from_chat
        title = chat.title if hasattr(chat, "title") and chat.title else str(chat.id)
        return f"(переслано из {title})"
    return "(переслано)"

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
    async def telegram_message_handler(update, context):
        msg = update.effective_message
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id
        from relaybot.config import TELEGRAM_TARGETS
        if not any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        ):
            return
        if msg.edit_date:
            return
        sender = get_plain_telegram_name(update.effective_user)
        text = msg.text or ""
        attachments = []

        # Only append the largest photo (last in msg.photo) if present
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
        group_title = get_telegram_group_title(msg)

        # Only show reply label if replying to a different user (not self)
        reply_to = None
        if getattr(msg, "reply_to_message", None):
            replied_msg = msg.reply_to_message
            replied_user = getattr(replied_msg, "from_user", None)
            if (
                replied_user and
                replied_user.id != msg.from_user.id
            ):
                if getattr(replied_user, "is_bot", False) and getattr(replied_msg, "text", None):
                    extracted = extract_reply_text_from_bot_message(replied_msg.text)
                    if extracted:
                        reply_to = extracted
                    else:
                        reply_to = get_plain_telegram_name(replied_user)
                else:
                    reply_to = get_plain_telegram_name(replied_user)
        # If replying to self (or not a reply), reply_to remains None

        repost_text = None
        if is_repost(msg):
            repost_text = get_repost_text(msg)

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

    async def telegram_edit_handler(update, context):
        msg = update.effective_message
        chat_id = msg.chat_id
        topic_id = getattr(msg, "message_thread_id", None)
        msg_id = msg.message_id
        from relaybot.config import TELEGRAM_TARGETS
        if not any(
            chat_id == t["chat_id"] and (t.get("topic_id") is None or t.get("topic_id") == topic_id)
            for t in TELEGRAM_TARGETS
        ) or not msg.edit_date:
            return
        sender = get_plain_telegram_name(update.effective_user)
        text = msg.text or ""
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
        group_title = get_telegram_group_title(msg)

        reply_to = None
        if getattr(msg, "reply_to_message", None):
            replied_msg = msg.reply_to_message
            replied_user = getattr(replied_msg, "from_user", None)
            if (
                replied_user and
                replied_user.id != msg.from_user.id
            ):
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
            repost_text = get_repost_text(msg)

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

    app.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED, telegram_message_handler))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, telegram_edit_handler))