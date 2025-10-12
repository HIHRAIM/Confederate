import asyncio
import discord
from discord.ext import commands
from telegram.ext import Application
from relaybot.config import DISCORD_TOKEN, TELEGRAM_TOKEN, RELAY_GROUPS, EXTRA_BRIDGES
from relaybot.queues import RelayQueues
from relaybot.discord_handlers import setup_discord_handlers
from relaybot.telegram_handlers import setup_telegram_handlers

async def discord_to_telegram_worker(queues, mappings):
    while True:
        (group_idx, discord_chan_id, discord_msg_id), body = await queues.discord_to_telegram.get()
        group = RELAY_GROUPS[group_idx]
        for target in group["telegram_targets"]:
            try:
                sent = await mappings["telegram_app"].bot.send_message(
                    chat_id=target["chat_id"],
                    text=body,
                    message_thread_id=target.get("topic_id")
                )
                key = (group_idx, target["chat_id"], target.get("topic_id"), discord_chan_id, discord_msg_id)
                mappings["discord_to_telegram"][key] = sent.message_id
                
                back_key = (group_idx, target["chat_id"], target.get("topic_id"), sent.message_id)
                if back_key not in mappings["telegram_to_discord_map"]:
                    mappings["telegram_to_discord_map"][back_key] = []
                mappings["telegram_to_discord_map"][back_key].append((discord_chan_id, discord_msg_id))
            except Exception as e:
                print(f"[Discord->TG Worker] {e}")

async def telegram_to_discord_worker(bot, queues, mappings):
    await bot.wait_until_ready()
    while True:
        (group_idx, telegram_chat_id, telegram_topic_id, telegram_msg_id), body = await queues.telegram_to_discord.get()
        group = RELAY_GROUPS[group_idx]
        for chan_id in group["discord_channels"]:
            try:
                channel = bot.get_channel(chan_id)
                if channel:
                    sent = await channel.send(body)
                    back_key = (group_idx, telegram_chat_id, telegram_topic_id, telegram_msg_id)
                    if back_key not in mappings["telegram_to_discord_map"]:
                        mappings["telegram_to_discord_map"][back_key] = []
                    mappings["telegram_to_discord_map"][back_key].append((chan_id, sent.id))
                    
                    # Обратное сопоставление для редактирования из Discord
                    d2t_key = (group_idx, telegram_chat_id, telegram_topic_id, chan_id, sent.id)
                    mappings["discord_to_telegram"][d2t_key] = telegram_msg_id
            except Exception as e:
                print(f"[TG->Discord Worker] {e}")

async def telegram_to_telegram_worker(queues, mappings):
    while True:
        group_idx, src_chat_id, src_topic_id, src_msg_id, body = await queues.telegram_to_telegram.get()
        group = RELAY_GROUPS[group_idx]
        for target in group["telegram_targets"]:
            dst_chat_id, dst_topic_id = target["chat_id"], target.get("topic_id")
            if dst_chat_id == src_chat_id and (dst_topic_id is None or dst_topic_id == src_topic_id):
                continue
            try:
                await mappings["telegram_app"].bot.send_message(
                    chat_id=dst_chat_id,
                    text=body,
                    message_thread_id=dst_topic_id
                )
            except Exception as e:
                print(f"[TG->TG Worker] {e}")

# --- Воркеры для мостов (EXTRA_BRIDGES) остаются без изменений ---
async def bridge_discord_to_telegram_worker(queues, mappings):
    while True:
        bridge_idx, discord_message, body = await queues.bridge_discord_to_telegram.get()
        bridge = EXTRA_BRIDGES[bridge_idx]
        try:
            sent = await mappings["telegram_app"].bot.send_message(
                chat_id=bridge["telegram_chat_id"],
                text=body,
                message_thread_id=bridge.get("telegram_topic_id")
            )
            mappings["bridge_discord_to_telegram"][(bridge_idx, discord_message.id)] = (bridge["telegram_chat_id"], bridge.get("telegram_topic_id"), sent.message_id)
            mappings["bridge_telegram_to_discord"][(bridge_idx, sent.message_id)] = (bridge["discord_channel_id"], discord_message.id)
        except Exception as e:
            print(f"[Bridge Discord->TG Worker] {e}")

