import discord
from discord import app_commands, Interaction, Embed
from discord.ext import commands
import random
import datetime
import asyncio
import json
import os
import shutil

# == Настройки ==
TOKEN = 'MTM5MzU5ODk1ODkwNjkwNDU3Ng.GmeVoI.KPOLxYfEL7AalXSpZuRSEXUrfRVWmkw7RyXTlg'

ORGANIZERS = [
    428596508271575040, # hihraim
    986333707575119872, # fersteaxpasique4
    199998796741083137, # taxcymus
    902982388886429697, # no.more.exe
    439829989274157057, # kyomist
    606330360606883860  # alemaster01
]

DESIGN_CHANNELS = {
    'ru': 1394301596984279140,
    'uk': 1394301596984279140,
    'pl': 1394301596984279140,
    'es': 1394445357206863993,
}

DESIGN_ACCEPTED_CHANNEL = {
    'ru': 1394778605816516638,
    'uk': 1394778605816516638,
    'pl': 1394778605816516638,
    'es': 1394603841852280893
}

ARTICLE_CHANNELS = {
    'ru': 1394982783637655632,
    'es': 1394445478531436695,
}

ARTICLE_ACCEPTED_CHANNEL = {
    'ru': 1394778705993269248,
    'es': 1394603889050779688
}

EMBED_COLOR = 0x3a66a3

# == Локализация ==
LOCALES = {
    'ru': {
        'start_embed': 'Вы выбрали русский язык. Пожалуйста, проголосуйте, используя команду `/vote_articles` или `/vote_designs` и следуя инструкции в [блоге](https://confederation.fandom.com/ru/wiki/Блог_участника:HIHRAIM/Супраконфедеративная_Викиолимпиада-2025/Голосование).',
        'vote_sent': 'Ваш голос отправлен. Ключ: {key}',
        'vote_accepted_title': 'Ваш голос принят!',
        'vote_rejected_title': 'Ваш голос отклонён!',
        'vote_accepted': 'Ваш голос одобрен одним из организаторов!',
        'vote_denied': 'Один из организаторов отклонил ваш голос. Причина: {reason}',
        'not_supported': ':x: Выбранный вами язык не поддерживается.',
        'not_organizer': ':x: Только организаторы могут использовать эту команду.',
        'msg_not_found': ':x: Сообщение с таким ключом не найдено.',
        'banned': ':x: Вы были заблокированы и не можете пользоваться ботом.',
        'ban_success': ':white_check_mark: Пользователь заблокирован.',
        'unban_success': ':white_check_mark: Пользователь разблокирован.',
        'already_banned': ':x: Пользователь уже заблокирован.',
        'not_banned': ':x: Пользователь не был заблокирован.',
    },
    'uk': {
        'start_embed': 'Ви обрали українську мову. Будь ласка, проголосуйте за допомогою команди `/vote_designs`.',
        'vote_sent': 'Ваш голос надіслано. Ключ: {key}',
        'no_article_competition': 'У цьому конкурсі не беруть участь проєкти обраною мовою.',
        'vote_accepted_title': 'Ваш голос прийнято!',
        'vote_rejected_title': 'Ваш голос відхилено!',
        'vote_accepted': 'Ваш голос було схвалено одним з організаторів!',
        'vote_denied': 'Один із організаторів відхилив ваш голос. Причина: {reason}',
        'not_supported': ':x: Обрана вами мова не підтримується.',
        'not_organizer': ':x: Тільки організатори можуть використовувати цю команду.',
        'msg_not_found': ':x: Повідомлення з таким ключем не знайдено.',
        'banned': ':x: Вас заблоковано, і ви не можете користуватися ботом.',
        'ban_success': ':white_check_mark: Користувача заблоковано.',
        'unban_success': ':white_check_mark: Користувача розблоковано.',
        'already_banned': ':x: Користувача вже заблоковано.',
        'not_banned': ':x: Користувач не був заблокований.',
    },
    'pl': {
        'start_embed': 'Wybrano język polski. Proszę oddać swoje głosy (`/vote_designs`).', # Требуется проверка
        'vote_sent': 'Twój głos został oddany. Klucz: {key}',
        'no_article_competition': 'W tym konkursie nie biorą udziału projekty w wybranym języku.',
        'vote_accepted_title': 'Twój głos został zaakceptowany!', # Требуется проверка
        'vote_rejected_title': 'Twój głos został odrzucony!', # Требуется проверка
        'vote_accepted': 'Twój głos został odebrany i zaakceptowany przez jednego z organizatorów!',
        'vote_denied': 'Jeden z organizatorów odrzucił twój głos. Powód: {reason}',
        'not_supported': ':x: Wybrany przez Ciebie język nie jest obsługiwany.',
        'not_organizer': ':x: Tylko organizatorzy mogą używać tej komendy.',
        'msg_not_found': ':x: Nie znaleziono wiadomości o podanym kluczu.',
        'banned': ':x: Zostałeś/aś zablokowany/a i nie możesz korzystać z bota.',
        'ban_success': ':white_check_mark: Zablokowano użytkownika.',
        'unban_success': ':white_check_mark: Odblokowano użytkownika.',
        'already_banned': ':x: Użytkownik jest już zablokowany.',
        'not_banned': ':x: Użytkownik nie był zablokowany.',
    },
    'es': {
        'start_embed': 'Has seleccionado español. Vota usando el comando /vote_articles o /vote_designs y siguiendo las instrucciones ~~del blog~~.',
        'vote_sent': 'Tu voto ha sido enviado. Key: {key}',
        'vote_accepted_title': '¡Tu voto fue aceptado!',
        'vote_rejected_title': 'Una disculpa, tu voto fue rechazado.',
        'vote_accepted': '¡Tu voto fue aprobado por uno de nuestros organizadores!',
        'vote_denied': 'Uno de nuestros organizados rechazó tu voto.  Motivo: {reason}',
        'not_supported': ':x: El idioma seleccionado no está disponible.',
        'not_organizer': ':x: Solo los organizadores pueden usar este comando.',
        'msg_not_found': ':x: No se encontró ningún mensaje con la clave dada.',
        'banned': ':x: Has sido bloqueado y no puedes usar el bot.',
        'ban_success': ':white_check_mark: El usuario fue bloqueado.',
        'unban_success': ':white_check_mark: El usuario fue desbloqueado.',
        'already_banned': ':x: El usuario ya está actualmente bloqueado.',
        'not_banned': ':x: El usuario no ha sido bloqueado previamente.',
    }
}

