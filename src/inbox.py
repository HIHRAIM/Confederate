"""The inbox system: the private chats of *other* Telegram bots, bridged.

An admin hands Confederate the token of another Telegram bot (`/setinbox`)
and names the chats that bot should report into (`/setinboxchat`, run once
per chat). From then on, every person who writes to that bot opens a
conversation: Confederate creates a thread in each Discord host channel and a
topic in each Telegram host group, puts them and the private chat into one
bridge, and the two sides talk through it like any other bridge. Naming a
second host is what widens a conversation past two chats — one incoming
private chat can reach a Discord thread and a Telegram topic at once.

**Confederate opens the threads and topics, not the receiver bot.** On
Discord there is no choice: the receiver bot is a Telegram bot with no
presence there at all. On Telegram there is one, and this is the cheaper
side of it — Confederate already administrates the host group, while the
receiver bot would have to be invited to every host and promoted with
`can_manage_topics` before it could create anything. Leaving it out of the
groups means the whole cost of a registered bot is one long-poll connection
for its own private chats; the alternative would add a second membership, a
second permission set and a second point of failure per host, and buy
nothing.

The other end of that decision is what a receiver bot *is* here: an aiogram
Bot and a Dispatcher of its own, polled by one task (`_runtimes`). It is
deliberately not attached to the main router — the handlers there assume the
main bot's chats, and an update from a receiver bot reaching them would be
relayed as though it came from a bridged group.

Writing to a receiver bot is itself the consent to being forwarded (`/start`
says so), so the consent ladder is not run here — and, unlike `/appeal`, no
`verified_users` row is written either: consenting to talk to one inbox is
not consenting to be relayed everywhere else.

Storage is db/inbox.py; the commands are discord_bot/commands/inbox.py and
telegram_bot/commands/inbox.py; the inbound detours that call the two
handle_inbox_host_* functions are discord_bot/events.py and
telegram_bot/relay.py.
"""
import asyncio
import functools
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Message

import db
import message_relay
from backup_crypto import decrypt_secret
from message_relay import (
    build_telegram_text, clean_display_name, convert_discord_timestamps,
    escape_html, telegram_entities_to_discord,
)
from utils import (
    DEFAULT_LANG, SUPPORTED_LANGS, get_chat_lang, is_admin, localized,
    rate_limit_ok,
)

logger = logging.getLogger("bridge.inbox")

INBOX_SILENCE_SECONDS = 30 * 86400
INBOX_MEDIA_GROUP_DELAY = 1.0
INBOX_DEADTOPIC_DAYS = 3

INBOX_STATUS_MARKS = {"user": "\U0001F7E9", "staff": "\U0001F7E8", "closed": "⬛"}

_runtimes = {}
_media_group_buffer = {}

def conversation_title(status, name):
    """The name a conversation's thread and topic carry: a status mark, then
    the writer's display name.

    The mark says whose turn it is at a glance down a channel list — 🟩 the
    writer wrote last and is waiting, 🟨 staff answered last, ⬛ closed. It
    leads the name rather than trailing it so the colour lines up in the
    sidebar whatever the names are. Clipped to 90 characters, under both
    platforms' limits (Discord 100, Telegram 128)."""
    mark = INBOX_STATUS_MARKS.get(status, INBOX_STATUS_MARKS["user"])
    return f"{mark} {clean_display_name(name, max_len=88)}"

def inbox_bot_instance(bot_id):
    """The running aiogram Bot of a registered receiver bot, or None when it
    is not polling (bad token, or the process has not started it yet). Every
    delivery into a private chat goes through this — the main bot cannot
    reach a conversation that belongs to another token."""
    runtime = _runtimes.get(str(bot_id))
    return runtime["bot"] if runtime else None

def inbox_bot_place_name(bot_row):
    """How a receiver bot is named to admins — in `/inboxlist`, in command
    replies and in the service log: its @username, falling back to its display
    name. Not what relayed messages are labelled with; that is
    inbox_place_name."""
    username = bot_row["username"] if bot_row else None
    if username:
        return f"@{username}"
    return (bot_row["title"] if bot_row else None) or "Telegram bot"

def inbox_place_name(bridge_id):
    """The community half of the ``[Telegram | DM] Name:`` header staff see on
    a message out of a private chat: the localized word for a direct message,
    exactly as the appeal system labels an appellant's DM.

    There is no community to name — the writer is one person in a private
    chat — and 'DM' is the thing worth saying about them: it marks the message
    as coming from outside, from someone who is not a member of any bridged
    chat. Rendered once, in the language of the host chats that will read it
    (relay_message takes one place name for every target), and in the default
    language when a conversation somehow has no host left."""
    for chat in db.get_bridge_chats(bridge_id):
        if chat["platform"] != "inbox":
            return localized("inbox_dm_place", get_chat_lang(chat["chat_id"]))
    return localized("inbox_dm_place", DEFAULT_LANG)

def _locale_to_lang(code):
    """Map a Telegram client language ('ru', 'en-US') to a supported bot
    language. Someone writing to a receiver bot has no chat whose /lang could
    be read, so their own client is the only signal for how to answer them."""
    code = str(code or "").lower()[:2]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG

def inbox_staff_label(bridge_id, platform, user_id):
    """How a staff member is signed in the copy the writer receives while the
    receiver bot has anonymization on: the stable 'Staff A', 'Staff B', …

    Not localized, and built from the English wording and the Latin alphabet
    whatever language the writer reads in, for the reason the appeal system's
    consul labels are (discord_bot/appeals.py: _consul_label): the label
    identifies one person across a conversation rather than saying anything,
    and someone who is 'Staff B' to one writer and 'Персонал Б' to another is
    harder to talk about than someone who is 'Staff B' to everybody. The
    alphabet is shared with the consul labels — one key, one alphabet."""
    idx = db.get_inbox_staff_ord(bridge_id, platform, user_id)
    letters = localized("appeal_consul_letters", DEFAULT_LANG)
    letter = letters[idx] if isinstance(letters, str) and idx < len(letters) else str(idx + 1)
    return localized("inbox_staff_name", DEFAULT_LANG, letter=letter)

