"""Telegram commands of the inbox system — the twins of
discord_bot/commands/inbox.py, same names and same semantics.

Two things are Telegram's own here. `/setinbox` refuses to run outside a
private chat with this bot and deletes the message it came in: a token
posted in a group is a token everyone in that group can use, and asking
politely afterwards does not take it back. And `/setinboxchat` refuses a
group that is not a forum, because a conversation is a *topic* and only a
forum group has them; a plain group would have to put every conversation in
the same stream, which is not a bridge anyone can read.
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
from utils import get_chat_lang, is_admin, is_chat_admin, localized

from telegram_bot.client import bot, router

def _chat_key(message: Message):
    """The bot-wide chat key of the chat a command came from."""
    return f"{message.chat.id}:{message.message_thread_id or 0}"

def _host_key(message: Message):
    """The key a host row is written under: the *group*, never the topic the
    command happened to be typed in.

    A conversation opens as a new topic of the group, so which topic an admin
    was standing in when they ran /setinboxchat means nothing — and storing it
    would mean /reminboxchat only worked from that same topic."""
    return f"{message.chat.id}:0"

def _argument(message: Message, index=1):
    """One whitespace-separated argument of the command, or None."""
    parts = (message.text or "").split()
    return parts[index] if len(parts) > index else None

async def _can_manage_topics(chat_id):
    """Whether *this* bot may open topics in the group.

    Being a forum is not enough, and neither is being an administrator: the
    'Manage topics' right is separate, and without it create_forum_topic
    fails. Asked when the host is configured rather than left to surface as a
    conversation that silently will not open — the person running the command
    is the one who can fix it, and they are standing right here."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception:
        return False
    if str(member.status) == "creator":
        return True
    return bool(getattr(member, "can_manage_topics", False))

def _resolve_managed_bot(message: Message, identifier):
    """Find the receiver bot a command is about and check the caller may
    manage it. Returns ``(bot_row, error_key)``; with no identifier the
    current chat is asked instead."""
    from inbox import can_manage_inbox_bot, inbox_bot_for_chat

    if identifier and identifier.strip():
        bot_row = db.find_inbox_bot(identifier)
    else:
        bot_row = (inbox_bot_for_chat(_chat_key(message))
                   or inbox_bot_for_chat(_host_key(message)))
    if bot_row is None:
        return None, "inbox_unknown_bot"
    if not can_manage_inbox_bot(bot_row, "telegram", message.from_user.id):
        return None, "no_permission"
    return bot_row, None

@router.message(Command("setinbox"))
async def setinbox_cmd(message: Message):
    """Register a receiver bot, or replace the token of one already known.

    Private chats only, and the message carrying the token is deleted as soon
    as it has been read — the token stays in Telegram's history otherwise,
    and in a group that history belongs to everyone."""
    from inbox import inbox_bot_place_name, register_inbox_bot

    lang = get_chat_lang(_chat_key(message))

    if getattr(message.chat, "type", None) != "private":
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        await message.answer(localized("setinbox_private_only", lang))
        return

    token = _argument(message)
    if not token:
        await message.reply(localized("setinbox_usage", lang))
        return

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    bot_row, error_key, was_update = await register_inbox_bot(
        token.strip(), "telegram", message.from_user.id
    )
    if error_key and bot_row is None:
        await message.answer(localized(error_key, lang))
        return

    name = inbox_bot_place_name(bot_row)
    if error_key:
        await message.answer(localized(error_key, lang, bot=name))
        return
    await message.answer(
        localized("setinbox_updated" if was_update else "setinbox_added", lang, bot=name)
    )

@router.message(Command("reminbox"))
async def reminbox_cmd(message: Message):
    """Take a receiver bot out of service: close its conversations, stop its
    polling, drop its rows."""
    from inbox import inbox_bot_place_name, unregister_inbox_bot

    lang = get_chat_lang(_chat_key(message))
    bot_row, error_key = _resolve_managed_bot(message, _argument(message))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    name = inbox_bot_place_name(bot_row)
    await unregister_inbox_bot(bot_row)
    await message.reply(localized("reminbox_done", lang, bot=name))

