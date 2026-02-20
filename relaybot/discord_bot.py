import discord
from relaybot.config import DISCORD_CHANNEL_IDS, EXTRA_BRIDGES
from relaybot.utils import format_message

def get_discord_server_name(message):
    return message.guild.name if message.guild else "Unknown Server"

def get_discord_display_name(member):
    return member.display_name if hasattr(member, "display_name") else str(member)

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

async def get_reply_to_name(message, bot_user):
    # If replying to a message, get proper reply target
    if message.reference and message.reference.resolved:
        replied = message.reference.resolved
        if hasattr(replied, "author"):
            # If replying to this bot, extract the special text
            if replied.author.id == bot_user.id and replied.content:
                extracted = extract_reply_text_from_bot_message(replied.content)
                if extracted:
                    return extracted
            return get_discord_display_name(replied.author)
    return None

def setup_discord_handlers(bot, queues, mappings):
    @bot.event
    async def on_ready():
        print(f"Discord bot logged in as {bot.user}")

    @bot.event
    async def on_message(message):
        if message.author.bot:
            return

        # Support threads + channels
        channel_id = (
            message.channel.id
            if message.channel.id in DISCORD_CHANNEL_IDS
            else (message.thread.id if getattr(message, "thread", None) and message.thread.id in DISCORD_CHANNEL_IDS else None)
        )
        if channel_id:
            username = get_discord_display_name(message.author)
            text = message.content or ""
            attachments = [a.url for a in message.attachments] if message.attachments else []
            server_name = get_discord_server_name(message)
            reply_to = await get_reply_to_name(message, bot.user)
            body = format_message("Discord", server_name, username, text, reply_to=reply_to, attachments=attachments)
            await queues.discord_to_telegram.put(((channel_id, message.id), body))

            # Crosspost to other Discord channels
            for dst_chan_id in DISCORD_CHANNEL_IDS:
                if dst_chan_id == channel_id:
                    continue
                dst_channel = bot.get_channel(dst_chan_id)
                if dst_channel:
                    try:
                        sent = await dst_channel.send(body)
                        mappings['discord_crosspost'].setdefault((channel_id, message.id), {})[dst_chan_id] = sent.id
                    except Exception as e:
                        print(f"[Discord] Crosspost error: {e}")

        # --- Additive: Extra bridge handling for Discord -> Telegram ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if message.channel.id == bridge["discord_channel_id"]:
                username = get_discord_display_name(message.author)
                text = message.content or ""
                attachments = [a.url for a in message.attachments] if message.attachments else []
                server_name = get_discord_server_name(message)
                reply_to = await get_reply_to_name(message, bot.user)
                body = format_message(
                    "Discord", server_name, username, text, reply_to=reply_to, attachments=attachments
                )
                # Instead of sending directly, put it in the bridge queue for mapping (for edits/deletes)
                await queues.bridge_discord_to_telegram.put((idx, message, body))

    @bot.event
    async def on_message_edit(before, after):
        if after.author.bot:
            return
        channel_id = (
            after.channel.id
            if after.channel.id in DISCORD_CHANNEL_IDS
            else (after.thread.id if getattr(after, "thread", None) and after.thread.id in DISCORD_CHANNEL_IDS else None)
        )
        if channel_id:
            username = get_discord_display_name(after.author)
            text = after.content or ""
            attachments = [a.url for a in after.attachments] if after.attachments else []
            server_name = get_discord_server_name(after)
            reply_to = await get_reply_to_name(after, bot.user)
            body = format_message("Discord", server_name, username, text, reply_to=reply_to, attachments=attachments)
            for key, tg_msg_id in list(mappings["discord_to_telegram"].items()):
                tg_chat_id, tg_topic_id, d_chan_id, d_msg_id = key
                if d_chan_id == channel_id and d_msg_id == after.id:
                    try:
                        await mappings["telegram_app"].bot.edit_message_text(
                            chat_id=tg_chat_id,
                            message_id=tg_msg_id,
                            text=body,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        print(f"[Discord->TG Edit] {e}")
            crossposts = mappings["discord_crosspost"].get((channel_id, after.id), {})
            for dst_chan_id, dst_msg_id in crossposts.items():
                dst_channel = bot.get_channel(dst_chan_id)
                if dst_channel:
                    try:
                        dst_msg = await dst_channel.fetch_message(dst_msg_id)
                        await dst_msg.edit(content=body)
                    except Exception as e:
                        print(f"[Discord Crosspost Edit] {e}")

        # --- Additive: Extra bridge edit handling ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if after.channel.id == bridge["discord_channel_id"]:
                username = get_discord_display_name(after.author)
                text = after.content or ""
                attachments = [a.url for a in after.attachments] if after.attachments else []
                server_name = get_discord_server_name(after)
                reply_to = await get_reply_to_name(after, bot.user)
                body = format_message("Discord", server_name, username, text, reply_to=reply_to, attachments=attachments)
                await queues.bridge_discord_edit_delete.put({
                    "action": "edit",
                    "bridge_idx": idx,
                    "discord_msg": after,
                    "body": body
                })

    @bot.event
    async def on_message_delete(message):
        channel_id = (
            message.channel.id
            if message.channel.id in DISCORD_CHANNEL_IDS
            else (message.thread.id if getattr(message, "thread", None) and message.thread.id in DISCORD_CHANNEL_IDS else None)
        )
        if channel_id:
            for key in list(mappings["discord_to_telegram"].keys()):
                tg_chat_id, tg_topic_id, d_chan_id, d_msg_id = key
                if d_chan_id == channel_id and d_msg_id == message.id:
                    telegram_msg_id = mappings["discord_to_telegram"].pop(key)
                    try:
                        await mappings["telegram_app"].bot.delete_message(
                            chat_id=tg_chat_id,
                            message_id=telegram_msg_id
                        )
                    except Exception as e:
                        print(f"[Discord->TG Delete] {e}")
            crossposts = mappings["discord_crosspost"].pop((channel_id, message.id), {})
            for dst_chan_id, dst_msg_id in crossposts.items():
                dst_channel = bot.get_channel(dst_chan_id)
                if dst_channel:
                    try:
                        dst_msg = await dst_channel.fetch_message(dst_msg_id)
                        await dst_msg.delete()
                    except Exception as e:
                        print(f"[Discord Crosspost Delete] {e}")

        # --- Additive: Extra bridge delete handling ---
        for idx, bridge in enumerate(EXTRA_BRIDGES):
            if message.channel.id == bridge["discord_channel_id"]:
                await queues.bridge_discord_edit_delete.put({
                    "action": "delete",
                    "bridge_idx": idx,
                    "discord_msg": message,
                })