SUPPORTED_LANGS = ['ru', 'uk', 'pl', 'es']

# == Память и файлы ==
votes_db = {}
VOTES_FILE = "votes_db.json"
BACKUP_FILE = "votes_db_backup.json"
banned_users = set()
BANNED_FILE = "banned_users.json"
user_message_count = {}
USER_MESSAGE_COUNT_FILE = "user_message_count.json"
user_lang = {}
USER_LANG_FILE = "user_lang.json"
user_fandom_nick = {}
USER_FANDOM_NICK_FILE = "user_fandom_nick.json"

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

def save_user_lang():
    try:
        with open(USER_LANG_FILE, "w", encoding="utf-8") as f:
            json.dump(user_lang, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_user_lang():
    global user_lang
    if os.path.exists(USER_LANG_FILE):
        try:
            with open(USER_LANG_FILE, "r", encoding="utf-8") as f:
                user_lang = json.load(f)
        except Exception:
            user_lang = {}
    else:
        user_lang = {}

def save_user_fandom_nick():
    try:
        with open(USER_FANDOM_NICK_FILE, "w", encoding="utf-8") as f:
            json.dump(user_fandom_nick, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_user_fandom_nick():
    global user_fandom_nick
    if os.path.exists(USER_FANDOM_NICK_FILE):
        try:
            with open(USER_FANDOM_NICK_FILE, "r", encoding="utf-8") as f:
                user_fandom_nick = json.load(f)
        except Exception:
            user_fandom_nick = {}
    else:
        user_fandom_nick = {}

# == Утилиты ==
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
    return str(user_id) in banned_users

# == Инициализация бота ==
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

async def update_presence():
    while True:
        guild_count = len(bot.guilds)
        member_ids = set()
        for guild in bot.guilds:
            try:
                member_ids.update(member.id for member in guild.members)
            except Exception:
                pass
        member_count = len(member_ids)
        status_text = f"Servers: {guild_count} | Users: {member_count}"
        await bot.change_presence(activity=discord.Game(name=status_text))
        await asyncio.sleep(300)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    try:
        synced = await tree.sync()
        print(f"Commands synced: {len(synced)}")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    bot.loop.create_task(periodic_save_votes())
    bot.loop.create_task(update_presence())

# === Команды бота ===
@tree.command(name="start", description="Start the bot and select the language")
@app_commands.describe(lang="Select language (ru, uk, pl, es)", fandom_nick="Your username on Fandom")
async def start(interaction: Interaction, lang: str, fandom_nick: str):
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
    user_fandom_nick[interaction.user.id] = fandom_nick
    save_user_lang()
    save_user_fandom_nick()
    emb = Embed(
        title="Supraconfedetative Wiki Olympiad 2025",
        description=LOCALES[lang]['start_embed'],
        color=EMBED_COLOR
    )
    await interaction.response.send_message(embed=emb)

@tree.command(name="vote_articles", description="A vote for the wiki in the articles competition")
@app_commands.describe(text="The text of your vote")
async def vote_articles(interaction: Interaction, text: str):
    if interaction.user.id not in user_lang or interaction.user.id not in user_fandom_nick:
        await interaction.response.send_message(
            "First, use the `/start` command and specify your language and nickname on Fandom.",
            ephemeral=True
        )
        return
    lang = get_user_lang(interaction.user.id)
    if is_banned(interaction.user.id):
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'])
        return
    if lang in ['uk', 'pl']:
        loc = get_locale(lang)
        await interaction.response.send_message(loc['no_article_competition'])
        return
    channel_id = ARTICLE_CHANNELS.get(lang)
    accepted_channel_id = ARTICLE_ACCEPTED_CHANNEL.get(lang)
    await process_vote_command_slash(interaction, "articles", text, channel_id, accepted_channel_id)

@tree.command(name="vote_designs", description="A vote for the wiki in the design competition")
@app_commands.describe(text="The text of your vote")
async def vote_designs(interaction: Interaction, text: str):
    if interaction.user.id not in user_lang or interaction.user.id not in user_fandom_nick:
        await interaction.response.send_message(
            "First, use the `/start` command and specify your language and nickname on Fandom.",
            ephemeral=True
        )
        return
    lang = get_user_lang(interaction.user.id)
    if is_banned(interaction.user.id):
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'])
        return
    channel_id = DESIGN_CHANNELS.get(lang)
    accepted_channel_id = DESIGN_ACCEPTED_CHANNEL.get(lang)
    await process_vote_command_slash(interaction, "designs", text, channel_id, accepted_channel_id)

@tree.command(name="support", description="Appeal to the organizers")
@app_commands.describe(text="The text of your appeal to the organizers")
async def support(interaction: Interaction, text: str):
    if interaction.user.id not in user_lang or interaction.user.id not in user_fandom_nick:
        await interaction.response.send_message(
            "First, use the `/start` command and specify your language and nickname on Fandom.",
            ephemeral=True
        )
        return
    lang = get_user_lang(interaction.user.id)
    if is_banned(interaction.user.id):
        loc = get_locale(lang)
        await interaction.response.send_message(loc['banned'], ephemeral=True)
        return
    if lang in ['ru', 'uk', 'pl']:
        channel_id = 1394300619308925009
    elif lang == 'es':
        channel_id = 1394445015996174356
    else:
        channel_id = 1394300619308925009
    await process_vote_command_slash(interaction, "support", text, channel_id, None)

async def process_vote_command_slash(interaction: Interaction, vote_type, text, channel_id, accepted_channel_id):
    if is_banned(interaction.user.id):
        return
    if not is_dm(interaction):
        return
    lang = user_lang.get(interaction.user.id, 'ru')
    fandom_nick = user_fandom_nick.get(interaction.user.id, interaction.user.name)
    user_id_str = str(interaction.user.id)
    count = user_message_count.get(user_id_str, 0) + 1
    user_message_count[user_id_str] = count
    save_user_message_count()
    key = gen_key()
    votes_db[key] = {
        'user_id': interaction.user.id,
        'username': interaction.user.name,
        'user_avatar': str(interaction.user.display_avatar.url) if interaction.user.display_avatar else None,
        'text': text,
        'lang': lang,
        'user_message_number': count,
        'accepted_channel_id': accepted_channel_id,
        'fandom_nick': fandom_nick
    }
    save_votes_db()
    if lang in ['ru', 'uk', 'pl']:
        fandom_url = f"https://confederation.fandom.com/ru/wiki/User:{fandom_nick}"
    elif lang == 'es':
        fandom_url = f"https://confederacion-hispana.fandom.com/es/wiki/User:{fandom_nick}"
    else:
        fandom_url = fandom_nick
    footer_text = f"{fandom_nick} | ID: {interaction.user.id} | Message №{count} | Key: {key}"
    emb = Embed(
        title=f"Category: {vote_type.capitalize()}",
        description=text,
        color=EMBED_COLOR
    )
    emb.set_footer(
        text=footer_text,
        icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else discord.Embed.Empty
    )
    thread = bot.get_channel(channel_id)
    if thread:
        msg = await thread.send(embed=emb)
        await thread.send(f"Fandom: {fandom_url}")
    else:
        print("Ветка для голосов не найдена!")
    loc = get_locale(lang)
    await interaction.response.send_message(
        loc.get('vote_sent', f"Ваш голос отправлен. Ключ: {key}").format(key=key)
    )

# ==== Команды организаторов ====
def is_organizer(user):
    return user.id in ORGANIZERS

async def find_vote_by_key(key):
    return votes_db.get(key)

@tree.command(name="ban", description="Organizer: ban a user by ID")
@app_commands.describe(user_id="User ID for banning")
async def ban(interaction: Interaction, user_id: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'])
        return
    user_id_str = str(user_id).strip()
    if user_id_str in banned_users:
        loc = get_locale(get_user_lang(interaction.user.id))
        await interaction.response.send_message(loc['already_banned'])
        return
    banned_users.add(user_id_str)
    save_banned_users()
    loc = get_locale(get_user_lang(interaction.user.id))
    await interaction.response.send_message(loc['ban_success'])

@tree.command(name="unban", description="Organizer: unban a user by ID")
@app_commands.describe(user_id="User ID for unbanning")
async def unban(interaction: Interaction, user_id: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'])
        return
    user_id_str = str(user_id).strip()
    if user_id_str not in banned_users:
        loc = get_locale(get_user_lang(interaction.user.id))
        await interaction.response.send_message(loc['not_banned'])
        return
    banned_users.remove(user_id_str)
    save_banned_users()
    loc = get_locale(get_user_lang(interaction.user.id))
    await interaction.response.send_message(loc['unban_success'])

@tree.command(name="accepted", description="Organizer: approve a vote by key")
@app_commands.describe(key="10-digit message key")
async def accepted(interaction: Interaction, key: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'])
        return
    if not key or key not in votes_db:
        await interaction.response.send_message(LOCALES['ru']['msg_not_found'])
        return
    vote = votes_db[key]
    fandom_nick = vote.get('fandom_nick', vote['username'])
    lang = vote['lang']
    user_discord = vote['username']
    if lang in ['ru', 'uk', 'pl']:
        embed_title = f"Голос от пользователя {user_discord} / {fandom_nick}"
    elif lang == 'es':
        embed_title = f"Voz del usuario {user_discord} / {fandom_nick}"
    else:
        embed_title = f"Голос от пользователя {user_discord} / {fandom_nick}"
    emb = Embed(
        title=embed_title,
        description=vote['text'],
        color=EMBED_COLOR
    )
    thread = bot.get_channel(vote.get('accepted_channel_id'))
    if thread:
        await thread.send(embed=emb)
    user = await bot.fetch_user(vote['user_id'])
    loc = get_locale(lang)
    emb2 = Embed(
        title=loc['vote_accepted_title'],
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
        f"The vote from user **{vote['username']}** with key `{key}` has been ACCEPTED, and a notification has been sent to the participant."
    )

@tree.command(name="denied", description="Organizer: deny a vote by key")
@app_commands.describe(key="10-digit message key", reason="Reason for denial")
async def denied(interaction: Interaction, key: str, reason: str):
    if not is_organizer(interaction.user):
        await interaction.response.send_message(LOCALES['ru']['not_organizer'])
        return
    if not key or key not in votes_db:
        await interaction.response.send_message(LOCALES['ru']['msg_not_found'])
        return
    vote = votes_db[key]
    user = await bot.fetch_user(vote['user_id'])
    loc = get_locale(vote['lang'])
    emb = Embed(
        title=loc['vote_rejected_title'],
        description=loc['vote_denied'].format(reason=reason),
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
            f":warning: Failed to send a direct message to the user, but the vote with key `{key}` has been REJECTED."
        )
    else:
        await interaction.response.send_message(
            f"The vote from user **{vote['username']}** with key `{key}` has been REJECTED. Reason: {reason}. A notification has been sent to the participant."
        )

@tree.command(name="reply", description="Organizer: reply to a user by key")
@app_commands.describe(key="10-digit message key", text="Текст ответа")
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
        title="Organizer's reply",
        description=f"{text}\n\nYour original vote: {vote['text']}\Key: {key}",
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

def load_user_message_count():
    global user_message_count
    if os.path.exists(USER_MESSAGE_COUNT_FILE):
        try:
            with open(USER_MESSAGE_COUNT_FILE, "r", encoding="utf-8") as f:
                user_message_count = json.load(f)
        except Exception:
            user_message_count = {}
    else:
        user_message_count = {}

def save_user_message_count():
    try:
        with open(USER_MESSAGE_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump(user_message_count, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# == Запуск бота ==
if __name__ == '__main__':
    load_votes_db()
    load_banned_users()
    load_user_message_count()
    load_user_lang()
    load_user_fandom_nick()
    bot.run(TOKEN)
