from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery
import db, message_relay
from message_relay import telegram_entities_to_discord, escape_html
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, get_chat_lang,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    set_chat_lang
)
from config import TELEGRAM_TOKEN
import time
import asyncio

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

_media_group_buffer = {}

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
    if getattr(message, "animation", None):
        count += 1
    return count

async def _flush_media_group(buffer_key):
    await asyncio.sleep(1.0)
    payload = _media_group_buffer.pop(buffer_key, None)
    if not payload:
        return
    await _relay_from_telegram_impl(payload["message"], grouped_file_count=payload["count"])

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
    if not is_admin("telegram", message.from_user.id):
        await message.reply("No permission")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /atb <bridge_id>")
        return

    try:
        bridge_id = int(parts[1])
    except ValueError:
        await message.reply("Invalid bridge id")
        return

    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"

    if db.chat_exists(chat_id):
        await message.reply("Chat already attached to a bridge")
        return

    db.attach_chat("telegram", chat_id, bridge_id)

    lang = get_chat_lang(chat_id)
    try:
        await bot.send_message(
            chat_id=int(message.chat.id),
            message_thread_id=int(thread) or None,
            text=localized_bot_joined(lang)
        )
    except Exception:
        await message.reply(f"Chat attached to bridge {bridge_id}")
    else:
        await message.reply(f"Chat attached to bridge {bridge_id}")

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

    if len(parts) > 1:
        await message.reply("Удаление по ID в Telegram не поддерживается. Запустите /rfb в том чате/теме, который нужно удалить.")
        return

    user_id = message.from_user.id
    if is_admin("telegram", user_id) or is_chat_admin("telegram", current_chat_id, user_id):
        allowed = True
    else:
        allowed = False

    if not allowed:
        await message.reply("No permission")
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (current_chat_id,)).fetchone()
    if not row:
        await message.reply("Chat is not attached to any bridge")
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

    await message.reply("Chat removed from bridge")

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
                payload = {"message": message, "count": 0, "task": None}
                _media_group_buffer[key] = payload

            payload["count"] += files_count
            if getattr(message, "caption", None) and not getattr(payload["message"], "caption", None):
                payload["message"] = message
            elif message.message_id < payload["message"].message_id:
                payload["message"] = message

            if payload.get("task"):
                payload["task"].cancel()
            payload["task"] = asyncio.create_task(_flush_media_group(key))
            return

    await _relay_from_telegram_impl(message)

async def _relay_from_telegram_impl(message: Message, grouped_file_count: int | None = None):
    if message.from_user and message.from_user.is_bot:
        return

    is_sticker = getattr(message, "sticker", None) is not None

    thread = message.message_thread_id or 0
    origin_chat_id = f"{message.chat.id}:{thread}"
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

    chat_id = origin_chat_id

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    prefix = str(message.chat.id)
    user_id_str = str(message.from_user.id)

    if db.is_shadow_banned("telegram", user_id_str):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            pass
        return

    if not db.is_user_verified("telegram", user_id_str, prefix):
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
                db.add_pending_consent("telegram", prefix, user_id_str, str(sent.message_id), chat_key)
            except Exception:
                pass
            return

    reply_to_name = None
    if (
        not is_forward
        and getattr(message, "reply_to_message", None)
        and message.reply_to_message.message_id != message.message_thread_id
    ):
        if message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
            reply_to_name = extract_username_from_bot_message(
                getattr(message.reply_to_message, "text", "") or ""
            )
        else:
            try:
                reply_to_name = message.reply_to_message.from_user.full_name
            except Exception:
                reply_to_name = getattr(message.reply_to_message.from_user, "username", None)

    texts = []
    relay_file_count = None

    if is_sticker:
        texts = ["__TG_STICKER__"]
    else:
        base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""

        total_files = grouped_file_count if grouped_file_count is not None else _count_telegram_files(message)

        if total_files > 0:
            username = getattr(message.chat, "username", None)
            thread_id = thread
            if username:
                if total_files > 1:
                    relay_file_count = total_files
                    if thread_id:
                        link = f"https://t.me/{username}/{thread_id}/{message.message_id}"
                    else:
                        link = f"https://t.me/{username}/{message.message_id}"
                    texts = [(base_text + "\n" if base_text else "") + f"{link} (__TG_FILES_{total_files}__)"]
                else:
                    if thread_id:
                        link = f"https://t.me/{username}/{thread_id}/{message.message_id}"
                    else:
                        link = f"https://t.me/{username}/{message.message_id}"
                    texts = [(base_text + "\n" if base_text else "") + link]
            else:
                relay_file_count = total_files
                texts = [(base_text + "\n" if base_text else "") + f"[__TG_FILES_{total_files}__]"]
        else:
            texts = [base_text]

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line):
        if chat["platform"] == "telegram":
            chat_id_str, thread = chat["chat_id"].split(":")
            if body_telegram_html:
                body_html = body_telegram_html
            else:
                body_html = escape_html(body_plain)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            html_text = f"{escape_html(header)}\n{body_html}".strip()
            sent = await bot.send_message(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=html_text,
                parse_mode="HTML"
            )
            return str(sent.message_id)

        if chat["platform"] == "discord":
            from discord_bot import bot as dc_bot
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = dc_bot.get_channel(channel_id)
            if not channel:
                return None
            body = body_discord
            if reply_line:
                body = f"{reply_line}\n{body}"
            sent = await channel.send(f"{header}\n{body}".strip())
            return str(sent.id)

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

    for text in texts:
        current_discord_text = discord_text if text == (getattr(message, "text", "") or getattr(message, "caption", "") or "") else text
        current_telegram_html = telegram_html if text == (getattr(message, "text", "") or getattr(message, "caption", "") or "") else None
        await message_relay.relay_message(
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
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat,
            telegram_file_count=relay_file_count,
            forward_type=forward_type,
            forward_name=forward_name,
        )

