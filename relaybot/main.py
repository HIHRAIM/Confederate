import asyncio
import discord
from discord.ext import commands
from telegram.ext import Application
from relaybot.config import DISCORD_TOKEN, TELEGRAM_TOKEN, DISCORD_CHANNEL_IDS, TELEGRAM_TARGETS, EXTRA_BRIDGES
from relaybot.queues import RelayQueues
from relaybot.discord_handlers import setup_discord_handlers
from relaybot.telegram_handlers import setup_telegram_handlers

async def discord_to_telegram_worker(queues, mappings):
    while True:
        (discord_chan_id, discord_msg_id), body = await queues.discord_to_telegram.get()
        for target in TELEGRAM_TARGETS:
            try:
                if target["topic_id"] is not None:
                    sent = await mappings["telegram_app"].bot.send_message(
                        chat_id=target["chat_id"],
                        text=body,
                        message_thread_id=target["topic_id"]
                    )
                else:
                    sent = await mappings["telegram_app"].bot.send_message(
                        chat_id=target["chat_id"],
                        text=body
                    )
                key = (target["chat_id"], target.get("topic_id"), discord_chan_id, discord_msg_id)
                mappings["discord_to_telegram"][key] = sent.message_id
                back_key = (target["chat_id"], target.get("topic_id"), sent.message_id)
                if back_key not in mappings["telegram_to_discord_map"]:
                    mappings["telegram_to_discord_map"][back_key] = []
                mappings["telegram_to_discord_map"][back_key].append((discord_chan_id, discord_msg_id))
            except Exception as e:
                print(f"[Discord->TG Worker] {e}")

async def telegram_to_discord_worker(bot, queues, mappings):
    await bot.wait_until_ready()
    while True:
        (telegram_chat_id, telegram_topic_id, telegram_msg_id), body = await queues.telegram_to_discord.get()
        for chan_id in DISCORD_CHANNEL_IDS:
            try:
                channel = bot.get_channel(chan_id)
                if channel:
                    sent = await channel.send(body)
                    back_key = (telegram_chat_id, telegram_topic_id, telegram_msg_id)
                    if back_key not in mappings["telegram_to_discord_map"]:
                        mappings["telegram_to_discord_map"][back_key] = []
                    mappings["telegram_to_discord_map"][back_key].append((chan_id, sent.id))
                    mappings["discord_to_telegram"][(telegram_chat_id, telegram_topic_id, chan_id, sent.id)] = telegram_msg_id
            except Exception as e:
                print(f"[TG->Discord Worker] {e}")

async def telegram_to_telegram_worker(queues, mappings):
    while True:
        src_chat_id, src_topic_id, src_msg_id, body = await queues.telegram_to_telegram.get()
        for target in TELEGRAM_TARGETS:
            dst_chat_id, dst_topic_id = target["chat_id"], target.get("topic_id")
            if dst_chat_id == src_chat_id and ((dst_topic_id or None) == (src_topic_id or None)):
                continue
            try:
                if dst_topic_id is not None:
                    await mappings["telegram_app"].bot.send_message(
                        chat_id=dst_chat_id,
                        text=body,
                        message_thread_id=dst_topic_id
                    )
                else:
                    await mappings["telegram_app"].bot.send_message(
                        chat_id=dst_chat_id,
                        text=body
                    )
            except Exception as e:
                print(f"[TG->TG Worker] {e}")

# --------- ADDITIVE WORKERS FOR BRIDGES ---------

async def bridge_discord_to_telegram_worker(queues, mappings):
    # Waits for bridge messages to send from Discord to Telegram, and stores mapping for edits/deletes.
    while True:
        bridge_idx, discord_message, body = await queues.bridge_discord_to_telegram.get()
        bridge = EXTRA_BRIDGES[bridge_idx]
        try:
            sent = await mappings["telegram_app"].bot.send_message(
                chat_id=bridge["telegram_chat_id"],
                text=body,
                message_thread_id=bridge.get("telegram_topic_id")
            )
            # Store mapping for later edits/deletes
            mappings["bridge_discord_to_telegram"][(bridge_idx, discord_message.id)] = (bridge["telegram_chat_id"], bridge.get("telegram_topic_id"), sent.message_id)
            mappings["bridge_telegram_to_discord"][(bridge_idx, sent.message_id)] = (bridge["discord_channel_id"], discord_message.id)
        except Exception as e:
            print(f"[Bridge Discord->TG Worker] {e}")

