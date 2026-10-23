"""The aiogram objects (bot, dispatcher, router), the polling entry point,
and the small resolvers every Telegram module needs.

Named client.py rather than bot.py on purpose, mirroring discord_bot: the
package re-exports the Bot instance as ``telegram_bot.bot`` (that is how the
whole codebase reaches it), and a submodule of that same name would shadow
the instance on the package object.
"""
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import ChatMemberUpdated

import db
from config import TELEGRAM_TOKEN
from message_relay import escape_html

logger = logging.getLogger("bridge.telegram")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

async def get_telegram_avatar_url(user_id, host_chat_id=None):
    """Discord-usable webhook avatar URL for a Telegram sender, picked by the last
    digit of the user's ID.

    Which picture goes with which digit is TG_AVATAR_ASSETS, and it lives with
    the avatars in discord_bot/feeds.py rather than here: the module that
    hosts the pictures is also the one that knows at start-up which of them
    the bot is going to ask for."""
    try:
        last_digit = int(user_id) % 10
    except Exception:
        return None
    from discord_bot import TG_AVATAR_ASSETS, avatar_asset_url
    asset = TG_AVATAR_ASSETS.get(last_digit)
    if not asset:
        return None
    return await avatar_asset_url(asset)

async def _telegram_relay_avatar_url(bridge_id, user_id):
    """Resolve a sender's avatar only if some Discord target has /webhooks on."""
    if not user_id:
        return None
    try:
        targets = db.get_bridge_chats(bridge_id)
    except Exception:
        return None
    wh_targets = [c["chat_id"] for c in targets
                  if c["platform"] == "discord" and db.get_webhooks_enabled(c["chat_id"])]
    if not wh_targets:
        return None
    return await get_telegram_avatar_url(int(user_id), host_chat_id=wh_targets[0])

def _telegram_html_mention(user) -> str:
    """A ping-able HTML mention of a user: their @username when public,
    otherwise a tg://user link on their full name (the only way to address
    someone without a username)."""
    if getattr(user, "username", None):
        return f"@{escape_html(user.username)}"
    full_name = escape_html(getattr(user, "full_name", "User"))
    return f'<a href="tg://user?id={user.id}">{full_name}</a>'

def username_of(user):
    """Display form of a user for logs and support messages: @username, else
    their full name, else the bare id."""
    if user is None:
        return "Unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "full_name", None) or str(getattr(user, "id", "Unknown"))

async def resolve_telegram_user(identifier: str):
    """
    Принимает username (@name) или numeric id as string.
    Возвращает user_id (int) или None.
    """
    identifier = identifier.strip()
    if identifier.lstrip("-").isdigit():
        try:
            return int(identifier)
        except Exception:
            return None
    if identifier.startswith("@"):
        try:
            ch = await bot.get_chat(identifier)
            return ch.id
        except Exception:
            return None
    try:
        ch = await bot.get_chat(identifier)
        return ch.id
    except Exception:
        return None

async def send_relay_texts(chat_key, texts, *, reply_to_message_id=None, **options):
    """Send a relayed copy that `message_relay.build_telegram_texts` made
    into one or more Telegram messages, in order, into one chat or topic.

    Only the first carries the reply reference and `options` (the link
    preview); a reply whose target is gone is sent again without it, as a
    single copy always was. Returns the first message's id — a
    `message_relay.SentCopy` with the others when there were more. A part
    that fails ends the sending, and what was sent is kept."""
    from message_relay import SentCopy

    chat_id_str, thread = chat_key.split(":")
    base = dict(chat_id=int(chat_id_str), message_thread_id=int(thread) or None, parse_mode="HTML")
    first = dict(base, text=texts[0], **options)
    if reply_to_message_id:
        first["reply_to_message_id"] = int(reply_to_message_id)
    try:
        sent = await bot.send_message(**first)
    except Exception:
        if not reply_to_message_id:
            raise
        first.pop("reply_to_message_id", None)
        sent = await bot.send_message(**first)
    parts = []
    quiet = {"disable_web_page_preview": True} if options.get("disable_web_page_preview") else {}
    for text in texts[1:]:
        try:
            part = await bot.send_message(**base, text=text, **quiet)
        except Exception as e:
            logger.warning("A continuation of a relayed copy failed (%s): %s", chat_key, e)
            break
        parts.append(part.message_id)
    return SentCopy(sent.message_id, parts) if parts else str(sent.message_id)