def touch_inbox_bridge(bridge_id):
    """Restart the silence window of the conversation a bridge belongs to.

    Called for staff messages the way relay_inbox_message calls it for the
    writer's: a conversation where only the staff side has spoken lately is
    just as alive as one where only the writer has, and the 30-day sweep must
    not close it out from under them.

    The deadtopic bookkeeping of its Discord threads is refreshed with it.
    on_message already does that for messages people write there, but a copy
    relayed *into* the thread is the bot's own message and would not count —
    a conversation carried entirely by the writer would then collect phantom
    messages it does not need."""
    conv = db.get_inbox_conversation_by_bridge(bridge_id)
    if conv:
        db.touch_inbox_conversation(conv["bot_id"], conv["user_id"])
    db.cur.execute(
        """
        UPDATE deadtopic_chats SET last_message_ts=strftime('%s','now')
        WHERE chat_id IN (SELECT chat_id FROM chats WHERE bridge_id=? AND platform='discord')
        """,
        (bridge_id,)
    )
    db.conn.commit()

async def mark_inbox_conversation(bridge_id, status):
    """Move a conversation's mark to 🟩 (the writer spoke) or 🟨 (staff did)
    and rename its thread and topic — but only when the mark actually changes.

    Discord allows a thread two renames per ten minutes, so a conversation
    going back and forth would spend that budget within a minute if every
    message renamed. Off the change, a busy exchange renames twice: once when
    the writer takes the floor and once when staff take it back."""
    conv = db.get_inbox_conversation_by_bridge(bridge_id)
    if conv is None:
        return
    if not db.set_inbox_conversation_status(conv["bot_id"], conv["user_id"], status):
        return
    await _rename_conversation_chats(bridge_id, conversation_title(status, conv["title"]))

async def _rename_conversation_chats(bridge_id, title):
    """Rename every thread and topic of a conversation.

    Failures are swallowed on purpose: a rename that Discord rate-limits away
    leaves the previous mark standing, which is stale but harmless, and must
    not cost the message that triggered it."""
    for chat in db.get_bridge_chats(bridge_id):
        try:
            if chat["platform"] == "discord":
                from discord_bot import resolve_discord_chat_channel
                thread = await resolve_discord_chat_channel(chat["chat_id"])
                if thread is not None:
                    await thread.edit(name=title)
            elif chat["platform"] == "telegram":
                from telegram_bot import bot as tg_bot
                group_id, thread_id = chat["chat_id"].split(":")
                await tg_bot.edit_forum_topic(
                    chat_id=int(group_id), message_thread_id=int(thread_id), name=title
                )
        except Exception as e:
            logger.info("inbox rename skipped (%s): %s", chat["chat_id"], e)

def inbox_sender_override(bridge_id, platform, user_id):
    """``(sender_name, avatar_url)`` to relay a staff message under, or
    ``(None, None)`` when the conversation's bot does not anonymize.

    The substitution happens at the origin, so every copy of the message
    carries the label — the private chat and any other host chat alike. That
    is the appeal system's arrangement too, and the alternative (naming staff
    to each other but not to the writer) would need a per-target sender name
    the relay core does not carry."""
    conv = db.get_inbox_conversation_by_bridge(bridge_id)
    if not conv:
        return None, None
    bot_row = db.get_inbox_bot(conv["bot_id"])
    if not bot_row or not bot_row["anonymize"]:
        return None, None
    return inbox_staff_label(bridge_id, platform, user_id), ""

def can_manage_inbox_bot(bot_row, platform, user_id):
    """Whether someone may reconfigure a receiver bot.

    Bot Admins always, plus whoever handed the token in — they answer for
    that bot, so they may refresh its token, name more host chats for it, run
    its bans and unregister it. Ownership is recorded per platform: the same
    number can name a Discord user and a Telegram one, and matching only the
    id would let a stranger inherit somebody's bot."""
    if is_admin(platform, user_id):
        return True
    if bot_row is None:
        return False
    return (str(bot_row["owner_platform"]) == str(platform)
            and str(bot_row["owner_id"]) == str(user_id))

def inbox_conversation_of_chat(chat_id):
    """The conversation a thread or topic *is*, or None when the chat is an
    ordinary bridged chat. This is what lets `/close` and `/inboxban`
    work with no arguments: run in the right place, the chat names its own
    subject."""
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if not row or not db.is_inbox_bridge(row["bridge_id"]):
        return None
    return db.get_inbox_conversation_by_bridge(row["bridge_id"])

def inbox_bot_for_chat(chat_id):
    """The receiver bot a chat is about: the one whose conversation it is, or
    the one it hosts. None when the chat says nothing — or, for a chat
    hosting several bots, when it says more than one thing and the command
    has to be told which."""
    conv = inbox_conversation_of_chat(chat_id)
    if conv:
        return db.get_inbox_bot(conv["bot_id"])
    hosts = db.get_inbox_hosts_of_chat(chat_id)
    if len(hosts) == 1:
        return db.get_inbox_bot(hosts[0]["bot_id"])
    return None

