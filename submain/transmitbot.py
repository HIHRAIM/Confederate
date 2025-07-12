import discord
from discord import app_commands, Interaction, Embed
from discord.ext import commands
import random
import datetime
import asyncio
import json
import os

# ---------- НАСТРОЙКИ ----------
TOKEN = 'MTM5MzU5ODk1ODkwNjkwNDU3Ng.GmeVoI.KPOLxYfEL7AalXSpZuRSEXUrfRVWmkw7RyXTlg'
VOTE_THREAD_ID = 1236406030250934303
ACCEPTED_THREAD_ID = 1393607029158842578
ORGANIZERS = [
    428596508271575040,
    986333707575119872
]
EMBED_COLOR = 0x3a66a3 # Цвет для embed сообщений

# ---------- ЛОКАЛИЗАЦИЯ ----------
LOCALES = {
    'ru': {
        'start_embed': 'Заготовка',
        'vote_sent': 'Ваш голос отправлен. Ключ: {key}',
        'vote_accepted': 'Ваш голос одобрен одним из организаторов!',
        'vote_denied': 'Один из организаторов отклонил ваш голос. Причина: {reason}',
        'not_supported': ':x: The language you selected is not supported.',
        'not_organizer': ':x: Только организаторы могут использовать эту команду.',
        'msg_not_found': ':x: Сообщение с таким ключом не найдено.',
        'reply_prefix': '',
    },
    'uk': {
        'start_embed': '',
        'vote_sent': 'Ваш голос отправлен. Ключ: {key}',
        'vote_accepted': '',
        'vote_denied': '',
        'not_supported': ':x: The language you selected is not supported.',
        'not_organizer': ':x: Only organizers can use this command.',
        'msg_not_found': ':x: Message with this key not found.',
        'reply_prefix': '',
    },
    'pl': {
        'start_embed': '',
        'vote_sent': 'Ваш голос отправлен. Ключ: {key}',
        'vote_accepted': '',
        'vote_denied': '',
        'not_supported': ':x: The language you selected is not supported.',
        'not_organizer': ':x: Only organizers can use this command.',
        'msg_not_found': ':x: Message with this key not found.',
        'reply_prefix': '',
    }
}
SUPPORTED_LANGS = ['ru', 'uk', 'pl']

# ---------- ПАМЯТЬ О СООБЩЕНИЯХ ----------
# Структура: ключ -> {user_id, username, user_avatar, text, lang, datetime}
VOTES_FILE = "votes_db.json"
votes_db = {}

def load_votes_db():
    global votes_db
    if os.path.exists(VOTES_FILE):
        with open(VOTES_FILE, "r", encoding="utf-8") as f:
            votes_db = json.load(f)
    else:
        votes_db = {}

