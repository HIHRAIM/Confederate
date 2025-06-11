# Стабильный код версии 2.3 в виде одного файла
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, filters, ContextTypes

DISCORD_TOKEN = "ODg4MzE0Njg5ODI0NjM2OTk4.GETBov.S29XND1_1AzIqqlbQe8n4PKmmo5nnMzdjorjbU"
TELEGRAM_TOKEN = "8089473914:AAH_IPAgAX-XnvcpbtW4nMEfDBw4NHqiiHY"

DISCORD_CHANNEL_IDS = [
    1381670819020673126,
    1365287139960422400,
]

TELEGRAM_TARGETS = [
    {"chat_id": -1002336919485, "topic_id": 1987},
    {"chat_id": -1002262445485, "topic_id": 210},
]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

discord_to_telegram_queue = asyncio.Queue()
telegram_to_discord_queue = asyncio.Queue()
telegram_to_telegram_queue = asyncio.Queue()

discord_to_telegram_map = {}
telegram_to_discord_map = {}
telegram_crosspost_map = {}
discord_crosspost_map = {}

def format_message(platform, username, text):
    return f"[{platform}] {username}:\n{text}"

def get_msk_time():
    dt = datetime.utcnow() + timedelta(hours=3)
    return dt.strftime("%d.%m.%Y %H:%M:%S")

def tg_target_key(target):
    return (target["chat_id"], target.get("topic_id"))

def tg_source_key(chat_id, topic_id, msg_id):
    return (chat_id, topic_id, msg_id)

@discord_bot.event
async def on_ready():
    print(f"Дискорд-бот вошёл как {discord_bot.user}")
    if not hasattr(discord_bot, 'telegram_worker_started'):
        asyncio.create_task(telegram_to_discord_worker())
        discord_bot.telegram_worker_started = True
    if not hasattr(discord_bot, 'telegram_to_telegram_worker_started'):
        asyncio.create_task(telegram_to_telegram_worker())
        discord_bot.telegram_to_telegram_worker_started = True

@discord_bot.event
async def on_message(message):
    if message.author.bot:
        return
    channel_id = None
    if message.channel.id in DISCORD_CHANNEL_IDS:
        channel_id = message.channel.id
    elif hasattr(message, "thread") and message.thread and message.thread.id in DISCORD_CHANNEL_IDS:
        channel_id = message.thread.id
    if not channel_id:
        return

    username = str(message.author)
    text = message.content or ""
    attachment_links = "\n".join(a.url for a in message.attachments) if message.attachments else ""
    body = format_message("Дискорд", username, text)
    if attachment_links:
        body += f"\nВложения:\n{attachment_links}"

    print(f"[ОТЛАДКА] Отправка в discord_to_telegram_queue: {body}")
    await discord_to_telegram_queue.put(((channel_id, message.id), body))

    for dst_chan_id in DISCORD_CHANNEL_IDS:
        if dst_chan_id == channel_id:
            continue
        dst_channel = discord_bot.get_channel(dst_chan_id)
        if dst_channel:
            try:
                sent = await dst_channel.send(body)
                discord_crosspost_map.setdefault((channel_id, message.id), {})[dst_chan_id] = sent.id
                print(f"[ОТЛАДКА] Кросспостинг сообщения из {channel_id}/{message.id} в {dst_chan_id}/{sent.id}")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось кросспостить в канал/тред Дискорда {dst_chan_id}: {e}")
        else:
            print(f"[ОШИБКА] Не найден канал/тред Дискорда для кросспоста: {dst_chan_id}")

