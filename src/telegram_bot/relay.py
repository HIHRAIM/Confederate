"""The inbound Telegram relay: turning an incoming message into bridge
copies, and keeping those copies in step with later edits.

Three things make this side harder than the Discord one and account for most
of the code here:

  * an album arrives as several separate updates, so messages are buffered
    for a second and relayed as one (`_flush_media_group`);
  * a message from a user who has not consented yet must be relayed *later*,
    once they press the button — and an aiogram Message object cannot be
    re-fetched, so everything the relay will need is serialized into the
    pending-consent row now (`_serialize_first_telegram_message`) and replayed
    from JSON then (`_relay_serialized_telegram_payload`);
  * attachments have no public URLs, so they are re-uploaded to GALLERY and
    represented by links, with a "[N files from Telegram]" footer standing in
    for whatever did not fit.

Not this module's zone: delivery into Discord (discord_bot/relay.py), the
neutral fan-out (message_relay.py), or the file upload itself (files.py).
"""
import asyncio
import json
import logging

from aiogram.types import Message

import db
import message_relay
from message_relay import (
    build_telegram_text, clean_display_name, escape_html,
    telegram_entities_to_discord,
)
from utils import (
    get_chat_lang, localized_consent_body, localized_consent_button,
    localized_consent_title, localized_file_count_text, rate_limit_ok,
)

from telegram_bot.client import (
    _telegram_html_mention, _telegram_relay_avatar_url, bot, router,
)
from telegram_bot.files import (
    _collect_gallery_candidates, _count_telegram_files,
    _upload_telegram_files_to_gallery,
)

logger = logging.getLogger("bridge.telegram")

_media_group_buffer = {}

def _telegram_source_link(message: Message):
    """Public t.me link to the original message, when one can be formed."""
    thread = message.message_thread_id or 0
    username = getattr(message.chat, "username", None)
    if username:
        if thread:
            return f"https://t.me/{username}/{thread}/{message.message_id}"
        return f"https://t.me/{username}/{message.message_id}"

    fwd_chat = getattr(message, "forward_from_chat", None)
    fwd_username = getattr(fwd_chat, "username", None) if fwd_chat else None
    fwd_msg_id = getattr(message, "forward_from_message_id", None)
    if fwd_username and fwd_msg_id:
        return f"https://t.me/{fwd_username}/{fwd_msg_id}"
    return None

def _compose_relay_texts(base_text, remaining_files, source_link):
    """Relay body: the message's own text/caption, plus the footer standing in
    for the files that were *not* re-uploaded to GALLERY — every file when the
    mechanic is off, the oversized leftovers when it is on, none at all when all
    of them went through. Returns ``(texts, relay_file_count)``."""
    if remaining_files <= 0:
        return [base_text], None

    prefix = (base_text + "\n") if base_text else ""
    if source_link:
        if remaining_files > 1:
            return [prefix + f"{source_link} (__TG_FILES_{remaining_files}__)"], remaining_files
        return [prefix + source_link], None
    return [prefix + f"[__TG_FILES_{remaining_files}__]"], remaining_files

def _build_telegram_relay_texts(message: Message, grouped_file_count: int | None = None, gallery_uploaded: int = 0):
    """The relay body of one Telegram message as ``(texts, relay_file_count)``.

    Stickers, voice messages and video notes become their own marker instead
    of a file footer — the other side gets a readable "[sticker]" rather than
    a link it cannot render. Everything else goes through
    `_compose_relay_texts` with the files GALLERY could not take."""
    is_sticker = getattr(message, "sticker", None) is not None
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

    return _compose_relay_texts(base_text, total_files - gallery_uploaded, _telegram_source_link(message))

def _relay_variants_for_text(text, base_text, discord_text, telegram_html):
    """Pair a relay text item with its Discord-markdown and Telegram-HTML forms.

    `_build_telegram_relay_texts` may append a file-count/link suffix to the
    message's text or caption. The formatted variants only cover the original
    text, so re-attach the suffix to both of them; that way caption/text
    formatting survives even when a suffix is present (a plain `text == base`
    check would otherwise drop it). Marker-only items (stickers, voice, …) have
    no base text and carry no formatting."""
    if base_text and text.startswith(base_text):
        suffix = text[len(base_text):]
        d = (discord_text or "") + suffix
        h = (telegram_html + escape_html(suffix)) if telegram_html is not None else None
        return d, h
    if text == base_text:
        return discord_text, telegram_html
    return text, None