@router.message(Command("setinboxchat"))
async def setinboxchat_cmd(message: Message):
    """Make this group one of the places a receiver bot reports into: every
    person writing to it gets a topic here.

    The group must be a forum — that is where topics come from. Which topic
    the command is run in does not matter; conversations open as new topics
    of the group either way."""
    from inbox import inbox_bot_place_name

    lang = get_chat_lang(_chat_key(message))
    bot_row, error_key = _resolve_managed_bot(message, _argument(message))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    if not getattr(message.chat, "is_forum", False):
        await message.reply(localized("setinboxchat_not_forum", lang))
        return

    if not await _can_manage_topics(message.chat.id):
        await message.reply(localized("setinboxchat_no_topic_rights", lang))
        return

    name = inbox_bot_place_name(bot_row)
    host_key = _host_key(message)
    if db.get_inbox_host(bot_row["bot_id"], host_key):
        await message.reply(localized("setinboxchat_already", lang, bot=name))
        return

    db.add_inbox_host(bot_row["bot_id"], "telegram", host_key, message.from_user.id)
    await message.reply(localized("setinboxchat_done", lang, bot=name))

@router.message(Command("reminboxchat"))
async def reminboxchat_cmd(message: Message):
    """Stop this group from hosting new conversations of a receiver bot.
    Topics already open keep working until their conversation closes."""
    from inbox import inbox_bot_place_name

    lang = get_chat_lang(_chat_key(message))
    bot_row, error_key = _resolve_managed_bot(message, _argument(message))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    name = inbox_bot_place_name(bot_row)
    if not db.remove_inbox_host(bot_row["bot_id"], _host_key(message)):
        await message.reply(localized("reminboxchat_not_host", lang, bot=name))
        return
    await message.reply(localized("reminboxchat_done", lang, bot=name))

@router.message(Command("inboxanon"))
async def inboxanon_cmd(message: Message):
    """Turn staff anonymization on or off for one receiver bot: with it on,
    everyone answering is signed 'Staff A', 'Staff B', … to the person
    writing in. Usage: /inboxanon enable|disable [bot]."""
    from inbox import inbox_bot_place_name

    lang = get_chat_lang(_chat_key(message))
    state = (_argument(message) or "").strip().lower()
    if state not in ("enable", "disable"):
        await message.reply(localized("inboxanon_usage", lang))
        return

    bot_row, error_key = _resolve_managed_bot(message, _argument(message, 2))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    enabled = state == "enable"
    db.set_inbox_anonymize(bot_row["bot_id"], enabled)
    await message.reply(localized(
        "inboxanon_enabled" if enabled else "inboxanon_disabled", lang,
        bot=inbox_bot_place_name(bot_row),
    ))

@router.message(Command("inboxlist"))
async def inboxlist_cmd(message: Message):
    """The registered receiver bots with their state, host chats and open
    conversations. Bot Admins see all of them; anyone else sees their own."""
    from inbox import inbox_bot_instance, inbox_bot_place_name

    lang = get_chat_lang(_chat_key(message))
    rows = db.get_inbox_bots()
    if not is_admin("telegram", message.from_user.id):
        rows = [r for r in rows
                if r["owner_platform"] == "telegram" and str(r["owner_id"]) == str(message.from_user.id)]
    if not rows:
        await message.reply(localized("inboxlist_empty", lang))
        return

    lines = [localized("inboxlist_title", lang)]
    for row in rows:
        hosts = db.get_inbox_hosts(row["bot_id"])
        host_names = ", ".join(h["chat_id"] for h in hosts) or localized("inboxlist_no_hosts", lang)
        lines.append(localized(
            "inboxlist_entry", lang,
            bot=inbox_bot_place_name(row),
            id=row["bot_id"],
            state=localized(
                "inboxlist_state_online" if inbox_bot_instance(row["bot_id"]) else "inboxlist_state_offline",
                lang,
            ),
            anon=localized("inboxlist_anon_on" if row["anonymize"] else "inboxlist_anon_off", lang),
            hosts=host_names,
            conversations=len(db.get_inbox_conversations_of_bot(row["bot_id"])),
        ))
    await message.reply("\n".join(lines))

@router.message(Command("close"))
async def close_conversation_cmd(message: Message):
    """Close the conversation this topic belongs to: its title goes ⬛, both
    sides are told, the topic is closed, the thread on the other platform is
    archived, and the bridge goes.

    Named without the `inbox` prefix the configuration commands carry — this
    one is run inside a conversation as part of answering it, not to set
    anything up."""
    from inbox import can_manage_inbox_bot, close_inbox_conversation, inbox_conversation_of_chat

    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)
    conv = inbox_conversation_of_chat(chat_key)
    if conv is None:
        await message.reply(localized("close_not_conversation", lang))
        return

    bot_row = db.get_inbox_bot(conv["bot_id"])
    if not (can_manage_inbox_bot(bot_row, "telegram", message.from_user.id)
            or is_chat_admin("telegram", chat_key, message.from_user.id)):
        await message.reply(localized("no_permission", lang))
        return

    await message.reply(localized("close_done", lang))
    await close_inbox_conversation(conv)

