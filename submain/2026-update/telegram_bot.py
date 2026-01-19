from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
import db, message_relay
from utils import is_admin, extract_username_from_bot_message
from config import TELEGRAM_TOKEN

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

@router.message(Command("atb"))
async def atb(message: Message):
    if not is_admin("telegram", message.from_user.id):
        await message.reply("Нет прав")
        return

    bridge_id = int(message.text.split()[1])
    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"

    if db.chat_exists(chat_id):
        await message.reply("Чат уже в мосту")
        return

    db.attach_chat("telegram", chat_id, bridge_id)
    await message.reply(f"Чат подключён к мосту {bridge_id}")

@router.message()
async def relay_from_telegram(message: Message):
    if message.from_user.is_bot:
        return

    thread = message.message_thread_id or 0
    chat_id = f"{message.chat.id}:{thread}"

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    reply_to_name = None
    if message.reply_to_message:
        if message.reply_to_message.from_user.is_bot:
            reply_to_name = extract_username_from_bot_message(
                message.reply_to_message.text or ""
            )
        else:
            reply_to_name = message.reply_to_message.from_user.full_name

    files = []
    if message.document:
        files.append(message.document.file_id)
    if message.photo:
        files.append(message.photo[-1].file_id)
    if message.video:
        files.append(message.video.file_id)

    texts = []
    if files:
        file = await bot.get_file(files[0])
        texts.append((message.text or "") + "\n" +
                     f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}")
        for f in files[1:]:
            file = await bot.get_file(f)
            texts.append(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}")
    else:
        texts.append(message.text or "")

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
            place_name=message.chat.title or "Личный чат",
            sender_name=message.from_user.full_name,
            text=text,
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat
        )

async def main():
    db.init()
    await dp.start_polling(bot)