def _serialize_first_telegram_message(message: Message, *, chat_id: str, bridge_id: int, reply_to_msg_db_id, forward_type, forward_name, external_reply=False):
    """Freeze everything needed to relay this message later, as JSON.

    Held for a user who has not consented yet: an aiogram Message cannot be
    re-fetched from the API, so the rendered texts, both formatted variants,
    the reply/forward context and the file ids are captured now and replayed
    by `_relay_serialized_telegram_payload` when the consent button is
    pressed."""
    texts, relay_file_count = _build_telegram_relay_texts(message)
    source_text = getattr(message, "text", None)
    source_caption = getattr(message, "caption", None)
    tg_html_source = None
    if source_text is not None:
        tg_html_source = getattr(message, "html_text", None)
        discord_text = telegram_entities_to_discord(source_text, getattr(message, "entities", None))
    elif source_caption is not None:
        tg_html_source = getattr(message, "html_text", None)
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
        "external_reply": external_reply,
        "texts": texts,
        "relay_file_count": relay_file_count,
        "base_text": (getattr(message, "text", "") or getattr(message, "caption", "") or ""),
        "discord_text": discord_text,
        "telegram_html": tg_html_source,
        "gallery_candidates": _collect_gallery_candidates(message),
        "source_link": _telegram_source_link(message),
        "total_files": _count_telegram_files(message),
    }
    return json.dumps(payload, ensure_ascii=False)

async def _relay_serialized_telegram_payload(payload_json: str):
    """Relay a message frozen by `_serialize_first_telegram_message`.

    The file upload is deliberately deferred to here rather than done at
    freeze time: a message that never gets consent must not cost an upload,
    and the consent may come long after."""
    try:
        payload = json.loads(payload_json)
    except Exception:
        return

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False):
        """Per-target delivery callback: Telegram inline here, Discord through
        the other half's deliverer."""
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

        if chat["platform"] == "inbox":
            from inbox import deliver_inbox_relay
            return await deliver_inbox_relay(
                chat, header=header, body_plain=body_plain,
                body_telegram_html=body_telegram_html, reply_line=reply_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name,
            )

    base_text = payload.get("base_text", "")
    avatar_url = await _telegram_relay_avatar_url(payload["bridge_id"], payload.get("origin_sender_id"))

    texts = payload.get("texts", [])
    relay_file_count = payload.get("relay_file_count")
    gallery_urls = None
    upload = None
    candidates = payload.get("gallery_candidates") or []
    if candidates and db.bridge_file_relay_enabled(payload["bridge_id"]):
        upload, uploaded = await _upload_telegram_files_to_gallery(candidates)
        if upload:
            gallery_urls = upload["urls"]
            texts, relay_file_count = _compose_relay_texts(
                base_text,
                int(payload.get("total_files") or uploaded) - uploaded,
                payload.get("source_link"),
            )

    relayed_db_id = None
    for text in texts:
        current_discord_text, current_telegram_html = _relay_variants_for_text(
            text, base_text, payload.get("discord_text", ""), payload.get("telegram_html")
        )
        relayed_db_id = await message_relay.relay_message(
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
            telegram_file_count=relay_file_count,
            forward_type=payload.get("forward_type"),
            forward_name=payload.get("forward_name"),
            external_reply=payload.get("external_reply", False),
            avatar_url=avatar_url,
            gallery_urls=gallery_urls,
        )

    if upload and relayed_db_id is not None:
        db.add_gallery_upload(
            relayed_db_id, upload["channel_id"], upload["message_id"],
            json.dumps(upload["urls"], ensure_ascii=False),
            json.dumps([str(payload.get("origin_message_id"))]),
            json.dumps([c["file_id"] for c in candidates], ensure_ascii=False),
        )

async def _flush_media_group(buffer_key):
    """Relay a buffered album once a second has passed with no new part.

    Every incoming part cancels and re-arms this task, so the delay measures
    the gap between parts, not the age of the album."""
    await asyncio.sleep(1.0)
    payload = _media_group_buffer.pop(buffer_key, None)
    if not payload:
        return
    await _relay_from_telegram_impl(
        payload["message"],
        grouped_file_count=payload["count"],
        grouped_message_ids=payload.get("message_ids"),
        grouped_messages=payload.get("messages"),
    )

