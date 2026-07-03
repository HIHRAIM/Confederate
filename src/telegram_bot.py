from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery
import db, message_relay
from message_relay import (
    telegram_entities_to_discord, escape_html,
    build_telegram_text, clip_text, clean_display_name, DISCORD_MSG_LIMIT,
)
import utils
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, get_chat_lang,
    rate_limit_ok,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    set_chat_lang, localized_sticker, localized_file_count_text,
    localized_voice_message, localized_video_message, localized_whois,
    localized_bridge_info, localized_help,
    localized, language_name, available_locales, locale_stats, locale_bar,
    compare_reply, LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS, DEFAULT_LANG,
)
from config import TELEGRAM_TOKEN, SUPPORT_CHATS
import os
import secrets
import time
import asyncio
import json
import logging

logger = logging.getLogger("bridge.telegram")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

_media_group_buffer = {}

_TG_AVATAR_ASSETS = {
    1: "user-green.png", 2: "user-green.png",
    3: "user-yellow.png", 4: "user-yellow.png",
    5: "user-red.png", 6: "user-red.png",
    7: "user-grey.png", 8: "user-grey.png",
    9: "user-blue.png", 0: "user-blue.png",
}

async def get_telegram_avatar_url(user_id, host_chat_id=None):
    """Discord-usable webhook avatar URL for a Telegram sender, picked by the last
    digit of the user's ID."""
    try:
        last_digit = int(user_id) % 10
    except Exception:
        return None
    asset = _TG_AVATAR_ASSETS.get(last_digit)
    if not asset:
        return None
    from discord_bot import avatar_asset_url
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

