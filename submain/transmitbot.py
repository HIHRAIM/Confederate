import discord
from discord import app_commands, Interaction, Embed
from discord.ext import commands
import random
import datetime
import asyncio
import json
import os
import shutil

# ---------- НАСТРОЙКИ ----------
TOKEN = 'MTM5MzU5ODk1ODkwNjkwNDU3Ng.GmeVoI.KPOLxYfEL7AalXSpZuRSEXUrfRVWmkw7RyXTlg'
VOTE_THREAD_ID = 1236406030250934303
ACCEPTED_THREAD_ID = 1393607029158842578
ORGANIZERS = [
    428596508271575040,
    986333707575119872
]

EMBED_COLOR = 0x3a66a3

# ---------- ЛОКАЛИЗАЦИЯ ----------
LOCALES = {
    'ru': {
        'start_embed': 'Заготовка',
        'vote_sent': 'Ваш голос отправлен. Ключ: {key}',
        'vote_accepted': 'Ваш голос одобрен одним из организаторов!',
        'vote_denied': 'Один из организаторов отклонил ваш голос. Причина: {reason}',
        'not_supported': ':x: Выбранный вами язык не поддерживается.',
        'not_organizer': ':x: Только организаторы могут использовать эту команду.',
        'msg_not_found': ':x: Сообщение с таким ключом не найдено.',
        'reply_prefix': '',
        'banned': ':x: Вы были заблокированы и не можете пользоваться ботом.',
        'ban_success': ':white_check_mark: Пользователь заблокирован.',
        'unban_success': ':white_check_mark: Пользователь разблокирован.',
        'already_banned': ':x: Пользователь уже заблокирован.',
        'not_banned': ':x: Пользователь не был заблокирован.',
    },
    'uk': {
        'start_embed': 'Ви вибрали українську мову. Будь ласка, надішліть свій голос.',
        'vote_sent': 'Ваш голос надіслано. Ключ: {key}',
        'vote_accepted': 'Ваш голос схвалено одним з організаторів!',
        'vote_denied': 'Один з організаторів відхилив ваш голос. Причина: {reason}',
        'not_supported': ':x: Обрана вами мова не підтримується.',
        'not_organizer': ':x: Тільки організатори можуть використовувати цю команду.',
        'msg_not_found': ':x: Повідомлення з таким ключем не знайдено.',
        'reply_prefix': '',
        'banned': ':x: Ви були заблоковані і не можете користуватися ботом.',
        'ban_success': ':white_check_mark: Користувача заблоковано.',
        'unban_success': ':white_check_mark: Користувача розблоковано.',
        'already_banned': ':x: Користувач вже заблокований.',
        'not_banned': ':x: Користувач не був заблокований.',
    },
    'pl': {
        'start_embed': 'Wybrałeś język polski. Proszę, wyślij swoją opinię.',
        'vote_sent': 'Twoja opinia została wysłana. Klucz: {key}',
        'vote_accepted': 'Twoja opinia została zaakceptowana przez jednego z organizatorów!',
        'vote_denied': 'Jeden z organizatorów odrzucił twoją opinię. Powód: {reason}',
        'not_supported': ':x: Wybrany przez Ciebie język nie jest obsługiwany.',
        'not_organizer': ':x: Tylko organizatorzy mogą używać tej komendy.',
        'msg_not_found': ':x: Nie znaleziono wiadomości o podanym kluczu.',
        'reply_prefix': '',
        'banned': ':x: Zostałeś zablokowany i nie możesz korzystać z bota.',
        'ban_success': ':white_check_mark: Użytkownik zablokowany.',
        'unban_success': ':white_check_mark: Użytkownik odblokowany.',
        'already_banned': ':x: Użytkownik jest już zablokowany.',
        'not_banned': ':x: Użytkownik nie był zablokowany.',
    },
    'en': {
        'start_embed': 'You have selected English. Please send your vote.',
        'vote_sent': 'Your vote has been sent. Key: {key}',
        'vote_accepted': 'Your vote has been accepted by one of the organizers!',
        'vote_denied': 'One of the organizers has denied your vote. Reason: {reason}',
        'not_supported': ':x: The language you selected is not supported.',
        'not_organizer': ':x: Only organizers can use this command.',
        'msg_not_found': ':x: Message with the specified key not found.',
        'reply_prefix': '',
        'banned': ':x: You have been banned and cannot use the bot.',
        'ban_success': ':white_check_mark: User has been banned.',
        'unban_success': ':white_check_mark: User has been unbanned.',
        'already_banned': ':x: User is already banned.',
        'not_banned': ':x: User was not banned.',
    }
}
SUPPORTED_LANGS = ['ru', 'uk', 'pl', 'en']

# ---------- ПАМЯТЬ О СООБЩЕНИЯХ ----------
VOTES_FILE = "votes_db.json"
BACKUP_FILE = "votes_db_backup.json"
BANNED_FILE = "banned_users.json"
votes_db = {}
banned_users = set()

def load_votes_db():
    global votes_db
    try:
        if os.path.exists(VOTES_FILE):
            with open(VOTES_FILE, "r", encoding="utf-8") as f:
                votes_db = json.load(f)
        else:
            votes_db = {}
    except Exception as e:
        print(f"Ошибка при загрузке базы голосов: {e}")
        votes_db = {}

