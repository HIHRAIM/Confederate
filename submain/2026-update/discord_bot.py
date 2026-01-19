import discord
from discord import app_commands
import db, message_relay
from utils import is_admin, extract_username_from_bot_message

class DiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = DiscordBot()

@bot.tree.command(name="atb")
async def atb(interaction: discord.Interaction, bridge_id: int):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("Нет прав", ephemeral=True)
        return

    if db.chat_exists(chat_id):
        await interaction.response.send_message("Чат уже в мосту", ephemeral=True)
        return

    db.attach_chat("discord", chat_id, bridge_id)
    await interaction.response.send_message(f"Чат подключён к мосту {bridge_id}", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    chat_id = f"{message.guild.id}:{message.channel.id}"
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    reply_to_name = None
    if message.reference and message.reference.resolved:
        replied = message.reference.resolved
        if replied.author.bot:
            reply_to_name = extract_username_from_bot_message(replied.content)
        else:
            reply_to_name = replied.author.display_name

    attachments = [a.url for a in message.attachments]
    texts = []

    if attachments:
        texts.append((message.content or "") + "\n" + attachments[0])
        for a in attachments[1:]:
            texts.append(a)
    else:
        texts.append(message.content or "")

    async def send_to_chat(chat, text):
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if not channel:
                return None
            sent = await channel.send(text)
            return str(sent.id)

        if chat["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id_str, thread = chat["chat_id"].split(":")
            sent = await tg_bot.send_message(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text
            )
            return str(sent.message_id)

    for text in texts:
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=chat_id,
            origin_message_id=str(message.id),
            messenger_name="Discord",
            place_name=message.guild.name,
            sender_name=message.author.display_name,
            text=text,
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat
        )

@bot.event
async def on_message_delete(message: discord.Message):
    row = db.cur.execute(
        "SELECT id FROM messages WHERE origin_platform='discord' AND origin_message_id=?",
        (str(message.id),)
    ).fetchone()
    if not row:
        return

    msg_id = row["id"]
    copies = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=?",
        (msg_id,)
    ).fetchall()

    for c in copies:
        if c["platform"] == "discord":
            channel_id = int(c["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.delete_messages(
                        [discord.Object(id=int(c["message_id_platform"]))]
                    )
                except:
                    pass

    db.cur.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    db.cur.execute("DELETE FROM message_copies WHERE message_id=?", (msg_id,))
    db.conn.commit()