def build_poll_keyboard(poll_id, options):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    rows = []
    for idx, opt in enumerate(options):
        label = opt if len(opt) <= 60 else opt[:59] + "…"
        rows.append([InlineKeyboardButton(text=f"{idx + 1}. {label}", callback_data=f"poll:{poll_id}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def poll_start_text_telegram(question, options, ends_at, lang):
    from datetime import datetime, timezone
    lines = [f"📊 {question}", localized("poll_anonymous", lang), ""]
    for i, opt in enumerate(options):
        lines.append(f"{i + 1}. {opt}")
    lines.append("")
    ends = datetime.fromtimestamp(ends_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(localized("poll_ends", lang, ends=ends))
    return "\n".join(lines)

def _telegram_html_mention(user) -> str:
    if getattr(user, "username", None):
        return f"@{escape_html(user.username)}"
    full_name = escape_html(getattr(user, "full_name", "User"))
    return f'<a href="tg://user?id={user.id}">{full_name}</a>'

def _count_telegram_files(message: Message) -> int:
    count = 0
    if getattr(message, "document", None):
        count += 1
    if getattr(message, "photo", None):
        count += 1
    if getattr(message, "video", None):
        count += 1
    if getattr(message, "audio", None):
        count += 1
    if getattr(message, "voice", None):
        count += 1
    if getattr(message, "video_note", None):
        count += 1
    if getattr(message, "animation", None):
        count += 1
    return count

def _build_telegram_relay_texts(message: Message, grouped_file_count: int | None = None):
    is_sticker = getattr(message, "sticker", None) is not None
    thread = message.message_thread_id or 0
    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    total_files = grouped_file_count if grouped_file_count is not None else _count_telegram_files(message)

    if is_sticker:
        return ["__TG_STICKER__"], None

    is_voice = getattr(message, "voice", None) is not None
    is_video_note = getattr(message, "video_note", None) is not None
    if not base_text and total_files == 1 and is_voice:
        return ["__TG_VOICE__"], None
    if not base_text and total_files == 1 and is_video_note:
        return ["__TG_VIDEO_NOTE__"], None

    relay_file_count = None
    if total_files > 0:
        link = None
        username = getattr(message.chat, "username", None)
        if username:
            if thread:
                link = f"https://t.me/{username}/{thread}/{message.message_id}"
            else:
                link = f"https://t.me/{username}/{message.message_id}"
        else:
            fwd_chat = getattr(message, "forward_from_chat", None)
            fwd_username = getattr(fwd_chat, "username", None) if fwd_chat else None
            fwd_msg_id = getattr(message, "forward_from_message_id", None)
            if fwd_username and fwd_msg_id:
                link = f"https://t.me/{fwd_username}/{fwd_msg_id}"

        if link:
            prefix = (base_text + "\n") if base_text else ""
            if total_files > 1:
                relay_file_count = total_files
                return [prefix + f"{link} (__TG_FILES_{total_files}__)"], relay_file_count
            return [prefix + link], relay_file_count

        relay_file_count = total_files
        return [(base_text + "\n" if base_text else "") + f"[__TG_FILES_{total_files}__]"], relay_file_count

    return [base_text], relay_file_count

def _serialize_first_telegram_message(message: Message, *, chat_id: str, bridge_id: int, reply_to_msg_db_id, forward_type, forward_name):
    texts, relay_file_count = _build_telegram_relay_texts(message)
    source_text = getattr(message, "text", None)
    source_caption = getattr(message, "caption", None)
    tg_html_source = None
    if source_text is not None:
        tg_html_source = getattr(message, "html_text", None)
        discord_text = telegram_entities_to_discord(source_text, getattr(message, "entities", None))
    elif source_caption is not None:
        tg_html_source = getattr(message, "html_caption", None)
        discord_text = telegram_entities_to_discord(source_caption, getattr(message, "caption_entities", None))
    else:
        discord_text = texts[0] if texts else ""

    payload = {
        "bridge_id": bridge_id,
        "origin_chat_id": chat_id,
        "origin_message_id": str(message.message_id),
        "origin_sender_id": str(message.from_user.id) if message.from_user else "",
        "place_name": message.chat.title or "Private chat",
        "sender_name": message.from_user.full_name if message.from_user else "Unknown",
        "reply_to_msg_db_id": reply_to_msg_db_id,
        "forward_type": forward_type,
        "forward_name": forward_name,
        "texts": texts,
        "relay_file_count": relay_file_count,
        "base_text": (getattr(message, "text", "") or getattr(message, "caption", "") or ""),
        "discord_text": discord_text,
        "telegram_html": tg_html_source,
    }
    return json.dumps(payload, ensure_ascii=False)

async def _relay_serialized_telegram_payload(payload_json: str):
    try:
        payload = json.loads(payload_json)
    except Exception:
        return

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False):
        if chat["platform"] == "telegram":
            chat_id_str, thread = chat["chat_id"].split(":")
            body_html = body_telegram_html if body_telegram_html else escape_html(body_plain)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            html_text = build_telegram_text(header, body_html, body_plain)
            send_kwargs = dict(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=html_text,
                parse_mode="HTML",
            )
            if reply_to_platform_message_id:
                send_kwargs["reply_to_message_id"] = int(reply_to_platform_message_id)
            try:
                sent = await bot.send_message(**send_kwargs)
            except Exception:
                if reply_to_platform_message_id:
                    send_kwargs.pop("reply_to_message_id", None)
                    sent = await bot.send_message(**send_kwargs)
                else:
                    raise
            return str(sent.message_id)

        if chat["platform"] == "discord":
            from discord_bot import deliver_discord_relay
            return await deliver_discord_relay(
                chat, header=header, body_discord=body_discord, reply_line=reply_line,
                reply_link_line=reply_link_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )

    base_text = payload.get("base_text", "")
    avatar_url = await _telegram_relay_avatar_url(payload["bridge_id"], payload.get("origin_sender_id"))
    for text in payload.get("texts", []):
        current_discord_text = payload.get("discord_text", "") if text == base_text else text
        current_telegram_html = payload.get("telegram_html") if text == base_text else None
        await message_relay.relay_message(
            bridge_id=payload["bridge_id"],
            origin_platform="telegram",
            origin_chat_id=payload["origin_chat_id"],
            origin_message_id=payload["origin_message_id"],
            origin_sender_id=payload["origin_sender_id"],
            messenger_name="Telegram",
            place_name=payload.get("place_name", "Private chat"),
            sender_name=payload.get("sender_name", "Unknown"),
            text=text,
            discord_text=current_discord_text,
            telegram_html=current_telegram_html,
            reply_to_msg_db_id=payload.get("reply_to_msg_db_id"),
            send_to_chat_func=send_to_chat,
            telegram_file_count=payload.get("relay_file_count"),
            forward_type=payload.get("forward_type"),
            forward_name=payload.get("forward_name"),
            avatar_url=avatar_url,
        )

async def _flush_media_group(buffer_key):
    await asyncio.sleep(1.0)
    payload = _media_group_buffer.pop(buffer_key, None)
    if not payload:
        return
    await _relay_from_telegram_impl(
        payload["message"],
        grouped_file_count=payload["count"],
        grouped_message_ids=payload.get("message_ids"),
    )

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

async def is_telegram_native_admin(chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

@router.message(Command("atb"))
async def atb(message: Message):
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

    try:
        bridge_id = int(parts[1])
    except ValueError:
        await message.reply(localized("atb_invalid_id", lang))
        return

    if db.chat_exists(chat_id):
        await message.reply(localized("atb_already_attached", lang))
        return

    db.attach_chat("telegram", chat_id, bridge_id)

    try:
        await bot.send_message(
            chat_id=int(message.chat.id),
            message_thread_id=int(thread) or None,
            text=localized_bot_joined(lang)
        )
    except Exception:
        await message.reply(localized("atb_attached", lang, bridge_id=bridge_id))
    else:
        await message.reply(localized("atb_attached", lang, bridge_id=bridge_id))

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

@router.message(lambda message: not ((getattr(message, "text", "") or "").startswith("/")))
async def relay_from_telegram(message: Message):
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        files_count = _count_telegram_files(message)
        if files_count > 0:
            thread = message.message_thread_id or 0
            key = (f"{message.chat.id}:{thread}", str(media_group_id))
            payload = _media_group_buffer.get(key)
            if not payload:
                payload = {"message": message, "count": 0, "task": None, "message_ids": []}
                _media_group_buffer[key] = payload

            payload["count"] += files_count
            payload["message_ids"].append(message.message_id)
            if getattr(message, "caption", None) and not getattr(payload["message"], "caption", None):
                payload["message"] = message
            elif message.message_id < payload["message"].message_id:
                payload["message"] = message

            if payload.get("task"):
                payload["task"].cancel()
            payload["task"] = asyncio.create_task(_flush_media_group(key))
            return

    await _relay_from_telegram_impl(message)

async def _relay_from_telegram_impl(message: Message, grouped_file_count: int | None = None, grouped_message_ids=None):
    thread = message.message_thread_id or 0
    origin_chat_id = f"{message.chat.id}:{thread}"

    is_bot_sender = bool(message.from_user and message.from_user.is_bot)
    if is_bot_sender:
        if not db.get_allow_bots(origin_chat_id):
            return
        if db.is_relay_copy("telegram", origin_chat_id, str(message.message_id)):
            return
    lang = get_chat_lang(origin_chat_id)

    forward_type = None
    forward_name = None
    if getattr(message, "forward_from_chat", None):
        forward_type = "chat"
        forward_name = message.forward_from_chat.title or "unknown"
    elif getattr(message, "forward_from", None):
        forward_type = "user"
        try:
            forward_name = message.forward_from.full_name
        except Exception:
            forward_name = getattr(message.forward_from, "username", "unknown")
    elif getattr(message, "forward_sender_name", None):
        forward_type = "unknown"

    is_forward = forward_type is not None

    pending_reply_to_msg_db_id = None
    if (
        not is_forward
        and getattr(message, "reply_to_message", None)
        and message.reply_to_message.message_id != message.message_thread_id
    ):
        replied_msg = message.reply_to_message
        replied_msg_id = str(replied_msg.message_id)
        if replied_msg.from_user and replied_msg.from_user.is_bot:
            copy_row = db.cur.execute(
                "SELECT message_id FROM message_copies WHERE platform='telegram' AND chat_id=? AND message_id_platform=?",
                (origin_chat_id, replied_msg_id)
            ).fetchone()
            pending_reply_to_msg_db_id = copy_row["message_id"] if copy_row else -1
        else:
            msg_row = db.cur.execute(
                "SELECT id FROM messages WHERE origin_platform='telegram' AND origin_chat_id=? AND origin_message_id=?",
                (origin_chat_id, replied_msg_id)
            ).fetchone()
            if msg_row:
                pending_reply_to_msg_db_id = msg_row["id"]
            else:
                member_db_id = db.find_message_db_id_by_media_member(origin_chat_id, replied_msg_id)
                pending_reply_to_msg_db_id = member_db_id if member_db_id is not None else -1

    chat_id = origin_chat_id

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    db.cur.execute(
        "UPDATE bridge_rules SET message_counter = COALESCE(message_counter, 0) + 1 WHERE bridge_id=?",
        (bridge_id,)
    )
    db.conn.commit()

    prefix = str(message.chat.id)
    user_id_str = str(message.from_user.id) if message.from_user else ""

    if not is_bot_sender and db.is_shadow_banned("telegram", user_id_str):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception as e:
            logger.warning(
                "Failed to delete shadow-banned message (user=%s, chat=%s): %s",
                user_id_str, origin_chat_id, e
            )
        return

    if not is_bot_sender and not db.is_user_verified("telegram", user_id_str, prefix):
        pend = db.get_pending_consent("telegram", prefix, user_id_str)
        if pend:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                pass
            return
        else:
            lang = get_chat_lang(f"{message.chat.id}:{thread}")
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            cbdata = f"verify:telegram|{prefix}|{user_id_str}"
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=localized_consent_button(lang), callback_data=cbdata)]
            ])
            try:
                mention = _telegram_html_mention(message.from_user)
                consent_text = (
                    f"{mention},\n"
                    f"<b>{escape_html(localized_consent_title(lang))}</b>\n\n"
                    f"{escape_html(localized_consent_body(lang))}"
                )

                sent = await bot.send_message(
                    chat_id=int(message.chat.id),
                    message_thread_id=int(thread) or None,
                    text=consent_text,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                chat_key = f"{message.chat.id}:{thread}"
                db.add_pending_consent(
                    "telegram",
                    prefix,
                    user_id_str,
                    str(sent.message_id),
                    chat_key,
                    first_message_id=str(message.message_id),
                    first_message_payload=_serialize_first_telegram_message(
                        message,
                        chat_id=chat_id,
                        bridge_id=bridge_id,
                        reply_to_msg_db_id=pending_reply_to_msg_db_id,
                        forward_type=forward_type,
                        forward_name=forward_name,
                    )
                )
            except Exception:
                chat_key = f"{message.chat.id}:{thread}"
                db.add_pending_consent(
                    "telegram",
                    prefix,
                    user_id_str,
                    "",
                    chat_key,
                    first_message_id=str(message.message_id),
                    first_message_payload=_serialize_first_telegram_message(
                        message,
                        chat_id=chat_id,
                        bridge_id=bridge_id,
                        reply_to_msg_db_id=pending_reply_to_msg_db_id,
                        forward_type=forward_type,
                        forward_name=forward_name,
                    )
                )
            return

    reply_to_msg_db_id = pending_reply_to_msg_db_id

    if not rate_limit_ok(("relay", "telegram", user_id_str), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping relay from telegram user %s in %s", user_id_str, origin_chat_id)
        return

    texts, relay_file_count = _build_telegram_relay_texts(message, grouped_file_count=grouped_file_count)

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False):
        if chat["platform"] == "telegram":
            chat_id_str, thread = chat["chat_id"].split(":")
            if body_telegram_html:
                body_html = body_telegram_html
            else:
                body_html = escape_html(body_plain)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            html_text = build_telegram_text(header, body_html, body_plain)
            send_kwargs = dict(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=html_text,
                parse_mode="HTML",
            )
            if reply_to_platform_message_id:
                send_kwargs["reply_to_message_id"] = int(reply_to_platform_message_id)
            try:
                sent = await bot.send_message(**send_kwargs)
            except Exception:
                if reply_to_platform_message_id:
                    send_kwargs.pop("reply_to_message_id", None)
                    sent = await bot.send_message(**send_kwargs)
                else:
                    raise
            return str(sent.message_id)

        if chat["platform"] == "discord":
            from discord_bot import deliver_discord_relay
            return await deliver_discord_relay(
                chat, header=header, body_discord=body_discord, reply_line=reply_line,
                reply_link_line=reply_link_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )

    source_text = getattr(message, "text", None)
    source_caption = getattr(message, "caption", None)
    tg_html_source = None
    if source_text is not None:
        tg_html_source = getattr(message, "html_text", None)
        discord_text = telegram_entities_to_discord(source_text, getattr(message, "entities", None))
    elif source_caption is not None:
        tg_html_source = getattr(message, "html_caption", None)
        discord_text = telegram_entities_to_discord(source_caption, getattr(message, "caption_entities", None))
    else:
        discord_text = texts[0] if texts else ""

    telegram_html = tg_html_source

    avatar_url = await _telegram_relay_avatar_url(
        bridge_id, message.from_user.id if message.from_user else None
    )

    relayed_db_id = None
    for text in texts:
        current_discord_text = discord_text if text == (getattr(message, "text", "") or getattr(message, "caption", "") or "") else text
        current_telegram_html = telegram_html if text == (getattr(message, "text", "") or getattr(message, "caption", "") or "") else None
        relayed_db_id = await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="telegram",
            origin_chat_id=chat_id,
            origin_message_id=str(message.message_id),
            origin_sender_id=str(message.from_user.id) if message.from_user else "",
            messenger_name="Telegram",
            place_name=message.chat.title or "Private chat",
            sender_name=message.from_user.full_name if message.from_user else "Unknown",
            text=text,
            discord_text=current_discord_text,
            telegram_html=current_telegram_html,
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            telegram_file_count=relay_file_count,
            forward_type=forward_type,
            forward_name=forward_name,
            is_bot_sender=is_bot_sender,
            avatar_url=avatar_url,
        )

    if grouped_message_ids and relayed_db_id is not None:
        db.record_media_group_members(chat_id, grouped_message_ids, relayed_db_id)

