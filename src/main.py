import asyncio
import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main
from utils import log_error

db.init()
db.cleanup_old_messages(days=30)

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

async def pending_cleanup_loop():
    """
    Удаляет устаревшие pending_consents (старше 24ч): удаляет бот-сообщение и строку в БД.
    Также очищает verified_users, у которых истёк срок.
    """
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    while True:
        try:
            rows = db.get_expired_pending_consents(older_than_seconds=24*3600)
            for p in rows:
                platform = p["platform"]
                bot_msg_id = p["bot_message_id"]
                chat_key = p["chat_key"]
                prefix = p["prefix"]
                user_id = p["user_id"]
                try:
                    if platform == "telegram":
                        chat_id_str, th = chat_key.split(":")
                        await tg.delete_message(int(chat_id_str), int(bot_msg_id))
                    elif platform == "discord":
                        guild_id, channel_id = chat_key.split(":")
                        ch = dc.get_channel(int(channel_id))
                        if ch:
                            try:
                                msg = await ch.fetch_message(int(bot_msg_id))
                                await msg.delete()
                            except Exception:
                                pass
                except Exception:
                    pass
                db.remove_pending_consent(platform, prefix, user_id)

            db.cleanup_expired_verified()

        except Exception as e:
            try:
                await log_error(f"pending_cleanup_loop error: {e}")
            except Exception:
                pass

        await asyncio.sleep(60)

async def daily_check_loop():
    """
    Сразу при старте проверяет все чаты, затем спит 24 часа.
    Сообщения об ошибках отправляет в Discord-канал 1202887165110124554.
    """
    from telegram_bot import bot as tg
    from discord_bot import bot as dc
    discord_alert_channel_id = 1202887165110124554

    async def send_alert(text):
        try:
            ch = dc.get_channel(discord_alert_channel_id)
            if ch:
                await ch.send(text)
        except Exception:
            print("Failed to send alert:", text)

    await asyncio.sleep(0)
    while True:
        try:
            rows = db.cur.execute("SELECT * FROM chats").fetchall()
            for r in rows:
                platform = r["platform"]
                chat_key = r["chat_id"]
                if platform == "telegram":
                    try:
                        prefix = chat_key.split(":",1)[0]
                        try:
                            ch = await tg.get_chat(int(prefix))
                        except Exception:
                            await send_alert(f"Не вижу чат {prefix} в Telegram (chat_id={chat_key}).")
                            continue
                        try:
                            me = await tg.get_me()
                            mem = await tg.get_chat_member(int(prefix), me.id)
                            can_delete = getattr(mem, "can_delete_messages", False)
                            if not can_delete:
                                await send_alert(f"У меня не хватает прав удалять сообщения в Telegram чате {chat_key}.")
                        except Exception:
                            await send_alert(f"Error checking permissions in Telegram chat {chat_key}.")
                    except Exception:
                        continue

                elif platform == "discord":
                    try:
                        guild_id, channel_id = chat_key.split(":")
                        ch = dc.get_channel(int(channel_id))
                        if not ch:
                            await send_alert(f"Не вижу канал {channel_id} на сервере {guild_id}.")
                            continue
                        perms = ch.permissions_for(dc.user)
                        if not perms.manage_messages:
                            await send_alert(f"У меня не хватает прав в канале {channel_id} на сервере/группе {guild_id}.")
                    except Exception:
                        continue
        except Exception as e:
            try:
                await send_alert(f"daily_check_loop error: {e}")
            except Exception:
                print("daily_check_loop exception:", e)

        await asyncio.sleep(24 * 3600)

async def main():
    await asyncio.gather(
        tg_main(),
        discord_bot.start(DISCORD_TOKEN),
        rules_loop(),
        pending_cleanup_loop(),
        daily_check_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
