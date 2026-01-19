import asyncio
import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main

db.init()
db.cleanup_old_messages(days=30)

async def main():
    await asyncio.gather(
        tg_main(),
        discord_bot.start(DISCORD_TOKEN)
    )

asyncio.run(main())