@router.message(Command("setadmin"))
async def setadmin(message: Message):
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply(localized("setadmin_usage", lang))
        return

    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        await message.reply(localized("chat_not_in_bridge", lang))
        return

    bridge_id = row["bridge_id"]
    db.add_bridge_admin(bridge_id, uid)
    await message.reply(localized("setadmin_bridge_done", lang, user_id=uid))
    try:
        await bot.send_message(uid, localized("setadmin_bridge_dm", lang, bridge_id=bridge_id))
    except Exception:
        pass

@router.message(Command("remadmin"))
async def remadmin(message: Message):
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_id)

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply(localized("remadmin_usage", lang))
        return

    if not is_admin("telegram", message.from_user.id):
        await message.reply(localized("no_permission", lang))
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        await message.reply(localized("chat_not_in_bridge", lang))
        return

    bridge_id = row["bridge_id"]
    db.remove_bridge_admin(bridge_id, uid)
    await message.reply(localized("remadmin_done", lang, user_id=uid))

@router.message(Command("lang"))
async def set_lang_handler(message: Message):
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split()
    if len(parts) != 2:
        await message.reply(localized("lang_usage", lang))
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

@router.my_chat_member()
async def my_chat_member_update(update: ChatMemberUpdated):
    """
    When bot is removed from a chat (left/kicked), clean up chat_settings for that chat.
    """
    try:
        new_status = update.new_chat_member.status
        me = await bot.get_me()
        if update.new_chat_member.user.id == me.id and new_status in ("left", "kicked"):
            db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{update.chat.id}:%",))
            db.conn.commit()
    except Exception:
        pass