def _can_set_header(message: Message, chat_key):
    """Bot Admins, and the Bridge Admins of the chat the command was run in.

    Narrower than `is_chat_admin` on purpose — the Discord twin's docstring
    says why."""
    if is_admin("telegram", message.from_user.id):
        return True
    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if row and str(message.from_user.id) in db.get_bridge_admins(row["bridge_id"]):
        return True
    return db.is_server_bridge_admin("telegram", str(message.chat.id), message.from_user.id)

@router.message(Command("close-header", "close_header"))
async def close_header_cmd(message: Message):
    """Drop the ``[Telegram | ЛС] Name:`` line from the copies a receiver
    bot's conversations deliver into this group.

    Scoped to this group and this receiver bot, staff side only; the writer's
    own copies carry just a name either way. Bot Admins and Bridge Admins.
    Usage: /close-header hide|show [bot]."""
    from inbox import inbox_bot_for_chat, inbox_bot_place_name

    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)

    state = (_argument(message) or "").strip().lower()
    if state not in ("hide", "show"):
        await message.reply(localized("close_header_usage", lang))
        return

    if not _can_set_header(message, chat_key):
        await message.reply(localized("no_permission", lang))
        return

    identifier = _argument(message, 2)
    if identifier and identifier.strip():
        bot_row = db.find_inbox_bot(identifier)
    else:
        bot_row = inbox_bot_for_chat(chat_key) or inbox_bot_for_chat(_host_key(message))
    if bot_row is None:
        await message.reply(localized("inbox_unknown_bot", lang))
        return

    host = db.get_inbox_host_of_community(bot_row["bot_id"], "telegram", chat_key)
    if host is None:
        await message.reply(
            localized("close_header_not_host", lang, bot=inbox_bot_place_name(bot_row))
        )
        return

    hidden = state == "hide"
    db.set_inbox_header_hidden(bot_row["bot_id"], host["chat_id"], hidden)
    await message.reply(localized(
        "close_header_hidden" if hidden else "close_header_shown", lang,
        bot=inbox_bot_place_name(bot_row),
    ))

@router.message(Command("inboxban"))
async def inboxban_cmd(message: Message):
    """Ban someone from one receiver bot: their conversation closes and
    everything they send that bot afterwards is dropped.

    Run inside their conversation topic it needs no arguments — the topic
    names both the bot and the person. Usage elsewhere:
    /inboxban <user> [bot]."""
    from inbox import ban_inbox_user, inbox_conversation_of_chat, resolve_inbox_user

    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)
    conv = inbox_conversation_of_chat(chat_key)

    bot_row, error_key = _resolve_managed_bot(message, _argument(message, 2))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    identifier = _argument(message)
    if identifier:
        target = await resolve_inbox_user(bot_row["bot_id"], identifier)
    elif conv is not None:
        target = conv["user_id"]
    else:
        target = None
    if target is None:
        await message.reply(localized("inboxban_usage", lang))
        return

    if db.is_inbox_banned(bot_row["bot_id"], target):
        await message.reply(localized("inboxban_already", lang))
        return

    await ban_inbox_user(bot_row, target, message.from_user.id)
    await message.reply(localized("inboxban_done", lang, user=target))

@router.message(Command("inboxunban"))
async def inboxunban_cmd(message: Message):
    """Lift a ban. Nothing reopens by itself: the user's next message to the
    bot starts a new conversation. Usage: /inboxunban <user> [bot]."""
    from inbox import inbox_conversation_of_chat, resolve_inbox_user

    chat_key = _chat_key(message)
    lang = get_chat_lang(chat_key)
    conv = inbox_conversation_of_chat(chat_key)

    bot_row, error_key = _resolve_managed_bot(message, _argument(message, 2))
    if error_key:
        await message.reply(localized(error_key, lang))
        return

    identifier = _argument(message)
    if identifier:
        target = await resolve_inbox_user(bot_row["bot_id"], identifier)
    elif conv is not None:
        target = conv["user_id"]
    else:
        target = None
    if target is None:
        await message.reply(localized("inboxunban_usage", lang))
        return

    if not db.remove_inbox_ban(bot_row["bot_id"], target):
        await message.reply(localized("inboxunban_not_banned", lang))
        return
    await message.reply(localized("inboxunban_done", lang, user=target))
