from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated
import db, message_relay
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, get_chat_lang,
    localized_forward_from_chat, localized_forward_from_user, localized_forward_unknown,
    localized_file_count_text, localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    set_chat_lang
)
from config import TELEGRAM_TOKEN
import time

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


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

    # send confirmation to this chat in its language
    lang = get_chat_lang(chat_id)
    try:
        await bot.send_message(
            chat_id=int(message.chat.id),
            message_thread_id=int(thread) or None,
            text=localized_bot_joined(lang)
        )
    except Exception:
        # try to at least notify the command issuer
        await message.reply(f"Chat attached to bridge {bridge_id}")
    else:
        await message.reply(f"Chat attached to bridge {bridge_id}")

    # notify other chats in this bridge
    # prepare origin display names
    channel_or_topic = f"topic {thread}" if thread else (message.chat.title or f"chat {message.chat.id}")
    server_name = message.chat.title or "Private chat"

    rows = db.get_bridge_chats(bridge_id)
    for c in rows:
        if c["platform"] == "telegram" and c["chat_id"] == chat_id:
            continue
        target_lang = get_chat_lang(c["chat_id"])
        notify = localized_bridge_join(channel_or_topic, server_name, target_lang)

        if c["platform"] == "telegram":
            # send to telegram chat
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
            # send to discord channel via discord bot
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

    # Если пользователь передал аргумент — запрещаем и объясняем.
    if len(parts) > 1:
        await message.reply("Удаление по ID в Telegram не поддерживается. Запустите /rfb в том чате/теме, который нужно удалить.")
        return

    # permission checks: bot admins or chat admin for this chat
    user_id = message.from_user.id
    if is_admin("telegram", user_id) or is_chat_admin("telegram", current_chat_id, user_id):
        allowed = True
    else:
        allowed = False

    if not allowed:
        await message.reply("No permission")
        return

    # find bridge for current chat
    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (current_chat_id,)).fetchone()
    if not row:
        await message.reply("Chat is not attached to any bridge")
        return

    bridge_id = row["bridge_id"]

    # build origin display names
    channel_or_topic = f"topic {thread}" if thread else (message.chat.title or f"chat {message.chat.id}")
    server_name = message.chat.title or "Private chat"

    # remove chat from DB
    db.cur.execute("DELETE FROM chats WHERE chat_id=?", (current_chat_id,))
    # cleanup related settings/admins for this prefix? (optional; currently removing only chats)
    db.conn.commit()

    # notify other chats in bridge
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

@router.message()
async def relay_from_telegram(message: Message):
    if message.from_user and message.from_user.is_bot:
        return

    is_sticker = getattr(message, "sticker", None) is not None

    thread = message.message_thread_id or 0
    origin_chat_id = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(origin_chat_id)

    forward_line = None
    if getattr(message, "forward_from_chat", None):
        forward_line = localized_forward_from_chat(message.forward_from_chat.title or "unknown", lang)
    elif getattr(message, "forward_from", None):
        try:
            name = message.forward_from.full_name
        except Exception:
            name = getattr(message.forward_from, "username", "unknown")
        forward_line = localized_forward_from_user(name, lang)
    elif getattr(message, "forward_sender_name", None):
        forward_line = localized_forward_unknown(lang)

    is_forward = forward_line is not None

    chat_id = origin_chat_id

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

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

    if is_sticker:
        texts = ["[Sticker]"]

    elif is_forward:
        base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
        texts = [f"{forward_line}\n{base_text}".strip()]

    else:
        base_text = getattr(message, "text", "") or getattr(message, "caption", "") or ""

        files = []
        if getattr(message, "document", None):
            files.append(("document", message.document.file_id))
        if getattr(message, "photo", None):
            try:
                files.append(("photo", message.photo[-1].file_id))
            except Exception:
                pass
        if getattr(message, "video", None):
            files.append(("video", message.video.file_id))

        if files:
            if getattr(message.chat, "username", None):
                link = f"https://t.me/{message.chat.username}/{message.message_id}"
                texts.append((base_text + "\n" if base_text else "") + link)
                for _ in files[1:]:
                    texts.append(link)
            else:
                n = len(files)
                marker = localized_file_count_text(n, lang)
                texts.append((base_text + "\n" if base_text else "") + f"[{marker}]")
                for _ in files[1:]:
                    texts.append(f"[{marker}]")
        else:
            texts = [base_text]

    async def send_to_chat(chat, text):
        if chat["platform"] == "telegram":
            chat_id_str, thread = chat["chat_id"].split(":")
            sent = await bot.send_message(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text
            )
            return str(sent.message_id)

        if chat["platform"] == "discord":
            from discord_bot import bot as dc_bot
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = dc_bot.get_channel(channel_id)
            if not channel:
                return None
            sent = await channel.send(text)
            return str(sent.id)

    for text in texts:
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="telegram",
            origin_chat_id=chat_id,
            origin_message_id=str(message.message_id),
            messenger_name="Telegram",
            place_name=message.chat.title or "Private chat",
            sender_name=message.from_user.full_name if message.from_user else "Unknown",
            text=text,
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat
        )


@router.message(Command("setadmin"))
async def setadmin(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply("Usage: /setadmin <user_id>")
        return

    target_user_id = parts[1]

    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"

    if not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_id, message.from_user.id)
    ):
        await message.reply("No permission")
        return

    db.cur.execute(
        """
        INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id)
        VALUES (?,?,?)
        """,
        ("telegram", chat_id, target_user_id)
    )
    db.conn.commit()

    await message.reply(
        f"User `{target_user_id}` added as chat admin"
    )

@router.message(Command("lang"))
async def set_lang_handler(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply("Usage: /lang <ru|en|uk|pl|es|pt>")
        return

    code = parts[1].strip().lower()

    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"

    if not (
        is_admin("telegram", message.from_user.id)
        or is_chat_admin("telegram", chat_key, message.from_user.id)
    ):
        await message.reply("No permission")
        return

    try:
        # используем utils.set_chat_lang — там есть проверка на поддерживаемый язык
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
            str(ref.message_id),
            hours,
            messages,
            int(time.time()),
            0
        )
    )
    db.conn.commit()

    await message.reply("Rules saved and will be posted automatically")


async def main():
    db.init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