async def edit_relay_texts(chat_key, message_db_id, chat, message_id_platform, texts, **options):
    """Rewrite a relayed Telegram copy after an edit: its first message with
    the first text (and `options`, the link preview), its continuations
    with the rest — sending new ones or deleting the ones no longer needed
    (`message_relay.sync_copy_parts`)."""
    from message_relay import sync_copy_parts

    chat_id = int(chat_key.split(":")[0])
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=int(message_id_platform),
                                    text=texts[0], parse_mode="HTML", **options)
    except Exception as e:
        if "not modified" not in str(e):
            raise

    async def edit_part(part_id, text):
        """Rewrite one continuation."""
        await bot.edit_message_text(chat_id=chat_id, message_id=int(part_id), text=text, parse_mode="HTML")

    async def send_part(text):
        """Send a continuation the longer edit needs."""
        thread = int(chat_key.split(":")[1]) or None
        sent = await bot.send_message(chat_id=chat_id, message_thread_id=thread, text=text, parse_mode="HTML")
        return sent.message_id

    async def delete_part(part_id):
        """Delete a continuation the shorter edit no longer needs."""
        await bot.delete_message(chat_id, int(part_id))

    await sync_copy_parts(message_db_id, chat, texts[1:], edit_part=edit_part,
                          send_part=send_part, delete_part=delete_part)

async def is_telegram_native_admin(chat_id: int, user_id: int):
    """Whether the user is a Telegram-side administrator (or owner) of the
    group — a right the bridge honors alongside its own roles for chat-level
    settings."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

TELEGRAM_GROUP_TYPES = ("group", "supergroup")

@router.my_chat_member()
async def my_chat_member_update(update: ChatMemberUpdated):
    """
    When bot is removed from a chat (left/kicked), clean up chat_settings for that chat.

    Also covers followed channels: losing administrator rights in a channel
    means its posts stop arriving on their own, so its feeds fall back to
    polling the public preview.

    A removal also starts the seven-day countdown on the group's
    `/allow-files` consent, and an addition calls it off — a group that takes
    the bot back within the week keeps the setting it gave
    (db.mark_server_departed).

    And it is the one moment the bot can learn that it has been *added* to a
    group, which is what starts the seven-day setup deadline
    (setup_deadline.py): Telegram has no equivalent of Discord's join
    timestamp, so a group with no row here is simply never examined. The
    change must come from outside the chat — old status left or kicked — or a
    promotion to administrator in a group the bot has been sitting in for
    years would read as a fresh arrival and put it on the clock.
    """
    try:
        new_status = str(update.new_chat_member.status)
        old_status = str(update.old_chat_member.status) if update.old_chat_member else ""
        me = await bot.get_me()
        if update.new_chat_member.user.id != me.id:
            return
        if new_status in ("left", "kicked"):
            db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{update.chat.id}:%",))
            db.conn.commit()
            db.forget_deadline("telegram", update.chat.id)
            db.mark_server_departed("telegram", update.chat.id)
        elif (getattr(update.chat, "type", None) in TELEGRAM_GROUP_TYPES
              and old_status in ("left", "kicked")):
            db.record_join("telegram", update.chat.id)
            db.clear_server_departure("telegram", update.chat.id)
            from utils import send_service_event
            await send_service_event(
                "joined_chat",
                platform="Telegram",
                chat=update.chat.title or str(update.chat.id),
                chat_id=update.chat.id,
            )
        if getattr(update.chat, "type", None) == "channel" and "administrator" not in new_status:
            from telegram_bot.feeds import _demote_live_channel_feeds
            await _demote_live_channel_feeds(update.chat.id)
    except Exception:
        pass

async def main():
    """Start long polling. Runs as one of the tasks main.py gathers."""
    await dp.start_polling(bot)
