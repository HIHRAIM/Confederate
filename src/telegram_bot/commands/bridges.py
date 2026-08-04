"""Bridge-membership commands on Telegram: /atb, /rfb and /bridge.

The Discord twins live in discord_bot/commands/bridges.py; the differences
here are Telegram's: a chat is a group *topic* (`chat_id:thread`), and /rfb
cannot take an argument at all — see its docstring.
"""
import asyncio

from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import (
    get_chat_lang, is_admin, is_chat_admin, localized, localized_bot_joined,
    localized_bridge_info, localized_bridge_join, localized_bridge_leave,
    rate_limit_ok,
)

from telegram_bot.client import bot, router

@router.message(Command("atb"))
async def atb(message: Message):
    """Attach this chat/topic to a bridge and announce the join in every other
    chat of it.

    The argument is a bridge number — created if it does not exist yet — or
    the word `new`, which opens a bridge on the lowest free number (see
    db.attach_chat_to_new_bridge). Bot Admins only, for both forms; a chat
    already in a bridge is refused, and that check runs before a number is
    allocated so a refused `/atb new` does not burn one."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(localized("atb_usage", lang))
        return

    raw = parts[1].strip()
    wants_new = raw.lower() == "new"
    if not wants_new:
        try:
            bridge_id = int(raw)
        except ValueError:
            await message.reply(localized("atb_invalid_id", lang))
            return

    if db.chat_exists(chat_id):
        await message.reply(localized("atb_already_attached", lang))
        return

    if wants_new:
        bridge_id = db.attach_chat_to_new_bridge("telegram", chat_id)
        if bridge_id is None:
            await message.reply(
                localized("atb_no_free_id", lang, limit=db.APPEAL_BRIDGE_ID_FLOOR)
            )
            return
        reply_key = "atb_attached_new"
    else:
        db.attach_chat("telegram", chat_id, bridge_id)
        reply_key = "atb_attached"

    try:
        await bot.send_message(
            chat_id=int(message.chat.id),
            message_thread_id=int(thread) or None,
            text=localized_bot_joined(lang)
        )
    except Exception:
        await message.reply(localized(reply_key, lang, bridge_id=bridge_id))
    else:
        await message.reply(localized(reply_key, lang, bridge_id=bridge_id))

    channel_or_topic = f"topic {thread}" if thread else (message.chat.title or f"chat {message.chat.id}")
    server_name = message.chat.title or "Private chat"

    rows = db.get_bridge_chats(bridge_id)
    for c in rows:
        if c["platform"] == "telegram" and c["chat_id"] == chat_id:
            continue
        target_lang = get_chat_lang(c["chat_id"])
        notify = localized_bridge_join(channel_or_topic, server_name, target_lang)

        if c["platform"] == "telegram":
            chat_id_str, th = c["chat_id"].split(":")
            try:
                await bot.send_message(
                    chat_id=int(chat_id_str),
                    message_thread_id=int(th) or None,
                    text=notify
                )
            except Exception:
                pass
        elif c["platform"] == "discord":
            try:
                from discord_bot import bot as dc_bot
                chan_id = int(c["chat_id"].split(":")[1])
                channel = dc_bot.get_channel(chan_id)
                if channel:
                    await channel.send(notify)
            except Exception:
                pass

@router.message(Command("rfb"))
async def rfb_handler(message: Message):
    """
    Удаление текущей темы/чата из моста. Удаление по ID в Telegram НЕ поддерживается —
    команда должна запускаться в той теме/чате, который нужно удалить.
    """
    parts = message.text.split()
    thread = message.message_thread_id or 0
    current_chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(current_chat_id)

    if len(parts) > 1:
        await message.reply(localized("rfb_by_id_unsupported", lang))
        return

    user_id = message.from_user.id
    if is_admin("telegram", user_id) or is_chat_admin("telegram", current_chat_id, user_id):
        allowed = True
    else:
        allowed = False

    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (current_chat_id,)).fetchone()
    if not row:
        await message.reply(localized("chat_not_in_bridge", lang))
        return

    bridge_id = row["bridge_id"]

    channel_or_topic = f"topic {thread}" if thread else (message.chat.title or f"chat {message.chat.id}")
    server_name = message.chat.title or "Private chat"

    db.cur.execute("DELETE FROM chats WHERE chat_id=?", (current_chat_id,))
    db.conn.commit()

    rows = db.get_bridge_chats(bridge_id)
    for c in rows:
        target_lang = get_chat_lang(c["chat_id"])
        notify = localized_bridge_leave(channel_or_topic, server_name, target_lang)

        if c["platform"] == "telegram":
            chat_id_str, th = c["chat_id"].split(":")
            try:
                await bot.send_message(
                    chat_id=int(chat_id_str),
                    message_thread_id=int(th) or None,
                    text=notify
                )
            except Exception:
                pass
        elif c["platform"] == "discord":
            try:
                from discord_bot import bot as dc_bot
                chan_id = int(c["chat_id"].split(":")[1])
                channel = dc_bot.get_channel(chan_id)
                if channel:
                    await channel.send(notify)
            except Exception:
                pass

    await message.reply(localized("rfb_removed", lang))

@router.message(Command("bridge"))
async def bridge_cmd(message: Message):
    """Show this chat's bridge: number, member chats, feeds and admins.

    The answer deletes itself after a minute — unlike Discord there are no
    ephemeral replies here, and a chat list is clutter once read."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("bridge-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
        """Reply, then remove the answer after a minute."""
        sent = await message.reply(text)
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)
    ).fetchone()

    if not row:
        await _reply_autodelete(localized_bridge_info("not_in_bridge", lang))
        return

    bridge_id = row["bridge_id"]
    chats = db.get_bridge_chats(bridge_id)

    from discord_bot import bot as dc_bot

    unknown = localized_bridge_info("unknown", lang)
    chat_lines = []
    for chat in chats:
        platform = chat["platform"]
        cid = chat["chat_id"]
        if platform == "discord":
            try:
                guild_id_str, channel_id_str = cid.split(":", 1)
                guild = dc_bot.get_guild(int(guild_id_str))
                server_name = guild.name if guild else unknown
                channel = guild.get_channel(int(channel_id_str)) if guild else None
                chat_name = channel.name if channel else unknown
                display_id = channel_id_str
            except Exception:
                server_name, chat_name, display_id = unknown, unknown, cid
        elif platform == "telegram":
            try:
                tg_chat_id_str, thread_str = cid.split(":", 1)
                thread_id = int(thread_str)
                tg_chat = await bot.get_chat(int(tg_chat_id_str))
                server_name = tg_chat.title or getattr(tg_chat, "full_name", None) or unknown
                if thread_id == 0:
                    chat_name = server_name
                    display_id = tg_chat_id_str
                else:
                    chat_name = localized_bridge_info("topic", lang, thread_id=thread_id)
                    display_id = None
            except Exception:
                server_name, chat_name, display_id = unknown, unknown, cid
        else:
            server_name, chat_name, display_id = platform, unknown, cid

        chat_lines.append(f"* {server_name}: {chat_name}" + (f" ({display_id})" if display_id is not None else ""))

    chats_str = "\n".join(chat_lines) if chat_lines else "—"
    text = localized_bridge_info("tg_template", lang, bridge_id=bridge_id, chats=chats_str)

    attached_feeds = db.get_bridge_feeds(bridge_id)
    if attached_feeds:
        from discord_bot import feed_module
        feeds_str = "\n".join(
            f"* {f['title'] or f['source']}: {feed_module(f['kind']).source_url(f['source'])}"
            for f in attached_feeds
        )
        text = f"{text}\n\n{localized_bridge_info('field_feeds', lang)}:\n{feeds_str}"

    try:
        from discord_bot import resolve_bridge_admins
        discord_admins, telegram_pings = await resolve_bridge_admins(bridge_id)
    except Exception:
        discord_admins, telegram_pings = [], []
    if discord_admins or telegram_pings:
        admin_lines = [localized_bridge_info("admins_title", lang)]
        if discord_admins:
            discord_str = ", ".join((uname or str(uid)) for uid, uname in discord_admins)
            admin_lines.append(localized_bridge_info("admins_discord", lang, admins=discord_str))
        if telegram_pings:
            admin_lines.append(localized_bridge_info("admins_telegram", lang, admins=", ".join(telegram_pings)))
        text = f"{text}\n\n" + "\n".join(admin_lines)

    await _reply_autodelete(text)