async def bridge_telegram_to_discord_worker(queues, mappings):
    # Waits for bridge messages to send from Telegram to Discord, and stores mapping for edits/deletes.
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

# --------- WORKERS FOR EDITS/DELETES ON BRIDGES ---------

async def bridge_discord_edit_delete_worker(queues, mappings):
    while True:
        item = await queues.bridge_discord_edit_delete.get()
        if item["action"] == "edit":
            bridge_idx = item["bridge_idx"]
            discord_msg = item["discord_msg"]
            body = item["body"]
            mapping = mappings["bridge_discord_to_telegram"].get((bridge_idx, discord_msg.id))
            if mapping:
                tg_chat_id, tg_topic_id, tg_msg_id = mapping
                try:
                    await mappings["telegram_app"].bot.edit_message_text(
                        chat_id=tg_chat_id,
                        message_id=tg_msg_id,
                        text=body,
                        # message_thread_id=tg_topic_id,  # REMOVE THIS LINE!
                    )
                except Exception as e:
                    print(f"[Bridge Discord->TG Edit] {e}")
        elif item["action"] == "delete":
            bridge_idx = item["bridge_idx"]
            discord_msg = item["discord_msg"]
            mapping = mappings["bridge_discord_to_telegram"].get((bridge_idx, discord_msg.id))
            if mapping:
                tg_chat_id, tg_topic_id, tg_msg_id = mapping
                try:
                    await mappings["telegram_app"].bot.delete_message(
                        chat_id=tg_chat_id,
                        message_id=tg_msg_id
                    )
                except Exception as e:
                    print(f"[Bridge Discord->TG Delete] {e}")

async def bridge_telegram_edit_delete_worker(queues, mappings):
    # This should be called by your Telegram handler via a queue when an edit or delete occurs in a bridge telegram chat.
    await mappings["discord_bot"].wait_until_ready()
    while True:
        item = await queues.bridge_telegram_edit_delete.get()
        if item["action"] == "edit":
            bridge_idx = item["bridge_idx"]
            telegram_msg = item["telegram_msg"]
            body = item["body"]
            mapping = mappings["bridge_telegram_to_discord"].get((bridge_idx, telegram_msg.message_id))
            if mapping:
                discord_channel_id, discord_msg_id = mapping
                discord_channel = mappings["discord_bot"].get_channel(discord_channel_id)
                if discord_channel:
                    try:
                        discord_msg = await discord_channel.fetch_message(discord_msg_id)
                        await discord_msg.edit(content=body)
                    except Exception as e:
                        print(f"[Bridge TG->Discord Edit] {e}")
        elif item["action"] == "delete":
            bridge_idx = item["bridge_idx"]
            telegram_msg = item["telegram_msg"]
            mapping = mappings["bridge_telegram_to_discord"].get((bridge_idx, telegram_msg.message_id))
            if mapping:
                discord_channel_id, discord_msg_id = mapping
                discord_channel = mappings["discord_bot"].get_channel(discord_channel_id)
                if discord_channel:
                    try:
                        discord_msg = await discord_channel.fetch_message(discord_msg_id)
                        await discord_msg.delete()
                    except Exception as e:
                        print(f"[Bridge TG->Discord Delete] {e}")

# --------- MAIN ---------

async def main():
    queues = RelayQueues()
    mappings = {
        "discord_to_telegram": {},
        "telegram_to_discord_map": {},
        "discord_crosspost": {},
        "telegram_app": None,
        "discord_bot": None,
        "TELEGRAM_TARGETS": TELEGRAM_TARGETS,
        # Bridge mappings:
        "bridge_telegram_to_discord": {},
        "bridge_discord_to_telegram": {},
    }
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True
    intents.messages = True

    discord_bot = commands.Bot(command_prefix="!", intents=intents)
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    mappings["telegram_app"] = telegram_app
    mappings["discord_bot"] = discord_bot

    setup_discord_handlers(discord_bot, queues, mappings)
    setup_telegram_handlers(telegram_app, queues, mappings)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.login(DISCORD_TOKEN)

    asyncio.create_task(discord_to_telegram_worker(queues, mappings))
    asyncio.create_task(telegram_to_telegram_worker(queues, mappings))
    asyncio.create_task(telegram_to_discord_worker(discord_bot, queues, mappings))

    # Bridge workers
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