@router.message(Command("setadmin"))
async def setadmin(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("Usage: /setadmin <user_id_or_username>")
        return

    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"
    if not (is_admin("telegram", message.from_user.id) or is_chat_admin("telegram", chat_id, message.from_user.id)):
        await message.reply("No permission")
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply("Could not resolve username")
            return
    else:
        uid = int(identifier)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        await message.reply("Chat is not attached to any bridge")
        return

    bridge_id = row["bridge_id"]
    db.add_bridge_admin(bridge_id, uid)
    await message.reply(f"User `{uid}` added as bridge admin")

@router.message(Command("lang"))
async def set_lang_handler(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply("Usage: /lang <ru|en|uk|pl|es|pt>")
        return

    code = parts[1].strip().lower()

    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"

    has_permission = (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_key, message.from_user.id)
        or await is_telegram_native_admin(message.chat.id, message.from_user.id)
    )
    if not has_permission:
        await message.reply("No permission")
        return

    try:
        set_chat_lang(chat_key, code)
    except ValueError:
        await message.reply("Unsupported language. Supported: ru, uk, pl, en, es, pt")
        return
    except Exception as e:
        await message.reply(f"Error saving language: {e}")
        return

    await message.reply(f"Language for this topic/thread set to: {code}")

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
    if not message.reply_to_message:
        await message.reply("Command must be a reply to a message containing rules")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /remindrules <hours> [messages]")
        return

    try:
        hours = int(parts[1])
    except ValueError:
        await message.reply("First parameter must be an integer (hours)")
        return

    messages = int(parts[2]) if len(parts) > 2 else None

    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"

    if not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
    ):
        await message.reply("No permission")
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await message.reply("Chat is not attached to any bridge")
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
            hours,
            messages,
            int(time.time()) - (hours * 3600),
            0
        )
    )
    db.conn.commit()

    await message.reply("Rules saved and will be posted automatically")

@router.callback_query(lambda c: c.data and c.data.startswith("verify:"))
async def handle_verify_callback(query: CallbackQuery):
    """
    Expected callback_data: verify:telegram|<prefix>|<user_id>
    Only the target user can confirm. On confirm — add verified and remove pending + bot message.
    """
    data = query.data
    try:
        _, payload = data.split(":", 1)
        parts = payload.split("|")
        platform = parts[0]
        prefix = parts[1]
        target_user_id = parts[2]
    except Exception:
        await query.answer("Invalid data", show_alert=True)
        return

    if str(query.from_user.id) != str(target_user_id):
        await query.answer("This button is not for you", show_alert=True)
        return

    db.add_verified_user("telegram", target_user_id, "*", days_valid=365)

    pend = db.get_pending_consent("telegram", prefix, target_user_id)
    if pend:
        chat_key = pend["chat_key"]
        bot_msg_id = pend["bot_message_id"]
        try:
            chat_id_str, th = chat_key.split(":")
            await bot.delete_message(chat_id=int(chat_id_str), message_id=int(bot_msg_id))
        except Exception:
            pass
        db.remove_pending_consent("telegram", prefix, target_user_id)

    await query.answer("Спасибо — вы подтверждены", show_alert=False)

