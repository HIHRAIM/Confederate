"""Role-management commands on Telegram (/setadmin, /remadmin,
/setlocaladmin, /remlocaladmin, /localizer_add, /localizer_rem) and the
bot-admin /backup.

Telegram has no typed command parameters, so each command parses its own
argument string; the two helpers here — scope parsing and target resolution
(which also accepts a reply or a text-mention) — are what the Discord side
gets from the slash-command framework for free.
"""
import logging

from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import get_chat_lang, is_admin, is_chat_admin, localized

from telegram_bot.client import bot, resolve_telegram_user, router

logger = logging.getLogger("bridge.telegram")

def _split_scope_arg(text):
    """``(target, scope)`` from an admin command's argument, where the optional
    trailing word is `local` (also accepted as `scope:local`)."""
    parts = (text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return "", ""
    head, _, tail = arg.rpartition(" ")
    tail_scope = tail.strip().lower().removeprefix("scope:")
    if head and tail_scope == "local":
        return head.strip(), "local"
    return arg, ""

async def _resolve_tg_admin_target(message: Message, arg):
    """Resolve an admin-command target to (user_id, username|None):
    a reply, a text-mention entity, a numeric id, or a public @username."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.username
    for ent in (message.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user.id, ent.user.username
    arg = (arg or "").strip()
    if not arg:
        return None, None
    if arg.lstrip("-").isdigit():
        uid = int(arg)
        try:
            ch = await bot.get_chat(uid)
            return uid, getattr(ch, "username", None)
        except Exception:
            return uid, None
    try:
        ch = await bot.get_chat(arg if arg.startswith("@") else f"@{arg}")
        return ch.id, getattr(ch, "username", None) or arg.lstrip("@")
    except Exception:
        return None, None

@router.message(Command("setadmin"))
async def setadmin(message: Message):
    """Bridge Admin rights, in the same two scopes as `/allow_files`: every
    bridge this group takes part in — including ones it joins later — or, with a
    trailing `local`, the bridge of this chat alone."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    identifier, scope = _split_scope_arg(message.text)
    if not identifier:
        await message.reply(localized("setadmin_usage", lang))
        return

    if not (is_admin("telegram", message.from_user.id)
            or is_chat_admin("telegram", chat_id, message.from_user.id)):
        await message.reply(localized("no_permission", lang))
        return

    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    place = message.chat.title or str(message.chat.id)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await message.reply(localized("chat_not_in_bridge", lang))
            return
        bridge_id = row["bridge_id"]
        db.add_bridge_admin(bridge_id, uid)
        await message.reply(localized("setadmin_bridge_done", lang, user_id=uid, bridge_id=bridge_id))
        dm = localized("setadmin_bridge_dm", lang, bridge_id=bridge_id, place=place)
    else:
        db.add_server_bridge_admin("telegram", message.chat.id, uid,
                                   added_by=message.from_user.id)
        await message.reply(localized("setadmin_server_done", lang, user_id=uid, place=place))
        dm = localized("setadmin_server_dm", lang, place=place)

    try:
        await bot.send_message(uid, dm)
    except Exception:
        pass

@router.message(Command("remadmin"))
async def remadmin(message: Message):
    """Revoke Bridge Admin rights in either scope; the server-wide branch also
    clears the materialized chat_admins rows of this group. Bot Admins only."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    identifier, scope = _split_scope_arg(message.text)
    if not identifier:
        await message.reply(localized("remadmin_usage", lang))
        return

    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await message.reply(localized("chat_not_in_bridge", lang))
            return
        db.remove_bridge_admin(row["bridge_id"], uid)
    else:
        db.remove_server_bridge_admin("telegram", message.chat.id, uid)
        db.cur.execute(
            "DELETE FROM chat_admins WHERE platform=? AND chat_id LIKE ? AND user_id=?",
            ("telegram", f"{message.chat.id}:%", str(uid))
        )
        db.conn.commit()

    await message.reply(localized("remadmin_done", lang, user_id=uid))

@router.message(Command("setlocaladmin"))
async def setlocaladmin(message: Message):
    """Grant server-wide Local Admin (the control-panel scoped-login role).
    Group chats only — the grant is scoped to a community."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("setlocaladmin_usage", lang))
        return

    uid, username = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    server_id = str(message.chat.id)
    if db.is_server_admin("telegram", server_id, uid):
        await message.reply(localized("setlocaladmin_already", lang, user_id=uid))
        return

    db.add_server_admin("telegram", server_id, uid,
                        username=username, added_by=message.from_user.id)
    await message.reply(localized("setlocaladmin_done", lang, user_id=uid))
    try:
        await bot.send_message(
            uid,
            localized("setlocaladmin_dm", lang,
                      server=message.chat.title or server_id)
        )
    except Exception:
        pass

@router.message(Command("remlocaladmin"))
async def remlocaladmin(message: Message):
    """Revoke a Local Admin grant made with /setlocaladmin."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply(localized("group_only", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("remlocaladmin_usage", lang))
        return

    uid, _ = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    server_id = str(message.chat.id)
    if not db.is_server_admin("telegram", server_id, uid):
        await message.reply(localized("remlocaladmin_not_admin", lang, user_id=uid))
        return

    db.remove_server_admin("telegram", server_id, uid)
    await message.reply(localized("remlocaladmin_done", lang, user_id=uid))

@router.message(Command("localizer_add", "localizer-add"))
async def localizer_add(message: Message):
    """Grant the Localizer role (control-panel localization editing)."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("localizer_add_usage", lang))
        return

    uid, username = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if db.is_localizer("telegram", uid):
        await message.reply(localized("localizer_add_already", lang, user_id=uid))
        return

    db.add_localizer("telegram", uid, username=username,
                     added_by=message.from_user.id)
    await message.reply(localized("localizer_add_done", lang, user_id=uid))
    try:
        await bot.send_message(uid, localized("localizer_add_dm", lang))
    except Exception:
        pass

@router.message(Command("localizer_rem", "localizer-rem"))
async def localizer_rem(message: Message):
    """Revoke a Localizer grant (admins hold the role implicitly and have no
    row to revoke)."""
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    if not message.from_user or not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    has_reply = message.reply_to_message and message.reply_to_message.from_user
    if not arg and not has_reply:
        await message.reply(localized("localizer_rem_usage", lang))
        return

    uid, _ = await _resolve_tg_admin_target(message, arg)
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if not db.remove_localizer("telegram", uid):
        await message.reply(localized("localizer_rem_not", lang, user_id=uid))
        return

    await message.reply(localized("localizer_rem_done", lang, user_id=uid))

@router.message(Command("backup"))
async def backup_tg_cmd(message: Message):
    """Send the caller an encrypted database snapshot. Private chats only:
    the file must not land in a group, even an admin one."""
    thread = message.message_thread_id or 0
    lang = get_chat_lang(f"{message.chat.id}:{thread}")
    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return
    if message.chat.type != "private":
        await message.reply(localized("backup_private_only", lang))
        return
    try:
        from aiogram.types import BufferedInputFile
        from backup_crypto import build_encrypted_backup, encrypted_filename
        data = build_encrypted_backup("bridge.db")
        doc = BufferedInputFile(data, filename=encrypted_filename("bridge.db"))
        await bot.send_document(chat_id=message.chat.id, document=doc)
    except Exception as e:
        logger.warning("Failed to send database backup: %s", e)
        await message.reply(localized("backup_failed", lang, error=str(e)))
