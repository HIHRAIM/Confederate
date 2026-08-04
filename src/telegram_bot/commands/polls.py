"""Polls on Telegram: the /poll command, the vote callback, and the two
renderers the Discord side calls when it posts a poll into a Telegram chat
(build_poll_keyboard, poll_start_text_telegram).

The poll itself lives in the database and is published to every chat of the
bridge by discord_bot/commands/polls.py: publish_poll — whichever platform
created it.
"""
import json
import time

from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import db
from utils import DEFAULT_LANG, get_chat_lang, localized

from telegram_bot.client import router

def build_poll_keyboard(poll_id, options):
    """The vote keyboard of one poll: one button per option, callback data
    'poll:<id>:<index>'. Long options are clipped to keep the button legible."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    rows = []
    for idx, opt in enumerate(options):
        label = opt if len(opt) <= 60 else opt[:59] + "…"
        rows.append([InlineKeyboardButton(text=f"{idx + 1}. {label}", callback_data=f"poll:{poll_id}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def poll_start_text_telegram(question, options, ends_at, lang):
    """The Telegram rendering of a poll. The end time is spelled out in UTC —
    Telegram has no equivalent of Discord's self-localizing timestamps."""
    from datetime import datetime, timezone
    lines = [f"📊 {question}", localized("poll_anonymous", lang), ""]
    for i, opt in enumerate(options):
        lines.append(f"{i + 1}. {opt}")
    lines.append("")
    ends = datetime.fromtimestamp(ends_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(localized("poll_ends", lang, ends=ends))
    return "\n".join(lines)

@router.message(Command("poll"))
async def poll_cmd(message: Message):
    """Create a bridge-wide poll from a pipe-separated argument:
    `/poll question | duration | option | option …` (2–10 options). The poll
    message is then published to every chat of the bridge, this one
    included."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key) or DEFAULT_LANG

    parts_cmd = (message.text or "").split(maxsplit=1)
    if len(parts_cmd) < 2:
        await message.reply(localized("poll_usage_telegram", lang))
        return
    segments = [s.strip() for s in parts_cmd[1].split("|")]
    if len(segments) < 4 or not segments[0]:
        await message.reply(localized("poll_usage_telegram", lang))
        return

    question = segments[0]
    time_str = segments[1]
    options = [s for s in segments[2:] if s][:10]
    if len(options) < 2:
        await message.reply(localized("poll_too_few", lang))
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await message.reply(localized("poll_not_in_bridge", lang))
        return
    bridge_id = row["bridge_id"]

    from utils import parse_poll_duration
    try:
        seconds = parse_poll_duration(time_str)
    except ValueError:
        await message.reply(localized("poll_duration_invalid", lang))
        return

    ends_at = int(time.time()) + seconds
    poll_id = db.create_poll(bridge_id, question, json.dumps(options, ensure_ascii=False), ends_at)
    place = message.chat.title or "Telegram"
    nick = message.from_user.full_name if message.from_user else "Unknown"
    from discord_bot import publish_poll
    await publish_poll(
        poll_id, bridge_id, question, options, ends_at,
        origin_chat_id=chat_key, origin_platform="telegram",
        origin_place=place, origin_nick=nick,
    )

@router.callback_query(lambda c: c.data and c.data.startswith("poll:"))
async def handle_poll_callback(query: CallbackQuery):
    """Register a Telegram vote: the poll must still be open and the voter
    verified (the vote crosses community lines like a message would).
    Re-voting replaces the previous choice."""
    try:
        _, pid_s, idx_s = query.data.split(":")
        poll_id = int(pid_s)
        idx = int(idx_s)
    except Exception:
        await query.answer()
        return

    chat = query.message.chat if query.message else None
    thread = (query.message.message_thread_id or 0) if query.message else 0
    lang = get_chat_lang(f"{chat.id}:{thread}") if chat else DEFAULT_LANG

    poll = db.get_poll(poll_id)
    if not poll or poll["closed"] or (poll["ends_at"] and poll["ends_at"] <= int(time.time())):
        await query.answer(localized("poll_closed", lang), show_alert=True)
        return

    user_id = str(query.from_user.id)
    prefix = str(chat.id) if chat else ""
    if not db.is_user_verified("telegram", user_id, prefix):
        await query.answer(localized("poll_not_verified", lang), show_alert=True)
        return

    db.record_poll_vote(poll_id, "telegram", user_id, idx)
    await query.answer(localized("poll_vote_recorded", lang))