@discord_bot.event
async def on_message_edit(before, after):
    channel_id = None
    if after.channel.id in DISCORD_CHANNEL_IDS:
        channel_id = after.channel.id
    elif hasattr(after, "thread") and after.thread and after.thread.id in DISCORD_CHANNEL_IDS:
        channel_id = after.thread.id
    if not channel_id or after.author.bot:
        return
    username = str(after.author)
    text = after.content or ""
    attachment_links = "\n".join(a.url for a in after.attachments) if after.attachments else ""
    body = format_message("Дискорд", username, text)
    if attachment_links:
        body += f"\nВложения:\n{attachment_links}"

    for key, tg_msg_id in list(discord_to_telegram_map.items()):
        tg_chat_id, tg_topic_id, d_chan_id, d_msg_id = key
        if d_chan_id == channel_id and d_msg_id == after.id:
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=tg_chat_id,
                    message_id=tg_msg_id,
                    text=body,
                    parse_mode=ParseMode.HTML,
                    )
                print(f"[ОТЛАДКА] Изменено сообщение в Телеграм {tg_msg_id} для сообщения Дискорда {after.id} в {tg_chat_id} (тема {tg_topic_id})")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось изменить сообщение в Телеграм: {e}")

    crossposts = discord_crosspost_map.get((channel_id, after.id), {})
    for dst_chan_id, dst_msg_id in crossposts.items():
        dst_channel = discord_bot.get_channel(dst_chan_id)
        if dst_channel:
            try:
                dst_msg = await dst_channel.fetch_message(dst_msg_id)
                await dst_msg.edit(content=body)
                print(f"[ОТЛАДКА] Изменено кросспостнутое сообщение в Дискорде {dst_chan_id}/{dst_msg_id}")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось изменить кросспостнутое сообщение в Дискорде: {e}")

@discord_bot.event
async def on_message_delete(message):
    channel_id = None
    if message.channel.id in DISCORD_CHANNEL_IDS:
        channel_id = message.channel.id
    elif hasattr(message, "thread") and message.thread and message.thread.id in DISCORD_CHANNEL_IDS:
        channel_id = message.thread.id
    if not channel_id:
        return
    for target in TELEGRAM_TARGETS:
        key = (target["chat_id"], target.get("topic_id"), channel_id, message.id)
        telegram_msg_id = discord_to_telegram_map.pop(key, None)
        if telegram_msg_id:
            try:
                await telegram_app.bot.delete_message(
                    chat_id=target["chat_id"],
                    message_id=telegram_msg_id
                )
                print(f"[ОТЛАДКА] Удалено сообщение в Телеграм {telegram_msg_id} для сообщения Дискорда {message.id} в {channel_id} (группа {target['chat_id']}, тема {target.get('topic_id')})")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось удалить сообщение в Телеграм: {e}")
    crossposts = discord_crosspost_map.pop((channel_id, message.id), {})
    for dst_chan_id, dst_msg_id in crossposts.items():
        dst_channel = discord_bot.get_channel(dst_chan_id)
        if dst_channel:
            try:
                dst_msg = await dst_channel.fetch_message(dst_msg_id)
                await dst_msg.delete()
                print(f"[ОТЛАДКА] Удалено кросспостнутое сообщение в Дискорде {dst_chan_id}/{dst_msg_id}")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось удалить кросспостнутое сообщение в Дискорде: {e}")

async def telegram_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    source_chat_id = msg.chat_id
    source_topic_id = getattr(msg, "message_thread_id", None)
    source_msg_id = msg.message_id

    if not any(
        source_chat_id == t["chat_id"] and (t["topic_id"] is None or t["topic_id"] == source_topic_id)
        for t in TELEGRAM_TARGETS
    ):
        return
    if msg.edit_date:
        return

    sender = update.effective_user.full_name or update.effective_user.username or "Неизвестно"
    text = msg.text or ""
    attachment_links = []
    if msg.photo:
        for p in msg.photo:
            file = await context.bot.get_file(p.file_id)
            attachment_links.append(file.file_path)
    if msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        attachment_links.append(file.file_path)
    if msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        attachment_links.append(file.file_path)
    body = format_message("Телеграм", sender, text)
    if attachment_links:
        body += "\nВложения:\n" + "\n".join(attachment_links)
    print(f"[ОТЛАДКА] Помещено в telegram_to_discord_queue: {body}")
    await telegram_to_discord_queue.put(((source_chat_id, source_topic_id, source_msg_id), body))
    print(f"[ОТЛАДКА] Помещено в telegram_to_telegram_queue: {body}")
    await telegram_to_telegram_queue.put((source_chat_id, source_topic_id, source_msg_id, body))