@router.message(Command("remindrules"))
async def remindrules(message: Message):
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

@router.callback_query(lambda c: c.data and c.data.startswith("verify:"))
async def handle_verify_callback(query: CallbackQuery):
    """
    Expected callback_data: verify:telegram|<prefix>|<user_id>
    Only the target user can confirm. On confirm — add verified and remove pending + bot message.
    """
    data = query.data
    if query.message:
        lang = get_chat_lang(f"{query.message.chat.id}:{query.message.message_thread_id or 0}")
    else:
        lang = DEFAULT_LANG
    try:
        _, payload = data.split(":", 1)
        parts = payload.split("|")
        platform = parts[0]
        prefix = parts[1]
        target_user_id = parts[2]
    except Exception:
        await query.answer(localized("verify_invalid_data", lang), show_alert=True)
        return

    if str(query.from_user.id) != str(target_user_id):
        await query.answer(localized("verify_button_not_yours", lang), show_alert=True)
        return

    if not db.get_pending_consent("telegram", prefix, target_user_id):
        await query.answer(localized("verify_invalid_data", lang), show_alert=True)
        return

    db.add_verified_user("telegram", target_user_id, prefix, days_valid=365)

    all_pendings = db.get_all_pending_consents_for_user("telegram", target_user_id)

    first_payloads = []
    for p in all_pendings:
        p_bot_msg_id = p["bot_message_id"]
        if p_bot_msg_id:
            try:
                p_chat_id_str, p_th = p["chat_key"].split(":")
                await bot.delete_message(chat_id=int(p_chat_id_str), message_id=int(p_bot_msg_id))
            except Exception:
                pass
        p_payload = p["first_message_payload"] if "first_message_payload" in p.keys() else None
        if p_payload:
            first_payloads.append(p_payload)
        db.remove_pending_consent("telegram", p["prefix"], target_user_id)

    for payload in first_payloads:
        await _relay_serialized_telegram_payload(payload)

    await query.answer(localized("verify_thanks", lang), show_alert=False)

