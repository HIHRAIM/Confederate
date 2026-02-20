import asyncio
from relaybot.utils import format_message

class ExtraBridge:
    def __init__(self, bridge_config):
        self.discord_channel_id = bridge_config["discord_channel_id"]
        self.telegram_chat_id = bridge_config["telegram_chat_id"]
        self.telegram_topic_id = bridge_config.get("telegram_topic_id")
        self.d2t_map = {}  # Discord msg_id -> Telegram msg_id
        self.t2d_map = {}  # Telegram msg_id -> Discord msg_id
        self.discord_to_telegram = asyncio.Queue()
        self.telegram_to_discord = asyncio.Queue()

    async def discord_to_telegram_worker(self, discord_bot, telegram_app):
        while True:
            message, author, server_name, text, reply_to, repost, attachments = await self.discord_to_telegram.get()
            body = format_message("Discord", server_name, author, text, reply_to=reply_to, repost=repost, attachments=attachments)
            sent = await telegram_app.bot.send_message(
                chat_id=self.telegram_chat_id,
                text=body,
                message_thread_id=self.telegram_topic_id
            )
            self.d2t_map[message.id] = sent.message_id
            self.t2d_map[sent.message_id] = message.id

    async def telegram_to_discord_worker(self, discord_bot, telegram_app):
        while True:
            msg, author, group_title, text, reply_to, repost, attachments = await self.telegram_to_discord.get()
            channel = discord_bot.get_channel(self.discord_channel_id)
            body = format_message("Telegram", group_title, author, text, reply_to=reply_to, repost=repost, attachments=attachments)
            sent = await channel.send(body)
            self.t2d_map[msg.message_id] = sent.id
            self.d2t_map[sent.id] = msg.message_id

    async def edit_discord_to_telegram(self, after, author, server_name, text, reply_to, repost, attachments, telegram_app):
        if after.id in self.d2t_map:
            tg_msg_id = self.d2t_map[after.id]
            body = format_message("Discord", server_name, author, text, reply_to=reply_to, repost=repost, attachments=attachments)
            await telegram_app.bot.edit_message_text(
                chat_id=self.telegram_chat_id,
                message_id=tg_msg_id,
                text=body,
                parse_mode="HTML"
            )

    async def edit_telegram_to_discord(self, msg, author, group_title, text, reply_to, repost, attachments, discord_bot):
        if msg.message_id in self.t2d_map:
            dc_msg_id = self.t2d_map[msg.message_id]
            channel = discord_bot.get_channel(self.discord_channel_id)
            try:
                discord_msg = await channel.fetch_message(dc_msg_id)
                body = format_message("Telegram", group_title, author, text, reply_to=reply_to, repost=repost, attachments=attachments)
                await discord_msg.edit(content=body)
            except Exception as e:
                print(f"[EXTRA BRIDGE Edit TG->DC] {e}")

    async def delete_discord_to_telegram(self, message, telegram_app):
        if message.id in self.d2t_map:
            tg_msg_id = self.d2t_map.pop(message.id)
            try:
                await telegram_app.bot.delete_message(
                    chat_id=self.telegram_chat_id,
                    message_id=tg_msg_id
                )
            except Exception as e:
                print(f"[EXTRA BRIDGE Delete DC->TG] {e}")

    async def delete_telegram_to_discord(self, msg, discord_bot):
        if msg.message_id in self.t2d_map:
            dc_msg_id = self.t2d_map.pop(msg.message_id)
            channel = discord_bot.get_channel(self.discord_channel_id)
            try:
                discord_msg = await channel.fetch_message(dc_msg_id)
                await discord_msg.delete()
            except Exception as e:
                print(f"[EXTRA BRIDGE Delete TG->DC] {e}")