async def telegram_edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    source_chat_id = msg.chat_id
    source_topic_id = getattr(msg, "message_thread_id", None)
    source_msg_id = msg.message_id

    if not any(
        source_chat_id == t["chat_id"] and (t["topic_id"] is None or t["topic_id"] == source_topic_id)
        for t in TELEGRAM_TARGETS
    ):
        return
    if not msg.edit_date:
        return
    sender = update.effective_user.full_name or update.effective_user.username or "Неизвестно"
    text = msg.text or ""
    attachment_links = []
    if msg.photo:
        for p in msg.photo:
            file = await context.bot.get_file(p.file_id)
            attachment_links.append(file.file_path)
    if msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        attachment_links.append(file.file_path)
    if msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        attachment_links.append(file.file_path)
    body = format_message("Телеграм", sender, text)
    key = (source_chat_id, source_topic_id, source_msg_id)

    for key2, msg_id2 in list(discord_to_telegram_map.items()):
        tg_chat_id2, tg_topic_id2, chan_id2, disc_msg_id2 = key2
        if (source_chat_id, source_topic_id, chan_id2, disc_msg_id2) == (tg_chat_id2, tg_topic_id2, chan_id2, disc_msg_id2):
            try:
                await telegram_app.bot.edit_message_text(
                    chat_id=tg_chat_id2,
                    message_id=msg_id2,
                    text=body,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=tg_topic_id2 if tg_topic_id2 else None
                )
                print(f"[ОТЛАДКА] Изменено основное сообщение Telegram {tg_chat_id2} тема {tg_topic_id2} id {msg_id2}")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось изменить основное сообщение Telegram: {e}")

    tg_xposts = telegram_crosspost_map.get(key, {})
    for (dst_chat_id, dst_topic_id), dst_msg_id in tg_xposts.items():
        try:
            await telegram_app.bot.edit_message_text(
                chat_id=dst_chat_id,
                message_id=dst_msg_id,
                text=body,
                parse_mode=ParseMode.HTML,
                message_thread_id=dst_topic_id if dst_topic_id else None
            )
            print(f"[ОТЛАДКА] Изменено кросспостнутое сообщение в Telegram {dst_chat_id} тема {dst_topic_id} id {dst_msg_id}")
        except Exception as e:
            print(f"[ОШИБКА] Не удалось изменить кросспостнутое сообщение в Telegram: {e}")

    discord_mapping = telegram_to_discord_map.get(key)
    if discord_mapping:
        for chan_id, disc_msg_id in discord_mapping:
            await try_edit_discord_message(chan_id, disc_msg_id, body)

async def try_edit_discord_message(chan_id, disc_msg_id, new_content):
    channel = discord_bot.get_channel(chan_id)
    if channel:
        try:
            discord_msg = await channel.fetch_message(disc_msg_id)
            await discord_msg.edit(content=new_content)
            print(f"[ОТЛАДКА] Изменено сообщение Дискорда {disc_msg_id} в {chan_id}")
        except Exception as e:
            print(f"[ОШИБКА] Не удалось изменить сообщение Дискорда: {e}")

telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED, telegram_message_handler))
telegram_app.add_handler(MessageHandler(filters.UpdateType.EDITED, telegram_edit_handler))

