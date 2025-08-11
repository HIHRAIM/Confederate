import discord
from discord.ext import commands
from datetime import datetime, timezone
import io
import asyncio

TOKEN = "MTI5NTQ1NDgyOTg4MzI5ODAyMw.GkcsXI.s7R5y6eRoc3QR-8pi4Fe3t_bDIRt1tWAg-OFMs"
SOURCE_CHANNEL_ID = 1020377084457123880
DESTINATION_CHANNEL_ID = 1404226192747397221

DATE_FROM = datetime(2022, 9, 16, 0, 0, 0, tzinfo=timezone.utc)
DATE_TO = datetime(2025, 8, 11, 23, 59, 59, tzinfo=timezone.utc)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def get_or_create_webhook(channel: discord.TextChannel):
    webhooks = await channel.webhooks()
    for webhook in webhooks:
        if webhook.user == channel.guild.me:
            return webhook
    return await channel.create_webhook(name="MigrationWebhook")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    source_channel = bot.get_channel(SOURCE_CHANNEL_ID)
    dest_channel = bot.get_channel(DESTINATION_CHANNEL_ID)
    if not source_channel or not dest_channel:
        print("ERROR: One or both channels not found!")
        await bot.close()
        return

    webhook = await get_or_create_webhook(dest_channel)
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

        ts = message.created_at.astimezone(timezone.utc).strftime("[%d-%m-%Y %H:%M] ")
        content = f"{ts}{message.content or ''}"

        try:
            await webhook.send(
                content=content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url if message.author.display_avatar else None,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            print(f"Migrated message {message.id} ({message.created_at})")
        except Exception as e:
            print(f"Failed to migrate message {message.id}: {e}")
        await asyncio.sleep(1)

    print("Migration finished.")
    await bot.close()

if __name__ == "__main__":
    bot.run(TOKEN)