@router.message(lambda message: not ((getattr(message, "text", "") or "").startswith("/")))
async def relay_from_telegram(message: Message):
    """Front door of the Telegram relay: everything that is not a command.

    An album is buffered (its parts arrive as separate updates) and relayed as
    one message; the part carrying the caption — or failing that the earliest
    part — is the one whose text and sender the copy will use."""
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        files_count = _count_telegram_files(message)
        if files_count > 0:
            thread = message.message_thread_id or 0
            key = (f"{message.chat.id}:{thread}", str(media_group_id))
            payload = _media_group_buffer.get(key)
            if not payload:
                payload = {"message": message, "count": 0, "task": None, "message_ids": [], "messages": []}
                _media_group_buffer[key] = payload

            payload["count"] += files_count
            payload["message_ids"].append(message.message_id)
            payload["messages"].append(message)
            if getattr(message, "caption", None) and not getattr(payload["message"], "caption", None):
                payload["message"] = message
            elif message.message_id < payload["message"].message_id:
                payload["message"] = message

            if payload.get("task"):
                payload["task"].cancel()
            payload["task"] = asyncio.create_task(_flush_media_group(key))
            return

    await _relay_from_telegram_impl(message)

async def _relay_from_telegram_impl(message: Message, grouped_file_count: int | None = None, grouped_message_ids=None, grouped_messages=None):
    """Relay one Telegram message (or one buffered album) into its bridge.

    The filter ladder mirrors the Discord side: bot senders only under
    /allow-bots and never the bridge's own copies, shadow-banned users
    deleted, unverified users prompted for consent with their message frozen
    for later, then the per-user rate limit. Everything after that is
    rendering: forward and reply context, the GALLERY upload, and one
    relay_message call per text item.

    A topic belonging to an inbox conversation is the one exception to the
    consent rung: answering an inbox is staff work in a staff group, not a
    bridged community chat. Such a topic is also where the 'Staff A' label
    replaces the sender's name, while its receiver bot anonymizes."""
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

    is_external_reply = (
        getattr(message, "external_reply", None) is not None
        and not getattr(message, "reply_to_message", None)
    )

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

    is_inbox_conversation = db.is_inbox_bridge(bridge_id)

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

    if not is_bot_sender and not is_inbox_conversation and not db.is_user_verified("telegram", user_id_str, prefix):
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
                        external_reply=is_external_reply,
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
                        external_reply=is_external_reply,
                    )
                )
            return

    reply_to_msg_db_id = pending_reply_to_msg_db_id

    if not rate_limit_ok(("relay", "telegram", user_id_str), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping relay from telegram user %s in %s", user_id_str, origin_chat_id)
        return

    source_messages = sorted(grouped_messages or [message], key=lambda m: m.message_id)
    candidates = []
    for m in source_messages:
        candidates.extend(_collect_gallery_candidates(m))

    gallery_urls = None
    upload = None
    uploaded = 0
    if candidates and db.bridge_file_relay_enabled(bridge_id):
        upload, uploaded = await _upload_telegram_files_to_gallery(candidates)
        if upload:
            gallery_urls = upload["urls"]

    texts, relay_file_count = _build_telegram_relay_texts(
        message, grouped_file_count=grouped_file_count, gallery_uploaded=uploaded
    )

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False):
        """Per-target delivery callback: Telegram inline here, Discord through
        the other half's deliverer."""
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

        if chat["platform"] == "inbox":
            from inbox import deliver_inbox_relay
            return await deliver_inbox_relay(
                chat, header=header, body_plain=body_plain,
                body_telegram_html=body_telegram_html, reply_line=reply_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name,
            )

    source_text = getattr(message, "text", None)
    source_caption = getattr(message, "caption", None)
    tg_html_source = None
    if source_text is not None:
        tg_html_source = getattr(message, "html_text", None)
        discord_text = telegram_entities_to_discord(source_text, getattr(message, "entities", None))
    elif source_caption is not None:
        tg_html_source = getattr(message, "html_text", None)
        discord_text = telegram_entities_to_discord(source_caption, getattr(message, "caption_entities", None))
    else:
        discord_text = texts[0] if texts else ""

    telegram_html = tg_html_source
    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""

    avatar_url = await _telegram_relay_avatar_url(
        bridge_id, message.from_user.id if message.from_user else None
    )

    sender_name = message.from_user.full_name if message.from_user else "Unknown"
    if is_inbox_conversation:
        from inbox import inbox_sender_override, mark_inbox_conversation, touch_inbox_bridge
        touch_inbox_bridge(bridge_id)
        await mark_inbox_conversation(bridge_id, "staff")
        if message.from_user:
            label, anon_avatar = inbox_sender_override(bridge_id, "telegram", message.from_user.id)
            if label:
                sender_name, avatar_url = label, anon_avatar or None

    relayed_db_id = None
    for text in texts:
        current_discord_text, current_telegram_html = _relay_variants_for_text(
            text, base_text, discord_text, telegram_html
        )
        relayed_db_id = await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="telegram",
            origin_chat_id=chat_id,
            origin_message_id=str(message.message_id),
            origin_sender_id=str(message.from_user.id) if message.from_user else "",
            messenger_name="Telegram",
            place_name=message.chat.title or "Private chat",
            sender_name=sender_name,
            text=text,
            discord_text=current_discord_text,
            telegram_html=current_telegram_html,
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            telegram_file_count=relay_file_count,
            forward_type=forward_type,
            forward_name=forward_name,
            external_reply=is_external_reply,
            is_bot_sender=is_bot_sender,
            avatar_url=avatar_url,
            gallery_urls=gallery_urls,
        )

    if grouped_message_ids and relayed_db_id is not None:
        db.record_media_group_members(chat_id, grouped_message_ids, relayed_db_id)

    if upload and relayed_db_id is not None:
        db.add_gallery_upload(
            relayed_db_id, upload["channel_id"], upload["message_id"],
            json.dumps(upload["urls"], ensure_ascii=False),
            json.dumps([str(m.message_id) for m in source_messages]),
            json.dumps([c["file_id"] for c in candidates], ensure_ascii=False),
        )

