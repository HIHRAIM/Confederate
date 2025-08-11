import discord
from discord.ext import commands
from datetime import datetime, timezone
import io
import asyncio

TOKEN = "MTI5NTQ1NDgyOTg4MzI5ODAyMw.GkcsXI.s7R5y6eRoc3QR-8pi4Fe3t_bDIRt1tWAg-OFMs"
SOURCE_CHANNEL_ID = 823652976312188938 # 871676989604528228
DESTINATION_CHANNEL_ID = 1404469187543437383

DATE_FROM = datetime(2020, 9, 16, 0, 0, 0, tzinfo=timezone.utc)
DATE_TO = datetime(2025, 8, 11, 23, 59, 59, tzinfo=timezone.utc)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    source_channel = bot.get_channel(SOURCE_CHANNEL_ID)
    dest_channel = bot.get_channel(DESTINATION_CHANNEL_ID)
    if not source_channel or not dest_channel:
        print("ERROR: One or both channels not found!")
        await bot.close()
        return

    print("Starting migration...")
    async for message in source_channel.history(limit=None, oldest_first=True):
        if not (DATE_FROM <= message.created_at <= DATE_TO):
            continue
        if message.author == bot.user:
            continue

        files = []
        for attachment in message.attachments:
            data = await attachment.read()
            file = discord.File(fp=io.BytesIO(data), filename=attachment.filename)
            files.append(file)

        # Проверяем, есть ли ответ на сообщение
        reply_part = ""
        if message.reference and message.reference.message_id:
            try:
                replied_msg = await source_channel.fetch_message(message.reference.message_id)
                reply_author_name = replied_msg.author.display_name
                reply_part = f" (отвечая {reply_author_name})"
            except Exception:
                reply_part = ""

        timestamp = int(message.created_at.timestamp())
        content = f"[<t:{timestamp}:d>] {message.author.display_name}{reply_part}:\n{message.content or ''}"

        try:
            await dest_channel.send(content=content, files=files, allowed_mentions=discord.AllowedMentions.none())
            print(f"Migrated message {message.id} ({message.created_at})")
        except Exception as e:
            print(f"Failed to migrate message {message.id}: {e}")
        await asyncio.sleep(1)

    print("Migration finished.")
    await bot.close()

if __name__ == "__main__":
    bot.run(TOKEN)