async def discord_to_telegram_worker():
    while True:
        (discord_chan_id, discord_msg_id), body = await discord_to_telegram_queue.get()
        print(f"[ОТЛАДКА] Получено из discord_to_telegram_queue: {body}")
        for target in TELEGRAM_TARGETS:
            try:
                if target["topic_id"] is not None:
                    sent = await telegram_app.bot.send_message(
                        chat_id=target["chat_id"],
                        text=body,
                        message_thread_id=target["topic_id"]
                    )
                else:
                    sent = await telegram_app.bot.send_message(
                        chat_id=target["chat_id"],
                        text=body
                    )
                key = (target["chat_id"], target.get("topic_id"), discord_chan_id, discord_msg_id)
                discord_to_telegram_map[key] = sent.message_id
                back_key = (target["chat_id"], target.get("topic_id"), sent.message_id)
                if back_key not in telegram_to_discord_map:
                    telegram_to_discord_map[back_key] = []
                telegram_to_discord_map[back_key].append((discord_chan_id, discord_msg_id))
                print(f"[ОТЛАДКА] Сообщение отправлено в Telegram {target['chat_id']} (тема: {target.get('topic_id')})")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось отправить сообщение в Telegram {target['chat_id']}: {e}")

async def telegram_to_discord_worker():
    await discord_bot.wait_until_ready()
    print("[ОТЛАДКА] telegram_to_discord_worker запущен, Дискорд-бот готов.")
    while True:
        (telegram_chat_id, telegram_topic_id, telegram_msg_id), body = await telegram_to_discord_queue.get()
        print(f"[ОТЛАДКА] Получено из telegram_to_discord_queue: {body}")
        for chan_id in DISCORD_CHANNEL_IDS:
            try:
                channel = discord_bot.get_channel(chan_id)
                if channel:
                    sent = await channel.send(body)
                    back_key = (telegram_chat_id, telegram_topic_id, telegram_msg_id)
                    if back_key not in telegram_to_discord_map:
                        telegram_to_discord_map[back_key] = []
                    telegram_to_discord_map[back_key].append((chan_id, sent.id))
                    discord_to_telegram_map[(telegram_chat_id, telegram_topic_id, chan_id, sent.id)] = telegram_msg_id
                    print(f"[ОТЛАДКА] Сообщение отправлено в канал/тред Дискорда {chan_id} и отображено Telegram {telegram_msg_id} <-> Discord {sent.id}")
                else:
                    print(f"[ОШИБКА] Канал/тред Дискорда с ID {chan_id} не найден!")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось отправить сообщение в Дискорд: {e}")

async def telegram_to_telegram_worker():
    print("[ОТЛАДКА] telegram_to_telegram_worker запущен.")
    while True:
        src_chat_id, src_topic_id, src_msg_id, body = await telegram_to_telegram_queue.get()
        for target in TELEGRAM_TARGETS:
            dst_chat_id, dst_topic_id = target["chat_id"], target.get("topic_id")
            if dst_chat_id == src_chat_id and ((dst_topic_id or None) == (src_topic_id or None)):
                continue
            try:
                if dst_topic_id is not None:
                    sent = await telegram_app.bot.send_message(
                        chat_id=dst_chat_id,
                        text=body,
                        message_thread_id=dst_topic_id
                    )
                else:
                    sent = await telegram_app.bot.send_message(
                        chat_id=dst_chat_id,
                        text=body
                    )
                tg_xkey = (src_chat_id, src_topic_id, src_msg_id)
                if tg_xkey not in telegram_crosspost_map:
                    telegram_crosspost_map[tg_xkey] = {}
                telegram_crosspost_map[tg_xkey][(dst_chat_id, dst_topic_id)] = sent.message_id
                print(f"[ОТЛАДКА] Кросспостинг сообщения Telegram {src_chat_id}/{src_topic_id}/{src_msg_id} -> {dst_chat_id}/{dst_topic_id}/{sent.message_id}")
            except Exception as e:
                print(f"[ОШИБКА] Не удалось кросспостить сообщение в Telegram {dst_chat_id}: {e}")

async def main():
    asyncio.create_task(discord_to_telegram_worker())
    asyncio.create_task(telegram_to_telegram_worker())
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
