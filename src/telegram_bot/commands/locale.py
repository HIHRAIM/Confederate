"""Localization commands on Telegram: /locale, /loc_compare, /loc_suggest
and the admin's /loc_reply.

The support-chat posting itself is shared with the Discord half
(discord_bot/commands/locale.py), so a suggestion filed here reaches the same
channels and can be answered from either platform.
"""
import logging
import os
import secrets

from aiogram.filters import Command
from aiogram.types import Message

import db
import utils
from config import SUPPORT_CHATS
from utils import (
    DEFAULT_LANG, LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS,
    available_locales, compare_reply, get_chat_lang, is_admin, language_name,
    locale_bar, locale_stats, localized, rate_limit_ok,
)

from telegram_bot.client import bot, router, username_of

logger = logging.getLogger("bridge.telegram")

@router.message(Command("locale"))
async def locale_cmd(message: Message):
    """Without an argument: the translation-status bar per language. With a
    language code: that language's i18n file, rate-limited per chat."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    ui_lang = get_chat_lang(chat_key)

    parts = message.text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else None

    if not arg:
        lines = [localized("loc_list_header", ui_lang)]
        for code in available_locales():
            st = locale_stats(code)
            lines.append(f"{language_name(code)} ({code}): {locale_bar(code)} {st['percent']}%")
        lines.append("")
        lines.append(localized("loc_list_footer", ui_lang))
        await message.reply("\n".join(lines))
        return

    if arg not in available_locales():
        await message.reply(localized("loc_unknown_lang", ui_lang, lang=arg, supported=", ".join(available_locales())))
        return

    if not rate_limit_ok(("locale-file", "telegram", message.chat.id), limit=1, window_seconds=600):
        await message.reply(localized("loc_cooldown", ui_lang))
        return

    path = os.path.join(os.path.dirname(utils.__file__), "i18n", f"{arg}.json")
    st = locale_stats(arg)
    caption = localized("loc_file_caption", ui_lang, name=language_name(arg), code=arg, percent=st["percent"])
    try:
        from aiogram.types import BufferedInputFile
        with open(path, "rb") as f:
            data = f.read()
        await message.reply_document(BufferedInputFile(data, filename=f"{arg}.json"), caption=caption)
    except Exception:
        await message.reply(caption)

@router.message(Command("loc_compare", "loc-compare"))
async def loc_compare_cmd(message: Message):
    """Show one reply key in all six languages with their status emoji."""
    thread = message.message_thread_id or 0
    ui_lang = get_chat_lang(f"{message.chat.id}:{thread}")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("loc_compare_usage", ui_lang))
        return
    key = parts[1].strip()
    data = compare_reply(key)
    if data is None:
        await message.reply(localized("loc_compare_not_found", ui_lang, key=key))
        return
    lines = [localized("loc_compare_header", ui_lang, key=key)]
    for code in LANG_ORDER:
        if code not in data:
            continue
        status, text = data[code]
        emoji = LOCALE_STATUS_EMOJI.get(status, "")
        if text is None:
            shown = localized("loc_compare_untranslated", ui_lang)
        else:
            shown = str(text)
            if len(shown) > 300:
                shown = shown[:297] + "..."
        lines.append(f"{emoji} {language_name(code)}: {shown}")
    msg = "\n".join(lines)
    if len(msg) > 3900:
        msg = msg[:3900]
    await message.reply(msg)

@router.message(Command("loc_suggest", "loc-suggest"))
async def loc_suggest_cmd(message: Message):
    """File a translation suggestion (`/loc_suggest <lang> <key> <text>`).
    It is stored under a short hex code, posted to the support chats of both
    platforms, and the code is echoed back for the eventual reply."""
    thread = message.message_thread_id or 0
    ui_lang = get_chat_lang(f"{message.chat.id}:{thread}")
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(localized("loc_suggest_usage", ui_lang))
        return
    language = parts[1].strip().lower()
    rkey = parts[2].strip()
    text = parts[3]
    if language not in SUPPORTED_LANGS:
        await message.reply(localized("loc_unknown_lang", ui_lang, lang=language, supported=", ".join(available_locales())))
        return
    if not SUPPORT_CHATS.get("discord") and not SUPPORT_CHATS.get("telegram"):
        await message.reply(localized("loc_suggest_no_support", ui_lang))
        return

    msg_code = secrets.token_hex(4)
    username = message.from_user.full_name if message.from_user else "Unknown"
    db.add_loc_suggestion(msg_code, "telegram", message.from_user.id, username,
                          language, rkey, text, ui_lang)
    try:
        from discord_bot import post_loc_suggestion
        await post_loc_suggestion(lang=language, key=rkey, suggestion=text, code=msg_code,
                                  ui_lang=ui_lang, username=username, user_id=message.from_user.id)
    except Exception as e:
        logger.warning("Failed to post loc suggestion: %s", e)
    await message.reply(localized("loc_suggest_confirm", ui_lang, code=msg_code))

@router.message(Command("loc_reply", "loc-reply"))
async def loc_reply_cmd(message: Message):
    """Answer a suggestion by its code: DM the suggester wherever they filed
    from, echo to the support chats, and close the ticket only when the DM
    got through."""
    thread = message.message_thread_id or 0
    ui_lang_cmd = get_chat_lang(f"{message.chat.id}:{thread}")
    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", ui_lang_cmd))
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(localized("loc_reply_usage", ui_lang_cmd))
        return
    code = parts[1].strip()
    reply_text = parts[2]
    row = db.get_loc_suggestion(code)
    if not row:
        await message.reply(localized("loc_reply_not_found", ui_lang_cmd, code=code))
        return

    ui_lang = row["ui_lang"] or DEFAULT_LANG
    title = localized("loc_reply_dm_title", ui_lang)
    body = localized("loc_reply_dm_body", ui_lang,
                     suggestion=row["suggestion"], reply=reply_text,
                     name=language_name(row["lang"]), lang=row["lang"], key=row["rkey"])

    ok = False
    if row["platform"] == "telegram":
        try:
            await bot.send_message(int(row["user_id"]), f"{title}\n\n{body}")
            ok = True
        except Exception:
            ok = False
    elif row["platform"] == "discord":
        try:
            import discord as _discord
            from discord_bot import bot as dc_bot
            user = await dc_bot.fetch_user(int(row["user_id"]))
            await user.send(embed=_discord.Embed(title=title, description=body))
            ok = True
        except Exception:
            ok = False

    try:
        from discord_bot import post_loc_reply
        await post_loc_reply(admin=username_of(message.from_user), code=code,
                             ui_lang=ui_lang, title=title, body=body)
    except Exception as e:
        logger.warning("Failed to post loc reply to support: %s", e)

    if ok:
        db.delete_loc_suggestion(code)
        await message.reply(localized("loc_reply_sent", ui_lang_cmd))
    else:
        await message.reply(localized("loc_reply_failed", ui_lang_cmd))
