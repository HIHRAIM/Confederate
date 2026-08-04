"""Chat-configuration commands on Telegram: /locallang, /lang, /allow_bots,
/allow_files and /remindrules.

Two differences from the Discord twins are deliberate: chat-level settings
also accept Telegram's own group administrators (is_telegram_native_admin),
and /remindrules takes its content from the replied-to message rather than a
parameter.
"""
import time

from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import (
    SUPPORTED_LANGS, get_chat_lang, is_admin, is_chat_admin, localized,
    set_chat_lang,
)

from telegram_bot.client import is_telegram_native_admin, logger, router

@router.message(Command("locallang"))
async def locallang_handler(message: Message):
    """Set the bot language for this chat/topic (overrides the group-wide
    /lang). Chat admins, Telegram group admins and Bot Admins."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split()
    if len(parts) != 2:
        await message.reply(localized("locallang_usage", lang))
        return

    code = parts[1].strip().lower()

    has_permission = (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_key, message.from_user.id)
        or await is_telegram_native_admin(message.chat.id, message.from_user.id)
    )
    if not has_permission:
        await message.reply(localized("no_permission", lang))
        return

    try:
        set_chat_lang(chat_key, code)
    except ValueError:
        await message.reply(localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))))
        return
    except Exception as e:
        logger.warning("Failed to save language for %s: %s", chat_key, e)
        await message.reply(localized("lang_save_error", lang))
        return

    await message.reply(localized("lang_set", code, code=code))

@router.message(Command("lang"))
async def lang_handler(message: Message):
    """Set the group-wide default language (stored under the bare group id;
    /locallang beats it per topic). Bridge Admins and Bot Admins."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split()
    if len(parts) != 2:
        await message.reply(localized("lang_usage", lang))
        return

    code = parts[1].strip().lower()

    allowed = is_admin("telegram", message.from_user.id)
    if not allowed:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row and str(message.from_user.id) in db.get_bridge_admins(row["bridge_id"]):
            allowed = True
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    try:
        set_chat_lang(str(message.chat.id), code)
    except ValueError:
        await message.reply(localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))))
        return
    except Exception as e:
        logger.warning("Failed to save language for %s: %s", message.chat.id, e)
        await message.reply(localized("lang_save_error", lang))
        return

    await message.reply(localized("lang_set_server", code, code=code))

@router.message(Command("allow_bots"))
async def allow_bots_cmd(message: Message):
    """Toggle relaying of other bots' messages for this chat (default off)."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ("enable", "disable"):
        await message.reply(localized("allow_bots_usage_tg", lang))
        return

    has_permission = (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
        or await is_telegram_native_admin(message.chat.id, message.from_user.id)
    )
    if not has_permission:
        await message.reply(localized("no_permission", lang))
        return

    enabled = parts[1].lower() == "enable"
    db.set_allow_bots(chat_id, enabled)
    if enabled:
        await message.reply(localized("allow_bots_enabled", lang))
    else:
        await message.reply(localized("allow_bots_disabled", lang))

@router.message(Command("allow_files", "allow-files"))
async def allow_files_cmd(message: Message):
    """Grant or withdraw the GALLERY file-reupload consent, group-wide or
    (with a trailing `local`) for this chat's bridge. The consent semantics
    are documented on db.bridge_file_relay_enabled."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    parts = message.text.split()
    action = parts[1].lower() if len(parts) > 1 else ""
    scope = parts[2].lower() if len(parts) > 2 else ""
    if len(parts) > 3 or action not in ("enable", "disable") or scope not in ("", "local"):
        await message.reply(localized("allow_files_usage_tg", lang))
        return

    if not (is_admin("telegram", message.from_user.id)
            or is_chat_admin("telegram", chat_id, message.from_user.id)):
        await message.reply(localized("allow_files_no_permission", lang))
        return

    enabled = action == "enable"
    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await message.reply(localized("chat_not_in_bridge", lang))
            return
        db.set_bridge_file_consent(row["bridge_id"], enabled, enabled_by=message.from_user.id)
        key = "allow_files_bridge_enabled" if enabled else "allow_files_bridge_disabled"
    else:
        db.set_server_file_consent("telegram", str(message.chat.id), enabled, enabled_by=message.from_user.id)
        key = "allow_files_enabled" if enabled else "allow_files_disabled"

    await message.reply(localized(key, lang))

@router.message(Command("remindrules"))
async def remindrules(message: Message):
    """Configure the bridge-wide rules reminder. The content is the message
    this command replies to (hence the reply requirement); the interval is
    '2h'/'30m'/bare hours, stored in minutes, and an optional second argument
    is the minimum message count between posts."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.reply_to_message:
        await message.reply(localized("remindrules_reply_required", lang))
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(localized("remindrules_usage_telegram", lang))
        return

    raw = parts[1].strip().lower()
    try:
        if raw.endswith("h"):
            interval_minutes = int(raw[:-1]) * 60
        elif raw.endswith("m"):
            interval_minutes = int(raw[:-1])
        else:
            interval_minutes = int(raw) * 60
        if interval_minutes <= 0:
            raise ValueError
    except ValueError:
        await message.reply(localized("remindrules_invalid_duration", lang))
        return

    messages = int(parts[2]) if len(parts) > 2 else None

    if not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
    ):
        await message.reply(localized("no_permission", lang))
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await message.reply(localized("chat_not_in_bridge", lang))
        return

    bridge_id = row["bridge_id"]
    ref = message.reply_to_message

    db.cur.execute(
        """
        INSERT OR REPLACE INTO bridge_rules
        (bridge_id, content, format, origin_platform, origin_chat_id,
         origin_message_id, hours, messages, last_post_ts, message_counter)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            bridge_id,
            getattr(ref, "text", "") or getattr(ref, "caption", "") or "",
            "telegram",
            "telegram",
            chat_id,
            str(ref.message_id) if hasattr(ref, "message_id") else str(ref.id),
            interval_minutes,
            messages,
            int(time.time()) - (interval_minutes * 60),
            0
        )
    )
    db.conn.commit()

    human = f"{interval_minutes // 60}h {interval_minutes % 60}m".replace("0h ", "").replace(" 0m", "").strip()
    await message.reply(localized("remindrules_saved", lang, interval=human))