@router.message(Command("verify"))
async def verify_cmd(message: Message):
    thread = message.message_thread_id or 0
    prefix = str(message.chat.id)
    user_id = str(message.from_user.id)
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

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
        await message.reply("Could not send verification message. Bot may lack permissions.")

@router.message(Command("unverify"))
async def unverify_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("Usage: /unverify <user_id_or_username>")
        return

    requester = message.from_user.id
    if not is_admin("telegram", requester):
        await message.reply("No permission")
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply("Could not resolve username to user id")
            return
    else:
        uid = int(identifier)

    db.cur.execute("DELETE FROM verified_users WHERE platform='telegram' AND user_id=?", (str(uid),))
    db.conn.commit()
    await message.reply(f"User {uid} unverified (removed from DB).")

@router.message(Command("shadow-ban"))
async def shadow_ban_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("Usage: /shadow-ban <user_id_or_username>")
        return

    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
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
        await message.reply("No permission")
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply("Could not resolve username")
            return
    else:
        uid = int(identifier)

    db.add_shadow_ban("telegram", uid)
    await message.reply(f"User {uid} shadow-banned on Telegram (messages will not be relayed).")

@router.message(Command("whois"))
async def whois_cmd(message: Message):
    reply = getattr(message, "reply_to_message", None)
    replied_id = str(
        getattr(reply, "message_id", "")
        or getattr(message, "reply_to_message_id", "")
        or ""
    )

    if not replied_id.strip():
        await message.reply("Use this command in reply to a bot-relay message.")
        return

    chat_key = f"{message.chat.id}:{message.message_thread_id or 0}"

    row = db.cur.execute(
        "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
        ("telegram", chat_key, replied_id)
    ).fetchone()

    if not row:
        await message.reply("Could not find origin for that message.")
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await message.reply("Origin entry missing")
        return

    origin_platform = msg_row["origin_platform"]
    origin_chat_id = msg_row["origin_chat_id"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    if origin_platform != "telegram":
        await message.reply("Origin is not Telegram; use /whois in corresponding platform or use Discord whois.")
        return

    try:
        prefix = origin_chat_id.split(":",1)[0]
        member = await bot.get_chat_member(int(prefix), int(origin_sender_id))
        u = member.user
        uname = f"@{u.username}" if u.username else "—"
        full = u.full_name or (u.first_name or "")
        await message.reply(f"Nickname: {full}\nUsername: {uname}\nID: {u.id}")
    except Exception as e:
        await message.reply(f"Could not fetch user data: {e}")

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

    base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
    if getattr(message, "text", None) is not None:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "entities", None))
        telegram_html = getattr(message, "html_text", None)
    else:
        discord_text = telegram_entities_to_discord(base_text, getattr(message, "caption_entities", None))
        telegram_html = getattr(message, "html_caption", None)

    header = f"[Telegram | {message.chat.title or 'Private chat'}] {message.from_user.full_name if message.from_user else 'Unknown'}:"

    copies = db.cur.execute("SELECT * FROM message_copies WHERE message_id=?", (row["id"],)).fetchall()
    for c in copies:
        try:
            if c["platform"] == "telegram":
                chat_id_str, th = c["chat_id"].split(":")
                text_html = f"{escape_html(header)}\n{telegram_html or escape_html(base_text)}"
                await bot.edit_message_text(
                    chat_id=int(chat_id_str),
                    message_id=int(c["message_id_platform"]),
                    text=text_html,
                    parse_mode="HTML"
                )
            elif c["platform"] == "discord":
                from discord_bot import bot as dc_bot
                channel_id = int(c["chat_id"].split(":")[1])
                channel = dc_bot.get_channel(channel_id)
                if not channel:
                    continue
                msg = await channel.fetch_message(int(c["message_id_platform"]))
                await msg.edit(content=f"{header}\n{discord_text}".strip())
        except Exception:
            pass

async def main():
    db.init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