@router.message(Command("verify"))
async def verify_cmd(message: Message):
    thread = message.message_thread_id or 0
    prefix = str(message.chat.id)
    user_id = str(message.from_user.id)
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    if not rate_limit_ok(("verify-cmd", "telegram", user_id), limit=2, window_seconds=60):
        return

    prev = db.get_pending_consent("telegram", prefix, user_id)
    if prev:
        try:
            pid_chat, pid_thread = prev["chat_key"].split(":")
            await bot.delete_message(chat_id=int(pid_chat), message_id=int(prev["bot_message_id"]))
        except Exception:
            pass
        db.remove_pending_consent("telegram", prefix, user_id)

    mention = _telegram_html_mention(message.from_user)
    consent_text = (
        f"{mention},\n"
        f"<b>{escape_html(localized_consent_title(lang))}</b>\n\n"
        f"{escape_html(localized_consent_body(lang))}"
    )
    cbdata = f"verify:telegram|{prefix}|{user_id}"

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=localized_consent_button(lang), callback_data=cbdata)]])
    try:
        sent = await bot.send_message(chat_id=int(message.chat.id), message_thread_id=int(thread) or None,
                                      text=consent_text, reply_markup=markup, parse_mode="HTML")
        db.add_pending_consent("telegram", prefix, user_id, str(sent.message_id), chat_key)
    except Exception:
        await message.reply(localized("verify_send_failed", lang))