@router.edited_message()
async def edited_message_handler(message: Message):
    """Propagate an edit of a Telegram origin message into every copy.

    Only the ``kind='main'`` copies carry the body — the per-file follow-ups
    are rebuilt separately, and only when the GALLERY upload actually
    changed. An edit inside an anonymized inbox conversation swaps the author
    name for the same 'Staff A' label the copies were sent under, so
    anonymity survives editing."""
    thread = message.message_thread_id or 0
    origin_chat_id = f"{message.chat.id}:{thread}"
    row = db.cur.execute(
        """
        SELECT id, bridge_id, origin_sender_id FROM messages
        WHERE origin_platform='telegram' AND origin_chat_id=? AND origin_message_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (origin_chat_id, str(message.message_id))
    ).fetchone()
    if not row:
        return

    gallery_urls, gallery_changed = await _refresh_gallery_for_edit(message, row["id"])

    texts, relay_file_count = _build_telegram_relay_texts(
        message, gallery_uploaded=len(gallery_urls or [])
    )
    rendered_text = texts[0] if texts else ""
    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    if getattr(message, "text", None) is not None:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "entities", None))
        telegram_html = getattr(message, "html_text", None)
    else:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "caption_entities", None))
        telegram_html = getattr(message, "html_text", None)

    author_name = message.from_user.full_name if message.from_user else "Unknown"
    if db.is_inbox_bridge(row["bridge_id"]):
        from inbox import inbox_sender_override
        label, _ = inbox_sender_override(row["bridge_id"], "telegram", row["origin_sender_id"])
        if label:
            author_name = label

    header = f"[Telegram | {clean_display_name(message.chat.title or 'Private chat')}] {clean_display_name(author_name)}:"

    copies = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=? AND COALESCE(kind,'main')='main'",
        (row["id"],)
    ).fetchall()
    for c in copies:
        try:
            if c["platform"] == "telegram":
                chat_id_str, th = c["chat_id"].split(":")
                target_lang = get_chat_lang(c["chat_id"])
                edit_plain, edit_html = _relay_variants_for_text(
                    rendered_text, base_text, discord_text, telegram_html
                )
                if relay_file_count is not None:
                    marker = localized_file_count_text(relay_file_count, target_lang)
                    edit_plain = edit_plain.replace(f"__TG_FILES_{relay_file_count}__", marker)
                    if edit_html is not None:
                        edit_html = edit_html.replace(f"__TG_FILES_{relay_file_count}__", escape_html(marker))
                edit_plain, _, edit_html = message_relay.apply_gallery_links(
                    "telegram", edit_plain, edit_plain, edit_html, gallery_urls
                )
                await bot.edit_message_text(
                    chat_id=int(chat_id_str),
                    message_id=int(c["message_id_platform"]),
                    text=build_telegram_text(header, edit_html or escape_html(edit_plain), edit_plain),
                    parse_mode="HTML"
                )
            elif c["platform"] == "discord":
                from discord_bot import bot as dc_bot, edit_discord_relay_copy
                target_lang = get_chat_lang(c["chat_id"])
                edit_discord, _ = _relay_variants_for_text(
                    rendered_text, base_text, discord_text, telegram_html
                )
                if relay_file_count is not None:
                    marker = localized_file_count_text(relay_file_count, target_lang)
                    edit_discord = edit_discord.replace(f"__TG_FILES_{relay_file_count}__", marker)
                _, edit_discord, _ = message_relay.apply_gallery_links(
                    "discord", edit_discord, edit_discord, None, gallery_urls
                )
                channel_id = int(c["chat_id"].split(":")[1])
                ch = dc_bot.get_channel(channel_id)
                if not ch:
                    try:
                        ch = await dc_bot.fetch_channel(channel_id)
                    except Exception:
                        continue
                await edit_discord_relay_copy(ch, c["message_id_platform"], header, edit_discord, message_db_id=row["id"], chat=c)
            elif c["platform"] == "inbox":
                from inbox import edit_inbox_relay_copy
                target_lang = get_chat_lang(c["chat_id"])
                edit_plain, edit_html = _relay_variants_for_text(
                    rendered_text, base_text, discord_text, telegram_html
                )
                if relay_file_count is not None:
                    marker = localized_file_count_text(relay_file_count, target_lang)
                    edit_plain = edit_plain.replace(f"__TG_FILES_{relay_file_count}__", marker)
                    if edit_html is not None:
                        edit_html = edit_html.replace(f"__TG_FILES_{relay_file_count}__", escape_html(marker))
                await edit_inbox_relay_copy(
                    c["chat_id"], c["message_id_platform"], author_name, edit_plain, edit_html
                )
        except Exception:
            pass

    if gallery_changed:
        await _resync_gallery_followups(row["id"], gallery_urls)

async def _refresh_gallery_for_edit(message: Message, message_db_id):
    """Bring a relayed message's GALLERY upload in line with an edit.

    Returns ``(urls, changed)``. A single-message upload is redone, since the
    edit may have replaced the media. An album's upload is kept as it is: the
    edit carries only the one message that was edited, so re-uploading from it
    would drop the album's other files. Any failure keeps the stored links —
    stale links beat a copy that suddenly points at nothing."""
    row = db.get_gallery_upload(message_db_id)
    if not row:
        return None, False

    try:
        stored_urls = json.loads(row["urls"]) or []
        source_ids = json.loads(row["source_message_ids"] or "[]")
    except Exception:
        stored_urls, source_ids = [], []

    candidates = _collect_gallery_candidates(message)
    if len(source_ids) > 1 or not candidates:
        return stored_urls, False

    file_ids = [c["file_id"] for c in candidates]
    try:
        stored_file_ids = json.loads(row["file_ids"] or "[]")
    except Exception:
        stored_file_ids = []
    if stored_file_ids and file_ids == stored_file_ids:
        return stored_urls, False

    upload, _ = await _upload_telegram_files_to_gallery(candidates)
    if not upload:
        return stored_urls, False

    from discord_bot import delete_gallery_message
    await delete_gallery_message(row["channel_id"], row["gallery_message_id"])
    db.add_gallery_upload(
        message_db_id, upload["channel_id"], upload["message_id"],
        json.dumps(upload["urls"], ensure_ascii=False), row["source_message_ids"],
        json.dumps(file_ids, ensure_ascii=False),
    )
    return upload["urls"], upload["urls"] != stored_urls

async def _resync_gallery_followups(message_db_id, gallery_urls):
    """Rebuild the per-file follow-up messages of a Telegram copy after a
    re-upload handed out new links: the old ones point at a deleted GALLERY
    message, and their number may have changed."""
    rows = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=? AND kind='file'",
        (message_db_id,)
    ).fetchall()
    if not rows:
        return

    chat_ids = []
    for r in rows:
        if r["platform"] != "telegram":
            continue
        if r["chat_id"] not in chat_ids:
            chat_ids.append(r["chat_id"])
        try:
            await bot.delete_message(int(r["chat_id"].split(":")[0]), int(r["message_id_platform"]))
        except Exception:
            pass

    db.cur.execute(
        "DELETE FROM message_copies WHERE message_id=? AND kind='file'",
        (message_db_id,)
    )
    db.conn.commit()

    for chat_id in chat_ids:
        chat_id_str, thread = chat_id.split(":")
        for url in (gallery_urls or [])[1:]:
            try:
                sent = await bot.send_message(
                    chat_id=int(chat_id_str),
                    message_thread_id=int(thread) or None,
                    text=escape_html(url),
                    parse_mode="HTML",
                )
            except Exception:
                continue
            db.cur.execute(
                """
                INSERT INTO message_copies
                (message_id, platform, chat_id, message_id_platform, kind)
                VALUES (?,'telegram',?,?,'file')
                """,
                (message_db_id, chat_id, str(sent.message_id))
            )
    db.conn.commit()
