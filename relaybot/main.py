import asyncio
import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main
from utils import log_error

db.init()
db.cleanup_old_messages(days=30)

async def main():
    await asyncio.gather(
        tg_main(),
        discord_bot.start(DISCORD_TOKEN),
        rules_loop(),
    )

async def rules_loop():
    import time
    from discord_bot import bot as dc
    from telegram_bot import bot as tg

    while True:
        now = int(time.time())
        rows = db.cur.execute("SELECT * FROM bridge_rules").fetchall()

        for r in rows:
            try:
                if now - r["last_post_ts"] < r["hours"] * 3600:
                    continue

                if r["messages"] is not None and r["message_counter"] < r["messages"]:
                    continue

                db.cur.execute(
                    """
                    UPDATE bridge_rules
                    SET last_post_ts=?, message_counter=0
                    WHERE bridge_id=?
                    """,
                    (now, r["bridge_id"])
                )
                db.conn.commit()

                chats = db.cur.execute(
                    "SELECT * FROM chats WHERE bridge_id=?",
                    (r["bridge_id"],)
                ).fetchall()

                for c in chats:
                    try:
                        if c["platform"] == "discord":
                            channel_id = int(c["chat_id"].split(":")[1])
                            channel = dc.get_channel(channel_id)
                            if channel:
                                await channel.send(r["content"])

                        elif c["platform"] == "telegram":
                            chat_id, thread = c["chat_id"].split(":")
                            await tg.send_message(
                                int(chat_id),
                                r["content"],
                                message_thread_id=int(thread) or None
                            )
                    except Exception as e:
                        await log_error(f"RULES SEND ERROR ({r['bridge_id']}->{c['chat_id']}): {e}")
            except Exception as e:
                await log_error(f"RULES LOOP ERROR: {e}")

        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