@router.message(Command("unverify"))
async def unverify_cmd(message: Message):
    lang = get_chat_lang(f"{message.chat.id}:{message.message_thread_id or 0}")
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        uid = message.from_user.id
    else:
        if not is_admin("telegram", message.from_user.id):
            await message.reply(localized("no_permission", lang))
            return
        identifier = parts[1].strip()
        if identifier.startswith("@") or not identifier.isdigit():
            uid = await resolve_telegram_user(identifier)
            if uid is None:
                await message.reply(localized("could_not_resolve_user", lang))
                return
        else:
            uid = int(identifier)

    db.cur.execute("DELETE FROM verified_users WHERE platform='telegram' AND user_id=?", (str(uid),))
    db.conn.commit()
    await message.reply(localized("unverify_done", lang, user_id=uid))

@router.message(Command("shadow-ban"))
async def shadow_ban_cmd(message: Message):
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply(localized("shadowban_usage", lang))
        return
    allowed = False
    if is_admin("telegram", message.from_user.id):
        allowed = True
    else:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row:
            bridge_id = row["bridge_id"]
            bridge_admins = db.get_bridge_admins(bridge_id)
            if str(message.from_user.id) in bridge_admins:
                allowed = True
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    db.add_shadow_ban("telegram", uid)
    await message.reply(localized("shadowban_done", lang, user_id=uid))