async def resolve_inbox_user(bot_id, identifier):
    """Turn what an admin typed — a numeric id or an @username — into a
    Telegram user id, asking the receiver bot itself about the name. Returns
    None when it cannot be resolved."""
    raw = str(identifier or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    bot = inbox_bot_instance(bot_id)
    if bot is None:
        return None
    try:
        chat = await bot.get_chat(raw if raw.startswith("@") else f"@{raw}")
        return chat.id
    except Exception:
        return None

async def register_inbox_bot(token, owner_platform, owner_id):
    """Validate a bot token, store it encrypted and bring the bot online.

    Returns ``(bot_row, error_key, was_update)``. The token is checked against
    Telegram before anything is written, so a typo never becomes a registered
    bot that cannot poll; it is encrypted on the way into the database and is
    not logged, echoed or put in any reply.

    Two different permissions meet here, which is why the check cannot live
    in the command: registering a *new* bot is a Bot Admin's business, while
    rotating the token of one already registered is also its owner's. Which
    of the two a call is only becomes clear after Telegram has been asked
    whose token it is."""
    from backup_crypto import encrypt_secret, secrets_available

    if not secrets_available():
        return None, "setinbox_no_key", False

    try:
        probe = Bot(token)
    except Exception:
        return None, "setinbox_invalid_token", False
    try:
        me = await probe.get_me()
    except Exception:
        return None, "setinbox_invalid_token", False
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass

    existing = db.get_inbox_bot(me.id)
    if existing is None:
        if not is_admin(owner_platform, owner_id):
            return None, "no_permission", False
    elif not can_manage_inbox_bot(existing, owner_platform, owner_id):
        return None, "no_permission", True

    db.add_inbox_bot(
        me.id, me.username, getattr(me, "full_name", None) or me.username,
        encrypt_secret(token), owner_platform, owner_id,
    )
    row = db.get_inbox_bot(me.id)
    started = await restart_inbox_bot(row)
    return row, (None if started else "setinbox_start_failed"), existing is not None

async def unregister_inbox_bot(bot_row):
    """Take a receiver bot out of service: close every open conversation,
    stop its polling and drop its rows. The conversations are closed first so
    both sides get the notice while the bridge still exists."""
    bot_id = str(bot_row["bot_id"])
    await close_inbox_conversations_of_bot(bot_id)
    await stop_inbox_bot(bot_id)
    db.remove_inbox_bot(bot_id)

async def ban_inbox_user(bot_row, user_id, banned_by):
    """Bar someone from a receiver bot: record the ban, tell them once, and
    close the conversation they had. The notice goes out before the close so
    it is the last thing they get from that bot rather than being lost among
    the closing note."""
    bot_id = str(bot_row["bot_id"])
    db.add_inbox_ban(bot_id, user_id, banned_by)

    conv = db.get_inbox_conversation(bot_id, user_id)
    bot = inbox_bot_instance(bot_id)
    if bot is not None:
        lang = (conv["lang"] if conv and conv["lang"] in SUPPORTED_LANGS else DEFAULT_LANG)
        try:
            await bot.send_message(int(user_id), localized("inbox_banned_user", lang))
        except Exception:
            pass
    if conv:
        await close_inbox_conversation(conv, notify_user=False)

def _is_start_command(text):
    """Whether the text is Telegram's own `/start` — the update a client
    sends when someone opens a chat with a bot and presses the button.

    It is the *only* command a receiver bot recognizes. Anything else
    beginning with a slash is relayed as the text it is, which is what keeps
    `/whois` from working from this side: the writer may be looked up by
    staff, but has no command surface of their own to look anyone up with."""
    head = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return head.split("@", 1)[0] == "/start"

def _build_inbox_dispatcher():
    """A Dispatcher serving one receiver bot.

    One per bot rather than one shared: aiogram guards `start_polling` with a
    per-dispatcher lock, so a single Dispatcher cannot poll several bots from
    several tasks, and a single task polling them all would stop them all
    together whenever one token is replaced."""
    dp = Dispatcher()
    dp.message.register(_on_inbox_message)
    dp.edited_message.register(_on_inbox_edited_message)
    return dp

async def start_inbox_bot(bot_row):
    """Bring a registered receiver bot online: decrypt its token, build its
    dispatcher and start polling in a task of its own. Returns whether it
    started; a token that no longer decrypts (BACKUP_KEY changed) or that
    Telegram rejects leaves the bot registered but silent, which is what
    /inboxlist reports."""
    bot_id = str(bot_row["bot_id"])
    if bot_id in _runtimes:
        return True
    try:
        token = decrypt_secret(bot_row["token"])
    except Exception:
        logger.warning("inbox bot %s: the stored token could not be decrypted", bot_id)
        return False

    bot = Bot(token)
    dp = _build_inbox_dispatcher()
    task = asyncio.create_task(_poll_inbox_bot(bot_id, bot, dp))
    _runtimes[bot_id] = {"bot": bot, "dp": dp, "task": task}
    return True

async def _poll_inbox_bot(bot_id, bot, dp):
    """Long-poll one receiver bot until it is stopped.

    A bot whose token was revoked raises out of here; it is reported once to
    the service chats and its runtime is forgotten, so /setinbox with a fresh
    token can start it again without a restart."""
    try:
        await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("inbox bot %s stopped polling: %s", bot_id, e)
        _runtimes.pop(bot_id, None)
        try:
            await bot.session.close()
        except Exception:
            pass
        from utils import log_error
        name = inbox_bot_place_name(db.get_inbox_bot(bot_id))
        await log_error(f"inbox bot {name} stopped polling: {type(e).__name__}")

async def stop_inbox_bot(bot_id):
    """Take a receiver bot offline and close its session. Safe to call for a
    bot that was never started."""
    runtime = _runtimes.pop(str(bot_id), None)
    if not runtime:
        return
    runtime["task"].cancel()
    try:
        await runtime["task"]
    except Exception:
        pass
    try:
        await runtime["bot"].session.close()
    except Exception:
        pass

async def restart_inbox_bot(bot_row):
    """Restart a receiver bot under a token that has just changed."""
    await stop_inbox_bot(bot_row["bot_id"])
    return await start_inbox_bot(bot_row)

async def start_all_inbox_bots():
    """Start every registered receiver bot — called once from main() after
    the schema is up."""
    for row in db.get_inbox_bots():
        try:
            await start_inbox_bot(row)
        except Exception as e:
            logger.warning("inbox bot %s failed to start: %s", row["bot_id"], e)

async def _on_inbox_message(message: Message):
    """Front door of a receiver bot's private chat.

    The ladder is short by design: only private chats (a receiver bot added
    to a group is not this feature), never other bots, `/start` answered with
    the greeting that states what forwarding means, then the bot's own ban
    list, the bot-wide shadow bans and the per-user rate limit. Everything
    surviving that opens or continues a conversation. An album is buffered
    the way the main relay buffers one, so its parts arrive as a single
    message rather than as one copy per file."""
    if getattr(message.chat, "type", None) != "private":
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    bot_id = str(message.bot.id)
    bot_row = db.get_inbox_bot(bot_id)
    if not bot_row:
        return

    user_id = str(user.id)
    if _is_start_command(getattr(message, "text", "") or ""):
        await _send_inbox_greeting(message, bot_row)
        return

    if db.is_inbox_banned(bot_id, user_id):
        return
    if db.is_shadow_banned("telegram", user_id):
        return
    if not rate_limit_ok(("inbox", bot_id, user_id), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping inbox message from %s to bot %s", user_id, bot_id)
        return

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        from telegram_bot.files import _count_telegram_files
        files = _count_telegram_files(message)
        if files > 0:
            _buffer_inbox_media_group(bot_id, user_id, media_group_id, message, files)
            return

    await _open_and_relay(bot_row, message)

def _buffer_inbox_media_group(bot_id, user_id, media_group_id, message, files):
    """Collect the parts of an album and re-arm the flush task.

    Every part cancels and re-arms it, so the wait measures the gap between
    parts rather than the age of the album. The part carrying the caption —
    or failing that the earliest — is the one whose text the copy will use;
    every part is kept, because each carries one of the files."""
    key = (bot_id, user_id, str(media_group_id))
    payload = _media_group_buffer.get(key)
    if not payload:
        payload = {"message": message, "count": 0, "task": None, "messages": []}
        _media_group_buffer[key] = payload

    payload["count"] += files
    payload["messages"].append(message)
    if getattr(message, "caption", None) and not getattr(payload["message"], "caption", None):
        payload["message"] = message
    elif message.message_id < payload["message"].message_id:
        payload["message"] = message

    if payload.get("task"):
        payload["task"].cancel()
    payload["task"] = asyncio.create_task(_flush_inbox_media_group(key))

async def _flush_inbox_media_group(key):
    """Relay a buffered album once a second has passed with no new part."""
    try:
        await asyncio.sleep(INBOX_MEDIA_GROUP_DELAY)
    except asyncio.CancelledError:
        return
    payload = _media_group_buffer.pop(key, None)
    if not payload:
        return
    bot_row = db.get_inbox_bot(key[0])
    if not bot_row:
        return
    try:
        await _open_and_relay(
            bot_row, payload["message"],
            grouped_messages=payload["messages"], grouped_file_count=payload["count"],
        )
    except Exception as e:
        logger.warning("inbox album relay failed (bot=%s, user=%s): %s", key[0], key[1], e)

async def _open_and_relay(bot_row, message: Message, grouped_messages=None, grouped_file_count=None):
    """Make sure the writer has an open conversation, then relay into it and
    move the mark to 🟩 — they have the floor until somebody answers."""
    bot_id = str(bot_row["bot_id"])
    user = message.from_user
    conv = db.get_inbox_conversation(bot_id, str(user.id))
    if conv is None:
        conv, error_key = await open_inbox_conversation(bot_row, user)
        if conv is None:
            lang = _locale_to_lang(getattr(user, "language_code", None))
            try:
                await message.answer(localized(error_key, lang))
            except Exception:
                pass
            return
    await relay_inbox_message(
        bot_row, conv, message,
        grouped_messages=grouped_messages, grouped_file_count=grouped_file_count,
    )
    await mark_inbox_conversation(conv["bridge_id"], "user")

async def _send_inbox_greeting(message: Message, bot_row):
    """Answer `/start` with the text that makes writing here informed
    consent: it names what happens to a message sent to this bot."""
    lang = _locale_to_lang(getattr(message.from_user, "language_code", None))
    try:
        await message.answer(
            localized("inbox_greeting", lang, bot=inbox_bot_place_name(bot_row)),
            parse_mode=None,
        )
    except Exception:
        pass

async def open_inbox_conversation(bot_row, user):
    """Open a conversation: claim a bridge, attach the private chat, then a
    thread or topic in every host chat.

    Returns ``(conversation_row, error_key)``. Hosts that fail individually
    are skipped rather than fatal — one Discord channel the bot lost access
    to must not keep the Telegram side of the conversation from existing —
    but a conversation with no chat at all is no conversation, so the claimed
    bridge is released and the failure named. A hole in the inbox range costs
    nothing.

    Reopening is asymmetric by design. A Telegram writer keeps their topic:
    it is closed rather than deleted when the conversation ends, and a later
    message reopens the same one, so a group does not accumulate a topic per
    exchange and the history stays in one place. Discord gets a fresh thread
    each time — an archived thread is left alone, and a channel reads better
    as a list of conversations than as a handful of threads reopened over
    months."""
    bot_id = str(bot_row["bot_id"])
    user_id = str(user.id)
    hosts = db.get_inbox_hosts(bot_id)
    if not hosts:
        return None, "inbox_not_configured"

    lang = _locale_to_lang(getattr(user, "language_code", None))
    bridge_id = db.claim_inbox_bridge_id()
    if bridge_id is None:
        return None, "inbox_open_failed"

    chat_key = db.inbox_chat_id(bot_id, user_id)
    db.attach_chat("inbox", chat_key, bridge_id)
    db.set_chat_lang(chat_key, lang)

    name = getattr(user, "full_name", None) or getattr(user, "username", None) or user_id
    title = conversation_title("user", name)

    opened = 0
    for host in hosts:
        try:
            if host["platform"] == "discord":
                opened += 1 if await _open_discord_thread(host, bridge_id, title, user) else 0
            elif host["platform"] == "telegram":
                opened += 1 if await _open_telegram_topic(
                    bot_id, user_id, host, bridge_id, title, user) else 0
        except Exception as e:
            logger.warning("inbox host %s could not be opened (bot=%s): %s",
                           host["chat_id"], bot_id, e)
            from utils import log_error
            await log_error(
                f"inbox host {host['chat_id']} could not be opened for "
                f"{inbox_bot_place_name(bot_row)}: {type(e).__name__} {e}"
            )

    if not opened:
        db.remove_chat_from_bridge(chat_key)
        return None, "inbox_open_failed"

    db.create_inbox_conversation(bot_id, user_id, bridge_id, lang, clean_display_name(name, max_len=88))
    return db.get_inbox_conversation(bot_id, user_id), None

def _conversation_info_text(host_chat_id, user):
    """The pinned first message of a thread or topic: who is on the other end
    and what writing here does. Rendered in the host chat's language, since
    it is staff who read it."""
    lang = get_chat_lang(host_chat_id)
    username = getattr(user, "username", None)
    return localized(
        "inbox_conversation_info", lang,
        name=clean_display_name(getattr(user, "full_name", None) or str(user.id)),
        username=f"@{username}" if username else "—",
        id=user.id,
    )

async def _open_discord_thread(host, bridge_id, title, user):
    """Open one Discord thread for a conversation and attach it to the
    bridge. A forum channel gets a forum post instead — same call, and the
    starter message is the info text rather than a follow-up."""
    import discord
    from discord_bot import bot as dc_bot

    guild_id, channel_id = host["chat_id"].split(":")
    channel = dc_bot.get_channel(int(channel_id))
    if channel is None:
        channel = await dc_bot.fetch_channel(int(channel_id))
    if channel is None:
        return False

    info = _conversation_info_text(host["chat_id"], user)
    if isinstance(channel, discord.ForumChannel):
        created = await channel.create_thread(name=title, content=info)
        thread, starter = created.thread, created.message
    else:
        thread = await channel.create_thread(
            name=title,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        starter = await thread.send(info)

    try:
        await starter.pin()
    except Exception:
        pass

    chat_key = f"{guild_id}:{thread.id}"
    db.attach_chat("discord", chat_key, bridge_id)
    _register_deadtopic(chat_key)
    return True

def _register_deadtopic(chat_key):
    """Keep a Discord conversation thread from being archived under the
    conversation.

    Discord archives a thread after its auto-archive window of silence, and a
    conversation waiting on an answer over a long weekend is exactly the case
    where that happens. The /deadtopic mechanic already solves this — a
    phantom message sent and deleted at once — so a conversation thread is
    registered with it automatically, at 3 days rather than the 6 the command
    sets: nobody enabled it here by hand, so it has to act before the window
    it is protecting against."""
    db.cur.execute(
        """
        INSERT INTO deadtopic_chats (chat_id, last_message_ts, bot_last_sent_ts, days)
        VALUES (?, strftime('%s','now'), 0, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_message_ts=excluded.last_message_ts,
            days=excluded.days
        """,
        (chat_key, INBOX_DEADTOPIC_DAYS)
    )
    db.conn.commit()

async def _open_telegram_topic(bot_id, user_id, host, bridge_id, title, user):
    """Open one Telegram forum topic for a conversation and attach it to the
    bridge — reopening the writer's previous topic in that group when they
    have one.

    Only a forum group can host: `/setinboxchat` refuses anywhere else and
    checks that the bot may manage topics, so reaching this with a plain
    group means something changed since, and the failure is reported by the
    caller. A remembered topic that can no longer be reopened (someone
    deleted it) is forgotten and replaced by a new one."""
    from telegram_bot import bot as tg_bot

    group_id = host["chat_id"].split(":")[0]
    thread_id = None

    remembered = db.get_inbox_topic(bot_id, user_id, host["chat_id"])
    if remembered:
        try:
            await tg_bot.reopen_forum_topic(
                chat_id=int(group_id), message_thread_id=int(remembered)
            )
            await tg_bot.edit_forum_topic(
                chat_id=int(group_id), message_thread_id=int(remembered), name=title
            )
            thread_id = int(remembered)
        except Exception as e:
            logger.info("inbox topic %s could not be reopened, making a new one: %s",
                        remembered, e)
            db.forget_inbox_topic(bot_id, user_id, host["chat_id"])

    if thread_id is None:
        topic = await tg_bot.create_forum_topic(chat_id=int(group_id), name=title)
        thread_id = topic.message_thread_id

    db.remember_inbox_topic(bot_id, user_id, host["chat_id"], thread_id)

    try:
        sent = await tg_bot.send_message(
            chat_id=int(group_id),
            message_thread_id=thread_id,
            text=escape_html(_conversation_info_text(host["chat_id"], user)),
            parse_mode="HTML",
        )
        await tg_bot.pin_chat_message(int(group_id), sent.message_id)
    except Exception:
        pass

    db.attach_chat("telegram", f"{group_id}:{thread_id}", bridge_id)
    return True

def _inbox_relay_texts(message: Message, grouped_file_count=None, gallery_uploaded=0):
    """The relay body of one message from a private chat, as
    ``(texts, relay_file_count)``.

    A shorter road than the main Telegram relay's in one way: no source link
    is offered, since a private chat has no addressable message. Files that
    GALLERY took are represented by their links (the caller attaches them);
    whatever it could not take falls back to the localized "[N files from
    Telegram]" marker. Stickers, voice messages and video notes keep their
    own markers, exactly as everywhere else."""
    from telegram_bot.files import _count_telegram_files

    if getattr(message, "sticker", None) is not None:
        return ["__TG_STICKER__"], None

    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    total = grouped_file_count if grouped_file_count is not None else _count_telegram_files(message)

    if not base_text and total == 1 and not gallery_uploaded:
        if getattr(message, "voice", None) is not None:
            return ["__TG_VOICE__"], None
        if getattr(message, "video_note", None) is not None:
            return ["__TG_VIDEO_NOTE__"], None

    remaining = total - gallery_uploaded
    if remaining <= 0:
        return [base_text], None

    prefix = (base_text + "\n") if base_text else ""
    return [prefix + f"[__TG_FILES_{remaining}__]"], remaining

def _inbox_message_variants(message: Message):
    """``(discord_markdown, telegram_html)`` for a message's own text, with
    its formatting carried across. Marker-only messages have neither."""
    source_text = getattr(message, "text", None)
    source_caption = getattr(message, "caption", None)
    if source_text is not None:
        return (telegram_entities_to_discord(source_text, getattr(message, "entities", None)),
                getattr(message, "html_text", None))
    if source_caption is not None:
        return (telegram_entities_to_discord(source_caption, getattr(message, "caption_entities", None)),
                getattr(message, "html_text", None))
    return None, None

def _inbox_reply_target(chat_key, message: Message):
    """The `messages` row a reply in a private chat points at, as the relay
    core wants it: a database id, or -1 for 'replying to something with no
    copy here'. ``None`` when the message is not a reply."""
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return None
    replied_id = str(replied.message_id)
    if replied.from_user and replied.from_user.is_bot:
        row = db.cur.execute(
            "SELECT message_id FROM message_copies"
            " WHERE platform='inbox' AND chat_id=? AND message_id_platform=?",
            (chat_key, replied_id)
        ).fetchone()
        return row["message_id"] if row else -1
    row = db.cur.execute(
        "SELECT id FROM messages"
        " WHERE origin_platform='inbox' AND origin_chat_id=? AND origin_message_id=?",
        (chat_key, replied_id)
    ).fetchone()
    return row["id"] if row else -1

def inbox_header_hidden_here(bot_id, chat):
    """Whether this host chat asked for bare copies (`/close-header`).

    Only the staff side is ever asked: the writer's own header was cut down to
    a name for everybody (inbox_writer_header), and there is nothing left
    there to hide."""
    return db.inbox_header_hidden(bot_id, chat["platform"], chat["chat_id"])

async def _inbox_send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html,
                              reply_line, reply_link_line=None, reply_to_platform_message_id=None,
                              sender_name=None, place_name=None, messenger_name=None,
                              avatar_url=None, is_bot_sender=False, bot_id=None):
    """Per-target delivery for a message that came out of a private chat: the
    host threads and topics, through the two ordinary deliverers.

    A host with `/close-header` on gets the body alone. The deliverers build
    their own header whenever they are handed a sender name, so suppressing it
    means withholding all three naming arguments *and* passing an empty
    header — half of that would leave one of the two paths still writing one."""
    hide = bot_id is not None and inbox_header_hidden_here(bot_id, chat)

    if chat["platform"] == "discord":
        from discord_bot import deliver_discord_relay
        return await deliver_discord_relay(
            chat, header="" if hide else header, body_discord=body_discord,
            reply_line=reply_line, reply_link_line=reply_link_line,
            reply_to_platform_message_id=reply_to_platform_message_id,
            sender_name=None if hide else sender_name,
            place_name=None if hide else place_name,
            messenger_name=None if hide else messenger_name,
            avatar_url=avatar_url, is_bot_sender=is_bot_sender,
        )
    if chat["platform"] == "telegram":
        from discord_bot import deliver_telegram_relay
        return await deliver_telegram_relay(
            chat, header="" if hide else header, body_plain=body_plain,
            body_telegram_html=body_telegram_html, reply_line=reply_line,
            reply_to_platform_message_id=reply_to_platform_message_id,
        )
    return None

async def _upload_inbox_files(bot_row, conv, messages):
    """Re-upload a private chat's attachments to GALLERY.

    Returns ``(upload, urls, uploaded_count, candidates)``, all empty when
    nothing was uploaded. The files are downloaded through the *receiver
    bot* — a file_id belongs to the token it was handed to, and the main
    bot's would get nothing for it — and the consent asked is the host
    chats' `/allow-files`, since they are the communities whose GALLERY the
    files land in (db.inbox_file_relay_enabled explains why the ordinary
    bridge-wide check cannot answer this)."""
    from telegram_bot.files import _collect_gallery_candidates, _upload_telegram_files_to_gallery

    candidates = []
    for msg in messages:
        candidates.extend(_collect_gallery_candidates(msg))
    if not candidates or not db.inbox_file_relay_enabled(conv["bridge_id"]):
        return None, None, 0, candidates

    source_bot = inbox_bot_instance(bot_row["bot_id"])
    if source_bot is None:
        return None, None, 0, candidates

    upload, uploaded = await _upload_telegram_files_to_gallery(candidates, source_bot=source_bot)
    if not upload:
        return None, None, 0, candidates
    return upload, upload["urls"], uploaded, candidates

async def relay_inbox_message(bot_row, conv, message: Message,
                              grouped_messages=None, grouped_file_count=None):
    """Relay one message from a private chat into its conversation bridge,
    re-uploading its attachments to GALLERY where the hosts allow it."""
    import json

    bot_id = str(bot_row["bot_id"])
    user = message.from_user
    chat_key = db.inbox_chat_id(bot_id, user.id)

    source_messages = sorted(grouped_messages or [message], key=lambda m: m.message_id)
    upload, gallery_urls, uploaded, candidates = await _upload_inbox_files(
        bot_row, conv, source_messages
    )

    texts, relay_file_count = _inbox_relay_texts(message, grouped_file_count, uploaded)
    discord_text, telegram_html = _inbox_message_variants(message)
    reply_to = _inbox_reply_target(chat_key, message)

    relayed_db_id = None
    for text in texts:
        relayed_db_id = await message_relay.relay_message(
            bridge_id=conv["bridge_id"],
            origin_platform="inbox",
            origin_chat_id=chat_key,
            origin_message_id=str(message.message_id),
            origin_sender_id=str(user.id),
            messenger_name="Telegram",
            place_name=inbox_place_name(conv["bridge_id"]),
            sender_name=getattr(user, "full_name", None) or str(user.id),
            text=text,
            discord_text=discord_text if discord_text is not None else text,
            telegram_html=telegram_html,
            reply_to_msg_db_id=reply_to,
            send_to_chat_func=functools.partial(_inbox_send_to_chat, bot_id=bot_id),
            telegram_file_count=relay_file_count,
            gallery_urls=gallery_urls,
        )

    if grouped_messages and relayed_db_id is not None:
        db.record_media_group_members(
            chat_key, [m.message_id for m in source_messages], relayed_db_id
        )

    if upload and relayed_db_id is not None:
        db.add_gallery_upload(
            relayed_db_id, upload["channel_id"], upload["message_id"],
            json.dumps(upload["urls"], ensure_ascii=False),
            json.dumps([str(m.message_id) for m in source_messages]),
            json.dumps([c["file_id"] for c in candidates], ensure_ascii=False),
        )

    touch_inbox_bridge(conv["bridge_id"])

def inbox_writer_header(sender_name):
    """The header a message carries in the writer's private chat: the sender's
    name and nothing else.

    They are talking to one team through one bot, so which platform the answer
    was typed on and which server it came from say nothing they can use — and
    naming the server would hand out an internal detail about the people
    answering, which the 'Staff A' anonymization is there to withhold. The
    staff side keeps the full ``[Messenger | DM] Name:`` header, because there
    the platform and the DM marker are exactly what distinguishes a
    conversation from a bridged chat."""
    return f"{clean_display_name(sender_name)}:" if sender_name else ""

async def deliver_inbox_relay(chat, *, header, body_plain, body_telegram_html, reply_line,
                              reply_to_platform_message_id, sender_name=None):
    """Deliver a relayed message into a receiver bot's private chat.

    The Telegram deliverer's twin, and different from it in three ways: it
    sends through the conversation's own bot rather than the main one, it
    rewrites the header down to the sender's name (inbox_writer_header), and
    a send that fails returns None instead of raising — a writer who blocked
    the bot or deleted their account must cost the conversation one copy, not
    the whole fan-out to the other host chats."""
    bot_id, user_id = chat["chat_id"].split(":")
    bot = inbox_bot_instance(bot_id)
    if bot is None:
        return None

    lang = get_chat_lang(chat["chat_id"])
    body_html = convert_discord_timestamps(body_telegram_html or escape_html(body_plain), lang)
    body_plain_local = convert_discord_timestamps(body_plain, lang)
    if reply_line:
        body_html = f"{escape_html(reply_line)}\n{body_html}"

    own_header = inbox_writer_header(sender_name) if sender_name is not None else header
    send_kwargs = dict(
        chat_id=int(user_id),
        text=build_telegram_text(own_header, body_html, body_plain_local),
        parse_mode="HTML",
    )
    if reply_to_platform_message_id:
        send_kwargs["reply_to_message_id"] = int(reply_to_platform_message_id)
    try:
        sent = await bot.send_message(**send_kwargs)
    except Exception:
        send_kwargs.pop("reply_to_message_id", None)
        try:
            sent = await bot.send_message(**send_kwargs)
        except Exception as e:
            logger.warning("inbox delivery failed (chat=%s): %s", chat["chat_id"], e)
            return None
    return str(sent.message_id)

async def edit_inbox_relay_copy(chat_id, message_id_platform, sender_name, body_plain, body_html):
    """Rewrite a copy living in a private chat after its origin was edited.

    Takes the sender's name rather than a ready header, because the header the
    other chats show is not the one this chat shows — see inbox_writer_header."""
    bot_id, user_id = str(chat_id).split(":")
    bot = inbox_bot_instance(bot_id)
    if bot is None:
        return
    lang = get_chat_lang(chat_id)
    try:
        await bot.edit_message_text(
            chat_id=int(user_id),
            message_id=int(message_id_platform),
            text=build_telegram_text(
                inbox_writer_header(sender_name),
                convert_discord_timestamps(body_html or escape_html(body_plain), lang),
                convert_discord_timestamps(body_plain, lang),
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

async def delete_inbox_message(chat_id, message_id_platform):
    """Delete a message in a private chat — a copy whose origin was deleted,
    or the origin of a copy someone deleted in a thread."""
    bot_id, user_id = str(chat_id).split(":")
    bot = inbox_bot_instance(bot_id)
    if bot is None:
        return
    try:
        await bot.delete_message(int(user_id), int(message_id_platform))
    except Exception:
        pass

async def inbox_whois_profile(origin_chat_id, sender_id):
    """``(nickname, username, bio)`` of the person behind a message that came
    out of a private chat, for the whois answer.

    Asked through that conversation's own bot: the main Telegram bot has
    never met this user and `get_chat` on a stranger tells it nothing. Every
    field falls back to an em dash, so a bot that is offline degrades the
    answer instead of failing it."""
    nickname = username = bio = "—"
    bot = inbox_bot_instance(str(origin_chat_id).split(":", 1)[0])
    if bot is None:
        return nickname, username, bio
    try:
        chat = await bot.get_chat(int(sender_id))
    except Exception:
        return nickname, username, bio
    nickname = getattr(chat, "full_name", None) or getattr(chat, "first_name", None) or "—"
    if getattr(chat, "username", None):
        username = f"@{chat.username}"
    bio = getattr(chat, "bio", None) or "—"
    return nickname, username, bio

async def _on_inbox_edited_message(message: Message):
    """Propagate an edit made in a private chat into every copy of it.

    Mirrors telegram_bot/relay.py: edited_message_handler, minus everything
    about GALLERY — an inbox message never has an upload to re-sync."""
    if getattr(message.chat, "type", None) != "private":
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    bot_id = str(message.bot.id)
    bot_row = db.get_inbox_bot(bot_id)
    if not bot_row:
        return

    chat_key = db.inbox_chat_id(bot_id, user.id)
    row = db.cur.execute(
        """
        SELECT id FROM messages
        WHERE origin_platform='inbox' AND origin_chat_id=? AND origin_message_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (chat_key, str(message.message_id))
    ).fetchone()
    if not row:
        return

    conv = db.get_inbox_conversation(bot_id, user.id)
    if conv is None:
        return

    texts, relay_file_count = _inbox_relay_texts(message)
    rendered = texts[0] if texts else ""
    discord_text, telegram_html = _inbox_message_variants(message)
    header = (f"[Telegram | {clean_display_name(inbox_place_name(conv['bridge_id']))}]"
              f" {clean_display_name(getattr(user, 'full_name', None) or str(user.id))}:")

    await _propagate_inbox_edit(
        bot_id, row["id"], header, rendered,
        discord_text if discord_text is not None else rendered,
        telegram_html, relay_file_count,
    )

async def _propagate_inbox_edit(bot_id, message_db_id, header, body_plain, body_discord,
                                body_html, relay_file_count):
    """Rewrite every copy of an edited private-chat message, localizing the
    file-count marker per target chat and honouring each host's
    `/close-header` — an edit must not put back a header the host turned off."""
    from utils import localized_file_count_text

    copies = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=? AND COALESCE(kind,'main')='main'",
        (message_db_id,)
    ).fetchall()

    for copy in copies:
        target_lang = get_chat_lang(copy["chat_id"])
        copy_header = "" if inbox_header_hidden_here(bot_id, copy) else header
        plain, discord_body, html = body_plain, body_discord, body_html
        if relay_file_count is not None:
            marker = localized_file_count_text(relay_file_count, target_lang)
            token = f"__TG_FILES_{relay_file_count}__"
            plain = plain.replace(token, marker)
            discord_body = discord_body.replace(token, marker)
            if html is not None:
                html = html.replace(token, escape_html(marker))
        try:
            if copy["platform"] == "discord":
                from discord_bot import edit_discord_relay_copy, resolve_discord_chat_channel
                channel = await resolve_discord_chat_channel(copy["chat_id"])
                if channel is None:
                    continue
                await edit_discord_relay_copy(
                    channel, copy["message_id_platform"], copy_header, discord_body,
                    message_db_id=message_db_id, chat=copy,
                )
            elif copy["platform"] == "telegram":
                from telegram_bot import bot as tg_bot
                chat_id_str, _ = copy["chat_id"].split(":")
                await tg_bot.edit_message_text(
                    chat_id=int(chat_id_str),
                    message_id=int(copy["message_id_platform"]),
                    text=build_telegram_text(copy_header, html or escape_html(plain), plain),
                    parse_mode="HTML",
                )
        except Exception:
            pass

async def handle_inbox_host_discord_message(message, bridge_id):
    """Relay a staff message written in a Discord conversation thread.

    No consent prompt: answering an inbox is staff work in a staff channel,
    not a bridged community chat. The sender name is replaced by the 'Staff
    A' label — and the avatar dropped with it, so nothing about them leaks
    through a webhook copy either — while the conversation's bot anonymizes."""
    from discord_bot.relay import _relay_verified_discord_message

    if db.is_shadow_banned("discord", str(message.author.id)):
        try:
            await message.delete()
        except Exception:
            pass
        return
    if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping inbox thread message from %s", message.author.id)
        return

    sender_name, avatar_url = inbox_sender_override(bridge_id, "discord", message.author.id)
    await _relay_verified_discord_message(
        message, bridge_id, sender_name=sender_name, avatar_url=avatar_url,
    )
    touch_inbox_bridge(bridge_id)
    await mark_inbox_conversation(bridge_id, "staff")

async def close_inbox_conversation(conv, *, notify_user=True):
    """Close a conversation: tell both sides, archive the thread, close the
    topic, then detach every chat of its bridge and drop the record.

    The notices go out before the detachment so they still land in chats that
    are part of the bridge; the bridge row itself disappears with its last
    chat (db.remove_chat_from_bridge). Every side effect is separately
    fail-safed — a thread the bot can no longer reach must not keep the
    record alive."""
    bot_id = str(conv["bot_id"])
    user_id = str(conv["user_id"])
    bridge_id = int(conv["bridge_id"])
    chats = db.get_bridge_chats(bridge_id)

    db.set_inbox_conversation_status(bot_id, user_id, "closed")
    await _rename_conversation_chats(bridge_id, conversation_title("closed", conv["title"]))

    for chat in chats:
        try:
            if chat["platform"] == "discord":
                await _close_discord_thread(chat["chat_id"])
            elif chat["platform"] == "telegram":
                await _close_telegram_topic(chat["chat_id"])
        except Exception:
            pass

    if notify_user:
        bot = inbox_bot_instance(bot_id)
        if bot is not None:
            lang = conv["lang"] if conv["lang"] in SUPPORTED_LANGS else DEFAULT_LANG
            try:
                await bot.send_message(int(user_id), localized("inbox_closed_user", lang))
            except Exception:
                pass

    for chat in chats:
        db.remove_chat_from_bridge(chat["chat_id"])
    db.delete_inbox_conversation(bot_id, user_id)

async def _close_discord_thread(chat_id):
    """Post the closing note in a conversation thread, then archive and lock
    it. Its deadtopic registration goes too — an archived thread wants no
    keep-alive, and the next conversation gets a thread of its own."""
    from discord_bot import resolve_discord_chat_channel

    db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id=?", (chat_id,))
    db.conn.commit()

    thread = await resolve_discord_chat_channel(chat_id)
    if thread is None:
        return
    try:
        await thread.send(localized("inbox_closed_chat", get_chat_lang(chat_id)))
    except Exception:
        pass
    try:
        await thread.edit(archived=True, locked=True)
    except Exception:
        pass

async def _close_telegram_topic(chat_id):
    """Post the closing note in a conversation topic, then close the topic.

    Closed, not deleted: the topic stays in the group with its history and
    its ⬛ mark, and the writer's next message reopens this same one
    (db.get_inbox_topic remembers which)."""
    from telegram_bot import bot as tg_bot

    group_id, thread_id = chat_id.split(":")
    try:
        await tg_bot.send_message(
            chat_id=int(group_id),
            message_thread_id=int(thread_id) or None,
            text=localized("inbox_closed_chat", get_chat_lang(chat_id)),
        )
    except Exception:
        pass
    try:
        await tg_bot.close_forum_topic(chat_id=int(group_id), message_thread_id=int(thread_id))
    except Exception:
        pass

async def close_inbox_conversations_of_bot(bot_id, *, notify_user=True):
    """Close every open conversation of one receiver bot — what unregistering
    it and banning through it both end in."""
    for conv in db.get_inbox_conversations_of_bot(bot_id):
        try:
            await close_inbox_conversation(conv, notify_user=notify_user)
        except Exception as e:
            logger.warning("inbox conversation %s could not be closed: %s", conv["bridge_id"], e)

async def inbox_maintenance_pass():
    """Close conversations nobody has written in for 30 days.

    The window matches how long a relayed message is kept (cleanup_old_messages):
    past it the thread can no longer edit or delete what it holds anyway, so
    keeping the bridge open buys nothing. The writer's next message simply
    opens a fresh conversation."""
    for conv in db.get_silent_inbox_conversations(INBOX_SILENCE_SECONDS):
        try:
            await close_inbox_conversation(conv)
        except Exception as e:
            logger.warning("inbox maintenance could not close %s: %s", conv["bridge_id"], e)