async def bridge_telegram_to_discord_worker(queues, mappings):
    await mappings["discord_bot"].wait_until_ready()
    while True:
        bridge_idx, telegram_msg, body = await queues.bridge_telegram_to_discord.get()
        bridge = EXTRA_BRIDGES[bridge_idx]
        discord_channel = mappings["discord_bot"].get_channel(bridge["discord_channel_id"])
        if discord_channel:
            try:
                sent = await discord_channel.send(body)
                mappings["bridge_telegram_to_discord"][(bridge_idx, telegram_msg.message_id)] = (bridge["discord_channel_id"], sent.id)
                mappings["bridge_discord_to_telegram"][(bridge_idx, sent.id)] = (bridge["telegram_chat_id"], bridge.get("telegram_topic_id"), telegram_msg.message_id)
            except Exception as e:
                print(f"[Bridge TG->Discord Worker] {e}")

async def bridge_discord_edit_delete_worker(queues, mappings):
    while True:
        item = await queues.bridge_discord_edit_delete.get()
        bridge_idx, discord_msg = item["bridge_idx"], item["discord_msg"]
        mapping = mappings["bridge_discord_to_telegram"].get((bridge_idx, discord_msg.id))
        if not mapping: continue
        tg_chat_id, _, tg_msg_id = mapping
        try:
            if item["action"] == "edit":
                await mappings["telegram_app"].bot.edit_message_text(chat_id=tg_chat_id, message_id=tg_msg_id, text=item["body"])
            elif item["action"] == "delete":
                await mappings["telegram_app"].bot.delete_message(chat_id=tg_chat_id, message_id=tg_msg_id)
        except Exception as e:
            print(f"[Bridge Discord Edit/Delete] {e}")

async def bridge_telegram_edit_delete_worker(queues, mappings):
    await mappings["discord_bot"].wait_until_ready()
    while True:
        item = await queues.bridge_telegram_edit_delete.get()
        bridge_idx, telegram_msg = item["bridge_idx"], item["telegram_msg"]
        mapping = mappings["bridge_telegram_to_discord"].get((bridge_idx, telegram_msg.message_id))
        if not mapping: continue
        discord_channel_id, discord_msg_id = mapping
        discord_channel = mappings["discord_bot"].get_channel(discord_channel_id)
        if not discord_channel: continue
        try:
            discord_msg = await discord_channel.fetch_message(discord_msg_id)
            if item["action"] == "edit":
                await discord_msg.edit(content=item["body"])
            elif item["action"] == "delete":
                await discord_msg.delete()
        except Exception as e:
            print(f"[Bridge TG Edit/Delete] {e}")

async def main():
    queues = RelayQueues()
    mappings = {
        "discord_to_telegram": {},
        "telegram_to_discord_map": {},
        "discord_crosspost": {},
        "telegram_app": None, "discord_bot": None,
        "bridge_telegram_to_discord": {}, "bridge_discord_to_telegram": {},
    }
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True
    intents.messages = True

    discord_bot = commands.Bot(command_prefix="!", intents=intents)
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    mappings.update({"telegram_app": telegram_app, "discord_bot": discord_bot})

    setup_discord_handlers(discord_bot, queues, mappings)
    setup_telegram_handlers(telegram_app, queues, mappings)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.login(DISCORD_TOKEN)

    # Основные воркеры
    asyncio.create_task(discord_to_telegram_worker(queues, mappings))
    asyncio.create_task(telegram_to_telegram_worker(queues, mappings))
    asyncio.create_task(telegram_to_discord_worker(discord_bot, queues, mappings))

    # Воркеры для мостов
    queues.bridge_discord_to_telegram = asyncio.Queue()
    queues.bridge_telegram_to_discord = asyncio.Queue()
    queues.bridge_discord_edit_delete = asyncio.Queue()
    queues.bridge_telegram_edit_delete = asyncio.Queue()
    asyncio.create_task(bridge_discord_to_telegram_worker(queues, mappings))
    asyncio.create_task(bridge_telegram_to_discord_worker(queues, mappings))
    asyncio.create_task(bridge_discord_edit_delete_worker(queues, mappings))
    asyncio.create_task(bridge_telegram_edit_delete_worker(queues, mappings))

    await discord_bot.connect()

if __name__ == "__main__":
    asyncio.run(main())