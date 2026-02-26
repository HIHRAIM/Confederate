import asyncio
import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main
from utils import get_chat_lang, localized_service_event
from config import SERVICE_CHATS

db.init()
db.cleanup_old_messages(days=30)

def _normalize_service_chat_key(platform, raw_key):
    key = str(raw_key).strip()
    if not key:
        return None, None

    if ":" in key:
        left, right = key.split(":", 1)
        try:
            return int(left), int(right)
        except Exception:
            return None, None

    try:
        single = int(key)
    except Exception:
        return None, None

    if platform == "telegram":
        return single, 0
    if platform == "discord":
        return None, single
    return None, None

async def send_service_event(event_key, **kwargs):
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    for chat_key in SERVICE_CHATS.get("telegram", set()):
        try:
            chat_id, thread = _normalize_service_chat_key("telegram", chat_key)
            if chat_id is None:
                continue
            lang = get_chat_lang(f"{chat_id}:{thread}")
            text = localized_service_event(event_key, lang, **kwargs)
            await tg.send_message(
                int(chat_id),
                text,
                message_thread_id=int(thread) or None
            )
        except Exception:
            pass

    for chat_key in SERVICE_CHATS.get("discord", set()):
        try:
            guild_id, channel_id = _normalize_service_chat_key("discord", chat_key)
            if channel_id is None:
                continue
            ch = dc.get_channel(channel_id)
            if not ch:
                try:
                    ch = await dc.fetch_channel(channel_id)
                except Exception:
                    ch = None
            effective_guild_id = guild_id
            if effective_guild_id is None and ch and getattr(ch, "guild", None):
                effective_guild_id = ch.guild.id
            lang_key = f"{effective_guild_id}:{channel_id}" if effective_guild_id is not None else str(channel_id)
            lang = get_chat_lang(lang_key)
            text = localized_service_event(event_key, lang, **kwargs)
            if ch:
                await ch.send(text)
        except Exception:
            pass

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
                            if not channel:
                                try:
                                    channel = await dc.fetch_channel(channel_id)
                                except Exception:
                                    channel = None
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
                        await send_service_event("daily_loop_error", error=f"RULES SEND ERROR ({r['bridge_id']}->{c['chat_id']}): {e}")
            except Exception as e:
                await send_service_event("daily_loop_error", error=f"RULES LOOP ERROR: {e}")

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
                await send_service_event("daily_loop_error", error=f"pending_cleanup_loop error: {e}")
            except Exception:
                pass

        await asyncio.sleep(60)

async def daily_check_loop():
    """
    Сразу при старте проверяет все чаты, затем спит 24 часа.
    Сообщения отправляет в service-чаты из config.SERVICE_CHATS.
    """
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

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
                            await send_service_event("daily_missing_tg_chat", chat_key=chat_key)
                            continue
                        try:
                            me = await tg.get_me()
                            mem = await tg.get_chat_member(int(prefix), me.id)
                            can_delete = getattr(mem, "can_delete_messages", False)
                            if not can_delete:
                                await send_service_event("daily_no_tg_delete_perm", chat_key=chat_key)
                        except Exception:
                            await send_service_event("daily_tg_perm_check_error", chat_key=chat_key, error="unknown")
                    except Exception:
                        continue

                elif platform == "discord":
                    try:
                        guild_id, channel_id = chat_key.split(":")
                        ch = dc.get_channel(int(channel_id))
                        if not ch:
                            try:
                                ch = await dc.fetch_channel(int(channel_id))
                            except Exception:
                                ch = None
                        if not ch:
                            await send_service_event("daily_missing_dc_channel", chat_key=chat_key)
                            continue
                        perms = ch.permissions_for(dc.user)
                        if not perms.manage_messages:
                            await send_service_event("daily_no_dc_manage_perm", chat_key=chat_key)
                    except Exception:
                        continue
        except Exception as e:
            try:
                await send_service_event("daily_loop_error", error=str(e))
            except Exception:
                print("daily_check_loop exception:", e)

        await asyncio.sleep(24 * 3600)

async def main():
    tasks = [
        asyncio.create_task(tg_main()),
        asyncio.create_task(discord_bot.start(DISCORD_TOKEN)),
        asyncio.create_task(rules_loop()),
        asyncio.create_task(pending_cleanup_loop()),
        asyncio.create_task(daily_check_loop()),
    ]

    await asyncio.sleep(5)
    await send_service_event("bot_started")

    try:
        await asyncio.gather(*tasks)
    finally:
        await send_service_event("bot_stopped")

if __name__ == "__main__":
    asyncio.run(main())