def save_votes_db():
    with open(VOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(votes_db, f, ensure_ascii=False, indent=2)

async def process_vote_command_slash(interaction: Interaction, vote_type, text):
    votes_db[key] = {
    }
    save_votes_db()

# ---------- ХРАНЕНИЕ ВЫБОРА ЯЗЫКА ПОЛЬЗОВАТЕЛЯ ----------
user_lang = {}

# ---------- УТИЛИТЫ ----------
def gen_key():
    return str(random.randint(10**9, 10**10-1))

def get_locale(lang):
    return LOCALES.get(lang, LOCALES['ru'])

def now_fmt():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def is_dm(interaction: Interaction):
    return isinstance(interaction.channel, discord.DMChannel)

# ---------- БОТ И КОМАНДЫ ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    try:
        synced = await tree.sync()
        print(f"Commands synced: {len(synced)}")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# ---------- DM КОМАНДЫ ----------
@tree.command(name="start", description="Запустить бота и выбрать язык")
@app_commands.describe(lang="Выберите язык: ru, uk или pl")
async def start(interaction: Interaction, lang: str):
    if not is_dm(interaction):
        await interaction.response.send_message("Эта команда работает только в ЛС.", ephemeral=True)
        return
    if lang not in SUPPORTED_LANGS:
        await interaction.response.send_message(LOCALES['ru']['not_supported'], ephemeral=True)
        return
    user_lang[interaction.user.id] = lang
    emb = Embed(
        title="Межвикийная Олимпиада",
        description=LOCALES[lang]['start_embed'],
        color=EMBED_COLOR
    )
    await interaction.response.send_message(embed=emb)

@tree.command(name="vote_articles", description="Голос за статью")
@app_commands.describe(text="Текст вашего голоса")
async def vote_articles(interaction: Interaction, text: str):
    await process_vote_command_slash(interaction, "articles", text)

@tree.command(name="vote_designs", description="Голос за дизайн")
@app_commands.describe(text="Текст вашего голоса")
async def vote_designs(interaction: Interaction, text: str):
    await process_vote_command_slash(interaction, "designs", text)

@tree.command(name="support", description="Вопрос или поддержка")
@app_commands.describe(text="Текст вашего обращения")
async def support(interaction: Interaction, text: str):
    await process_vote_command_slash(interaction, "support", text)

async def process_vote_command_slash(interaction: Interaction, vote_type, text):
    if not is_dm(interaction):
        await interaction.response.send_message("Эта команда работает только в ЛС.", ephemeral=True)
        return
    lang = user_lang.get(interaction.user.id, 'ru')
    key = gen_key()
    votes_db[key] = {
        'user_id': interaction.user.id,
        'username': interaction.user.name,
        'user_avatar': str(interaction.user.display_avatar.url) if interaction.user.display_avatar else None,
        'text': text,
        'lang': lang,
        'datetime': now_fmt()
    }
    emb = Embed(
        title=f"Голосование: {vote_type.capitalize()}",
        description=text,
        color=EMBED_COLOR
    )
    emb.set_footer(
        text=f"{interaction.user} | ID: {interaction.user.id} | {votes_db[key]['datetime']} | Ключ: {key}",
        icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else discord.Embed.Empty
    )
    thread = bot.get_channel(VOTE_THREAD_ID)
    if thread:
        await thread.send(embed=emb)
    else:
        print("Ветка для голосов не найдена!")
    loc = get_locale(lang)
    await interaction.response.send_message(
        loc.get('vote_sent', f"Ваш голос отправлен. Ключ: {key}").format(key=key)
    )

# ---------- ОРГАНИЗАТОРСКИЕ КОМАНДЫ ----------
def is_organizer(user):
    return user.id in ORGANIZERS

async def find_vote_by_key(key):
    return votes_db.get(key)

@tree.command(name="accepted", description="Организатор: одобрить голос по ключу")
@app_commands.describe(key="10-значный ключ сообщения")
async def accepted(interaction: Interaction, key: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'], ephemeral=True)
        return
    if not key or key not in votes_db:
        await interaction.response.send_message(LOCALES['ru']['msg_not_found'], ephemeral=True)
        return
    vote = votes_db[key]
    emb = Embed(
        title=f"Голос одобрен",
        description=vote['text'],
        color=EMBED_COLOR
    )
    emb.set_footer(
        text=f"{vote['username']} | ID: {vote['user_id']}",
        icon_url=vote['user_avatar'] if vote['user_avatar'] else discord.Embed.Empty
    )
    thread = bot.get_channel(ACCEPTED_THREAD_ID)
    if thread:
        await thread.send(embed=emb)
    user = await bot.fetch_user(vote['user_id'])
    loc = get_locale(vote['lang'])
    emb2 = Embed(
        title="",
        description=loc['vote_accepted'],
        color=EMBED_COLOR
    )
    try:
        await user.send(embed=emb2)
    except Exception:
        pass
    await interaction.response.send_message(":white_check_mark: Готово.", ephemeral=True)

@tree.command(name="denied", description="Организатор: отклонить голос по ключу с причиной")
@app_commands.describe(key="10-значный ключ сообщения", reason="Причина отказа")
async def denied(interaction: Interaction, key: str, reason: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'], ephemeral=True)
        return
    if not key or key not in votes_db:
        await interaction.response.send_message(LOCALES['ru']['msg_not_found'], ephemeral=True)
        return
    vote = votes_db[key]
    user = await bot.fetch_user(vote['user_id'])
    loc = get_locale(vote['lang'])
    emb = Embed(
        title="",
        description=loc['vote_denied'].format(reason=reason),
        color=EMBED_COLOR
    )
    dm_sent = True
    try:
        await user.send(embed=emb)
    except Exception:
        dm_sent = False
    if not dm_sent:
        await interaction.response.send_message(
            f":warning: Не удалось отправить сообщение пользователю в ЛС.", ephemeral=True)
    else:
        await interaction.response.send_message(":white_check_mark: Готово.", ephemeral=True)

@tree.command(name="reply", description="Организатор: ответить пользователю по ключу")
@app_commands.describe(key="10-значный ключ сообщения", text="Текст ответа")
async def reply(interaction: Interaction, key: str, text: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'], ephemeral=True)
        return
    if not key or key not in votes_db:
        await interaction.response.send_message(LOCALES['ru']['msg_not_found'], ephemeral=True)
        return
    vote = votes_db[key]
    user = await bot.fetch_user(vote['user_id'])
    emb = Embed(
        title="Ответ организатора",
        description=text,
        color=EMBED_COLOR
    )
    try:
        await user.send(embed=emb)
    except Exception:
        pass
    await interaction.response.send_message(":white_check_mark: Готово.", ephemeral=True)

# ---------- ЗАПУСК ----------
if __name__ == '__main__':
    load_votes_db()
    bot.run(TOKEN)
