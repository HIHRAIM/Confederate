"""The wiki-relay commands on Telegram: `/setwikifeed` and `/remwikifeed`.

Mirrors discord_bot/commands/wiki.py — same permission rule, same attach
machinery, same subscription — so a wiki connected from either platform
behaves identically and can be removed from the other.
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import bridge_feed_permission, feed_scope_name, get_chat_lang, localized

from telegram_bot.client import router

from discord_bot.commands.wiki import WIKIFEED_KEYS

@router.message(Command("setwikifeed"))
async def setwikifeed_cmd(message: Message):
    """Follow a wiki's recent changes in this chat's bridge.

    Any MediaWiki works and any link to it will do. Relaying starts from the
    moment of attaching — the history is not replayed — and reaches every
    chat of the bridge, including chats that join later."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user:
        return
    allowed, in_bridge = bridge_feed_permission("telegram", chat_id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        await message.reply(localized("wikifeed_usage", lang))
        return

    from discord_bot import attach_feed
    status, source, title, stale_since = await attach_feed(
        "wiki", raw, "telegram", chat_id, message.from_user.id)

    if status == "ok":
        text = localized("wikifeed_attached", lang, wiki=source, name=title or source,
                         where=feed_scope_name(chat_id, lang))
        from discord_bot.commands.wiki import attach_discussions_if_any
        if await attach_discussions_if_any(source, "telegram", chat_id, message.from_user.id):
            text += "\n\n" + localized("wikifeed_discussions_note", lang)
        if not in_bridge:
            text += "\n\n" + localized("wikifeed_chat_only_note", lang)
        if stale_since:
            text += "\n\n" + localized("feed_stale_note", lang, account=source, date=stale_since)
    else:
        key = WIKIFEED_KEYS.get(status) or WIKIFEED_KEYS["unreachable"]
        text = localized(key, lang, wiki=source or raw)
    await message.reply(text)

@router.message(Command("remwikifeed"))
async def remwikifeed_cmd(message: Message):
    """Unfollow a wiki. Any link to it is accepted, not only the one it was
    attached with — both reduce to the same subscription key."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user:
        return
    allowed, _ = bridge_feed_permission("telegram", chat_id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        await message.reply(localized("wikifeed_rem_usage", lang))
        return

    from discord_bot import feed_module
    source = feed_module("wiki").normalize_source(raw)
    if not source:
        await message.reply(localized("wikifeed_invalid_url", lang, wiki=raw))
        return

    existing = db.find_feed("wiki", source, chat_id)
    if existing and db.remove_feed("wiki", source, existing["chat_id"]):
        from discord_bot.commands.wiki import detach_discussions
        detach_discussions(source, existing["chat_id"])
        await message.reply(localized("wikifeed_removed", lang, wiki=source))
    else:
        await message.reply(localized("wikifeed_not_attached", lang, wiki=source))

@router.message(Command("wikifeeds"))
async def wikifeeds_cmd(message: Message):
    """List every wiki this chat receives activity from, with its filters and
    output settings."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)
    from discord_bot.commands.wiki import format_wiki_feed_list
    await message.reply(format_wiki_feed_list(chat_id, lang))

@router.message(Command("wikifeed_settings", "wikifeed-settings"))
async def wikifeed_settings_cmd(message: Message):
    """Configure one wiki subscription with `option=value` pairs, or show its
    current settings when only the link is given. Shares its validation and
    storage with the Discord command."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user:
        return
    allowed, _ = bridge_feed_permission("telegram", chat_id, message.from_user.id)
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(localized("wikifeed_settings_usage", lang))
        return

    from discord_bot.commands.wiki import apply_wiki_settings
    await message.reply(apply_wiki_settings(
        chat_id, parts[1], " ".join(parts[2:]), lang, message.from_user.id))
