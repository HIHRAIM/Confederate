"""The /set*feed and /rem*feed command pairs on Telegram.

Thin mirrors of the Discord commands: the localization key dicts and the
attach logic are imported from the Discord half (discord_bot/commands/feeds
and discord_bot/feeds), so a feed behaves identically whichever platform it
was attached from.
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import feed_scope_name, get_chat_lang, is_admin, is_chat_admin, localized

from telegram_bot.client import router

async def _feed_command_tg(message: Message, kind, keys, usage_key):
    """Shared body of `/setbskyfeed`, `/setytfeed` and `/settgfeed` on Telegram."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
    ):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    source = parts[1].strip() if len(parts) > 1 else ""
    if not source:
        await message.reply(localized(usage_key, lang))
        return

    from discord_bot import attach_feed
    status, resolved, title, stale_since = await attach_feed(
        kind, source, "telegram", chat_id, message.from_user.id)
    if status in ("ok", "live"):
        key = keys["attached_live"] if status == "live" else keys["attached"]
        text = localized(key, lang, account=resolved, name=title,
                         where=feed_scope_name(chat_id, lang))
        if stale_since:
            text += "\n\n" + localized("feed_stale_note", lang, account=resolved,
                                       date=stale_since)
        await message.reply(text)
    else:
        await message.reply(localized(keys.get(status) or keys["unreachable"], lang, account=resolved))

async def _rem_feed_command_tg(message: Message, kind, keys, usage_key):
    """Shared body of `/rembskyfeed`, `/remytfeed` and `/remtgfeed` on Telegram."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
    ):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        await message.reply(localized(usage_key, lang))
        return

    from discord_bot import feed_module
    source = feed_module(kind).normalize_source(raw)
    if not source:
        await message.reply(localized(keys["invalid"], lang))
        return

    existing = db.find_feed(kind, source, chat_id)
    if existing and db.remove_feed(kind, source, existing["chat_id"]):
        await message.reply(localized(keys["removed"], lang, account=source))
    else:
        await message.reply(localized(keys["not_attached"], lang, account=source))

@router.message(Command("setytfeed"))
async def setytfeed_cmd(message: Message):
    """Follow a YouTube channel's uploads in this chat's bridge."""
    from discord_bot import YTFEED_KEYS
    await _feed_command_tg(message, "youtube", YTFEED_KEYS, "ytfeed_usage")

@router.message(Command("remytfeed"))
async def remytfeed_cmd(message: Message):
    """Unfollow a YouTube channel."""
    from discord_bot import YTFEED_KEYS
    await _rem_feed_command_tg(message, "youtube", YTFEED_KEYS, "ytfeed_rem_usage")

@router.message(Command("setbskyfeed"))
async def setbskyfeed_cmd(message: Message):
    """Follow a Bluesky account's posts in this chat's bridge."""
    from discord_bot import BSKYFEED_KEYS
    await _feed_command_tg(message, "bluesky", BSKYFEED_KEYS, "bskyfeed_usage")

@router.message(Command("rembskyfeed"))
async def rembskyfeed_cmd(message: Message):
    """Unfollow a Bluesky account."""
    from discord_bot import BSKYFEED_KEYS
    await _rem_feed_command_tg(message, "bluesky", BSKYFEED_KEYS, "bskyfeed_rem_usage")

@router.message(Command("settgfeed"))
async def settgfeed_cmd(message: Message):
    """Follow a public Telegram channel in this chat's bridge."""
    from discord_bot import TGFEED_KEYS
    await _feed_command_tg(message, "telegram", TGFEED_KEYS, "tgfeed_usage")

@router.message(Command("remtgfeed"))
async def remtgfeed_cmd(message: Message):
    """Unfollow a Telegram channel."""
    from discord_bot import TGFEED_KEYS
    await _rem_feed_command_tg(message, "telegram", TGFEED_KEYS, "tgfeed_rem_usage")