def save_votes_db():
    try:
        if os.path.exists(VOTES_FILE):
            shutil.copyfile(VOTES_FILE, BACKUP_FILE)
        with open(VOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(votes_db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при сохранении базы голосов: {e}")

def load_banned_users():
    global banned_users
    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r", encoding="utf-8") as f:
                banned_users = set(json.load(f))
        except Exception as e:
            print(f"Ошибка при загрузке списка банов: {e}")
            banned_users = set()
    else:
        banned_users = set()

def save_banned_users():
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(banned_users), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при сохранении списка банов: {e}")

async def periodic_save_votes(interval=300):
    while True:
        save_votes_db()
        save_banned_users()
        await asyncio.sleep(interval)

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

def get_user_lang(user_id):
    return user_lang.get(user_id, 'ru')

def is_banned(user_id):
    return user_id in banned_users

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
    bot.loop.create_task(periodic_save_votes())

# ---------- КОМАНДЫ В ЛС ----------
@tree.command(name="start", description="Start the bot and select the language")
@app_commands.describe(lang="Select your language (ru, uk, pl, en)")
async def start(interaction: Interaction, lang: str):
    if is_banned(interaction.user.id):
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
    if not is_dm(interaction):
        await interaction.response.send_message("Эта команда работает только в ЛС.", ephemeral=True)
        return
    if lang not in SUPPORTED_LANGS:
        await interaction.response.send_message(LOCALES['ru']['not_supported'], ephemeral=True)
        return
    user_lang[interaction.user.id] = lang
    emb = Embed(
        title="Supraconfedetative Wiki Olympiad 2025",
        description=LOCALES[lang]['start_embed'],
        color=EMBED_COLOR
    )
    await interaction.response.send_message(embed=emb)

@tree.command(name="vote_articles", description="A vote for the wiki in the articles competition")
@app_commands.describe(text="Текст вашего голоса")
async def vote_articles(interaction: Interaction, text: str):
    if is_banned(interaction.user.id):
        lang = get_user_lang(interaction.user.id)
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
    await process_vote_command_slash(interaction, "articles", text)

@tree.command(name="vote_designs", description="A vote for the wiki in the design competition")
@app_commands.describe(text="Текст вашего голоса")
async def vote_designs(interaction: Interaction, text: str):
    if is_banned(interaction.user.id):
        lang = get_user_lang(interaction.user.id)
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
    await process_vote_command_slash(interaction, "designs", text)

@tree.command(name="support", description="Appeal to the organizers")
@app_commands.describe(text="Текст вашего обращения")
async def support(interaction: Interaction, text: str):
    if is_banned(interaction.user.id):
        lang = get_user_lang(interaction.user.id)
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
    await process_vote_command_slash(interaction, "support", text)

async def process_vote_command_slash(interaction: Interaction, vote_type, text):
    if is_banned(interaction.user.id):
        lang = get_user_lang(interaction.user.id)
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
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
    save_votes_db()
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

@tree.command(name="ban", description="Organizer: ban a user by ID")
@app_commands.describe(user_id="ID пользователя для бана")
async def ban(interaction: Interaction, user_id: int):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'], ephemeral=True)
        return
    if user_id in banned_users:
        loc = get_locale(get_user_lang(interaction.user.id))
        await interaction.response.send_message(loc['already_banned'], ephemeral=True)
        return
    banned_users.add(user_id)
    save_banned_users()
    loc = get_locale(get_user_lang(interaction.user.id))
    await interaction.response.send_message(loc['ban_success'], ephemeral=True)

@tree.command(name="unban", description="Organizer: unban a user by ID")
@app_commands.describe(user_id="ID пользователя для разбана")
async def unban(interaction: Interaction, user_id: int):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'], ephemeral=True)
        return
    if user_id not in banned_users:
        loc = get_locale(get_user_lang(interaction.user.id))
        await interaction.response.send_message(loc['not_banned'], ephemeral=True)
        return
    banned_users.remove(user_id)
    save_banned_users()
    loc = get_locale(get_user_lang(interaction.user.id))
    await interaction.response.send_message(loc['unban_success'], ephemeral=True)

@tree.command(name="accepted", description="Organizer: approve a vote by key")
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
        title="Ваш голос принят!",
        description=f"{loc.get('vote_accepted', 'Ваш голос одобрен одним из организаторов!')}",
        color=EMBED_COLOR
    )
    try:
        await user.send(embed=emb2)
    except Exception:
        pass
    del votes_db[key]
    save_votes_db()
    await interaction.response.send_message(
        f"The vote from user **{vote['username']}** with key `{key}` has been ACCEPTED, and a notification has been sent to the participant.", ephemeral=True
    )

@tree.command(name="denied", description="Organizer: deny a vote by key")
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
        title="Ваш голос отклонён",
        description=loc['vote_denied'].format(reason=reason) + f"",
        color=EMBED_COLOR
    )
    dm_sent = True
    try:
        await user.send(embed=emb)
    except Exception:
        dm_sent = False
    del votes_db[key]
    save_votes_db()
    if not dm_sent:
        await interaction.response.send_message(
            f":warning: Failed to send a direct message to the user, but the vote with key `{key}` has been REJECTED.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"The vote from user **{vote['username']}** with key `{key}` has been REJECTED. Reason: {reason}. A notification has been sent to the participant.", ephemeral=True
        )

@tree.command(name="reply", description="Organizer: reply to a user by key")
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
        description=f"{text}\n\nВаш исходный голос: {vote['text']}\nКлюч: {key}",
        color=EMBED_COLOR
    )
    try:
        await user.send(embed=emb)
    except Exception:
        pass
    del votes_db[key]
    save_votes_db()
    await interaction.response.send_message(
        f"The response to the request from user **{vote['username']}** with key `{key}` has been SENT.", ephemeral=True
    )

# ---------- ЗАПУСК ----------
if __name__ == '__main__':
    load_votes_db()
    load_banned_users()
    bot.run(TOKEN)