@router.message(Command("whois"))
async def whois_cmd(message: Message):
    lang = get_chat_lang(f"{message.chat.id}:{message.message_thread_id or 0}")

    async def _reply_autodelete(text: str):
        sent = await message.reply(text)
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    requester_id = str(message.from_user.id) if message.from_user else ""
    if not rate_limit_ok(("whois", "telegram", requester_id), limit=5, window_seconds=60):
        return

    if not (
        is_admin("telegram", message.from_user.id if message.from_user else 0)
        or db.is_user_verified("telegram", requester_id, str(message.chat.id))
    ):
        await _reply_autodelete(localized_whois("not_verified", lang))
        return

    reply = getattr(message, "reply_to_message", None)
    replied_id = str(
        getattr(reply, "message_id", "")
        or getattr(message, "reply_to_message_id", "")
        or ""
    )

    if not replied_id.strip():
        await _reply_autodelete(localized_whois("use_reply", lang))
        return

    chat_key = f"{message.chat.id}:{message.message_thread_id or 0}"

    row = db.cur.execute(
        "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
        ("telegram", chat_key, replied_id)
    ).fetchone()

    if not row:
        await _reply_autodelete(localized_whois("origin_not_found", lang))
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await _reply_autodelete(localized_whois("origin_missing", lang))
        return

    origin_platform = msg_row["origin_platform"]
    origin_chat_id = msg_row["origin_chat_id"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    if origin_platform == "telegram":
        try:
            prefix = origin_chat_id.split(":",1)[0]
            member = await bot.get_chat_member(int(prefix), int(origin_sender_id))
            u = member.user
            uname = f"@{u.username}" if u.username else "—"
            full = u.full_name or (u.first_name or "")
            full_user = await bot.get_chat(int(origin_sender_id))
            bio = getattr(full_user, "bio", None) or "—"
            await _reply_autodelete(
                localized_whois(
                    "tg_template",
                    lang,
                    nickname=full,
                    username=uname,
                    id=u.id,
                    bio=bio
                )
            )
        except Exception as e:
            logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
            await _reply_autodelete(localized_whois("fetch_error", lang, error=type(e).__name__))
        return

    if origin_platform == "discord":
        try:
            from discord_bot import bot as dc_bot
            import discord as _discord

            guild_id = origin_chat_id.split(":", 1)[0]
            guild = dc_bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None

            try:
                user_obj = await dc_bot.fetch_user(int(origin_sender_id))
            except Exception:
                user_obj = getattr(member, "user", None)

            nick = member.display_name if member else "—"
            user_name = "—"
            if user_obj:
                user_name = f"{user_obj.name}#{user_obj.discriminator}"
            elif member:
                user_name = f"{member.name}#{member.discriminator}"

            mode_key = str(getattr(member, "status", "offline"))
            if mode_key not in ("online", "idle", "dnd", "offline", "invisible"):
                mode_key = "offline"
            if mode_key == "invisible":
                mode_key = "offline"
            mode = localized_whois(f"mode_{mode_key}", lang)

            custom_status = "—"
            if member:
                try:
                    custom = _discord.utils.find(
                        lambda a: isinstance(a, _discord.CustomActivity),
                        member.activities or []
                    )
                    if custom and getattr(custom, "name", None):
                        custom_status = custom.name
                except Exception:
                    custom_status = "—"

            avatar_url = "—"
            banner_url = "—"
            created_at = "—"
            if user_obj:
                if getattr(user_obj, "display_avatar", None):
                    avatar_url = str(user_obj.display_avatar.url)
                if getattr(user_obj, "banner", None):
                    banner_url = str(user_obj.banner.url)
                if getattr(user_obj, "created_at", None):
                    created_at = user_obj.created_at.strftime("%Y-%m-%d %H:%M UTC")

            await _reply_autodelete(
                localized_whois(
                    "dc_template",
                    lang,
                    nickname=nick or "—",
                    username=user_name or "—",
                    id=origin_sender_id,
                    status=custom_status,
                    mode=mode,
                    registered=created_at,
                    avatar=avatar_url,
                    banner=banner_url,
                )
            )
        except Exception as e:
            logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
            await _reply_autodelete(localized_whois("fetch_error", lang, error=type(e).__name__))
        return

    await _reply_autodelete(localized_whois("origin_not_telegram", lang))

@router.message(Command("bridge"))
async def bridge_cmd(message: Message):
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("bridge-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
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

@router.message(Command("allow_bots"))
async def allow_bots_cmd(message: Message):
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

@router.message(Command("help"))
async def help_cmd(message: Message):
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("help-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
        sent = await message.reply(text, parse_mode="HTML")
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    everyone_lines = "\n".join([
        escape_html(localized_help("cmd_bridge", lang)),
        escape_html(localized_help("cmd_whois", lang)),
        escape_html(localized_help("cmd_verify", lang)),
        escape_html(localized_help("cmd_poll", lang)),
        escape_html(localized_help("cmd_locale", lang)),
        escape_html(localized_help("cmd_loc_compare", lang)),
        escape_html(localized_help("cmd_loc_suggest", lang)),
        escape_html(localized_help("cmd_help", lang)),
    ])

    admins_lines = "\n".join([
        escape_html(localized_help("cmd_rfb", lang)),
        escape_html(localized_help("cmd_setadmin", lang)),
        escape_html(localized_help("cmd_lang", lang)),
        escape_html(localized_help("cmd_remindrules", lang)),
        escape_html(localized_help("cmd_shadowban", lang)),
        escape_html(localized_help("cmd_unverify", lang)),
        escape_html(localized_help("cmd_allow_bots_tg", lang)),
    ])

    bot_admins_lines = "\n".join([
        escape_html(localized_help("cmd_atb", lang)),
        escape_html(localized_help("cmd_remadmin", lang)),
        escape_html(localized_help("cmd_backup", lang)),
        escape_html(localized_help("cmd_loc_reply", lang)),
    ])

    text = (
        f"<b>{localized_help('title', lang)}</b>\n\n"
        f"<b>{localized_help('section_everyone', lang)}</b>\n{everyone_lines}\n\n"
        f"<b>{localized_help('section_admins', lang)}</b>\n{admins_lines}\n\n"
        f"<b>{localized_help('section_bot_admins', lang)}</b>\n{bot_admins_lines}"
    )
    await _reply_autodelete(text)

@router.edited_message()
async def edited_message_handler(message: Message):
    thread = message.message_thread_id or 0
    origin_chat_id = f"{message.chat.id}:{thread}"
    row = db.cur.execute(
        """
        SELECT id FROM messages
        WHERE origin_platform='telegram' AND origin_chat_id=? AND origin_message_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (origin_chat_id, str(message.message_id))
    ).fetchone()
    if not row:
        return

    texts, relay_file_count = _build_telegram_relay_texts(message)
    rendered_text = texts[0] if texts else ""
    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    if getattr(message, "text", None) is not None:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "entities", None))
        telegram_html = getattr(message, "html_text", None)
    else:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "caption_entities", None))
        telegram_html = getattr(message, "html_caption", None)

    header = f"[Telegram | {clean_display_name(message.chat.title or 'Private chat')}] {clean_display_name(message.from_user.full_name if message.from_user else 'Unknown')}:"

    copies = db.cur.execute("SELECT * FROM message_copies WHERE message_id=?", (row["id"],)).fetchall()
    for c in copies:
        try:
            if c["platform"] == "telegram":
                chat_id_str, th = c["chat_id"].split(":")
                target_lang = get_chat_lang(c["chat_id"])
                localized_text = rendered_text
                if relay_file_count is not None:
                    localized_text = localized_text.replace(
                        f"__TG_FILES_{relay_file_count}__",
                        localized_file_count_text(relay_file_count, target_lang)
                    )
                await bot.edit_message_text(
                    chat_id=int(chat_id_str),
                    message_id=int(c["message_id_platform"]),
                    text=build_telegram_text(header, telegram_html or escape_html(discord_text), discord_text),
                    parse_mode="HTML"
                )
            elif c["platform"] == "discord":
                from discord_bot import bot as dc_bot, edit_discord_relay_copy
                channel_id = int(c["chat_id"].split(":")[1])
                ch = dc_bot.get_channel(channel_id)
                if not ch:
                    try:
                        ch = await dc_bot.fetch_channel(channel_id)
                    except Exception:
                        continue
                await edit_discord_relay_copy(ch, c["message_id_platform"], header, discord_text, message_db_id=row["id"], chat=c)
        except Exception:
            pass

@router.message(Command("backup"))
async def backup_tg_cmd(message: Message):
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

@router.message(Command("locale"))
async def locale_cmd(message: Message):
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

def username_of(user):
    if user is None:
        return "Unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "full_name", None) or str(getattr(user, "id", "Unknown"))

@router.message(Command("poll"))
async def poll_cmd(message: Message):
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

async def main():
    await dp.start_polling(bot)
