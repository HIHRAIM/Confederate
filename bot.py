import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

CHANNEL_IDS = [ # Тут каналы, изменять только их:
    1365287139960422400,
    1365305667992420362,
    1365307471274708992,
    1365307503981887661
]

webhook_cache = {}


@bot.event
async def on_ready():
    print(f"Бот {bot.user} подключен!")
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} слэш-команд.")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")


@bot.event
async def on_message(message):
    if message.author.bot or isinstance(message.author, discord.Webhook) or message.channel.id not in CHANNEL_IDS:
        return

    for channel_id in CHANNEL_IDS:
        if channel_id != message.channel.id:
            target_channel = bot.get_channel(channel_id)
            if target_channel:
                if channel_id not in webhook_cache:
                    webhooks = await target_channel.webhooks()
                    webhook = None
                    for wh in webhooks:
                        if wh.user == bot.user:  # Ищем вебхук, созданный ботом
                            webhook = wh
                            break
                    if not webhook:
                        webhook = await target_channel.create_webhook(name="MessageForwarder")
                    webhook_cache[channel_id] = webhook

                webhook = webhook_cache[channel_id]
                await webhook.send(
                    content=message.content,
                    username=message.author.name,
                    avatar_url=message.author.avatar.url if message.author.avatar else None
                )


@bot.tree.command(name="clear_webhook_messages", description="Удалить сообщения, отправленные вебхуком бота от участника за последние N дней.")
@app_commands.describe(webhook_id="ID вебхука (строка)", days="Количество дней")
async def clear_webhook_messages(interaction: discord.Interaction, webhook_id: str, days: int):
    """
    Удаляет сообщения, отправленные вебхуком бота с указанным ID за последние N дней.
    """
    if interaction.channel.id not in CHANNEL_IDS:
        await interaction.response.send_message("Эта команда доступна только в определённых каналах.", ephemeral=True)
        return

    try:
        webhook_id = int(webhook_id)
    except ValueError:
        await interaction.response.send_message("ID вебхука должен быть числом.", ephemeral=True)
        return

    webhooks = await interaction.channel.webhooks()
    if not any(webhook.id == webhook_id for webhook in webhooks):
        await interaction.response.send_message("Вебхук с указанным ID не найден в этом канале.", ephemeral=True)
        return

    after_time = datetime.utcnow() - timedelta(days=days)

    deleted_messages = 0
    async for message in interaction.channel.history(after=after_time):
        if message.webhook_id == webhook_id:
            await message.delete()
            deleted_messages += 1

    await interaction.response.send_message(
        f"Удалено {deleted_messages} сообщений, отправленных вебхуком с ID {webhook_id} за последние {days} дней.",
        ephemeral=True
    )


bot.run("MTI5NTQ1NDgyOTg4MzI5ODAyMw.GG0o2Y.k-p_tBbu15vUYUHQPWMZRBU2cKzBR_ZDooQ4Fg")