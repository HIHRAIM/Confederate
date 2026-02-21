from config import ADMINS
import db
import itertools

def is_admin(platform, user_id):
    return user_id in ADMINS.get(platform, set())

def extract_username_from_bot_message(text: str):
    try:
        return text.split("]", 1)[1].split(":", 1)[0].strip()
    except Exception:
        return None

def is_chat_admin(platform, chat_id, user_id):
    row = db.cur.execute(
        """
        SELECT 1 FROM chat_admins
        WHERE platform=? AND chat_id=? AND user_id=?
        """,
        (platform, chat_id, str(user_id))
    ).fetchone()
    if row:
        return True

    if ":" in chat_id:
        prefix = chat_id.split(":", 1)[0]
        group_key = f"{prefix}:0"
        row = db.cur.execute(
            """
            SELECT 1 FROM chat_admins
            WHERE platform=? AND chat_id=? AND user_id=?
            """,
            (platform, group_key, str(user_id))
        ).fetchone()
        return row is not None

    return False

async def log_error(text):
    try:
        from discord_bot import bot
        channel = bot.get_channel(1365305667992420362)
        if channel:
            await channel.send(f"⚠️ {text}")
    except:
        pass

STATUS_LANGUAGES = {
    'ru': {
        'template': "Объединяет {members} {members_word} с {servers} {servers_word}",
        'members': ('участника', 'участника', 'участников'),
        'servers': ('сервера', 'серверов', 'серверов')
    },
    'uk': {
        'template': "Об'єднує {members} {members_word} з {servers} {servers_word}",
        'members': ('учасника', 'учасника', 'учасників'),
        'servers': ('сервера', 'серверів', 'серверів')
    },
    'pl': {
        'template': "Łączy {members} {members_word} z {servers} {servers_word}",
        'members': ('użytkownika', 'użytkowników', 'użytkowników'),
        'servers': ('serwera', 'serwerów', 'serwerów')
    },
    'en': {
        'template': "Connecting {members} {members_word} from {servers} {servers_word}",
        'members': ('member', 'members', 'members'),
        'servers': ('server', 'servers', 'servers')
    },
    'es': {
        'template': "Uniendo a {members} {members_word} de {servers} {servers_word}",
        'members': ('miembro', 'miembros', 'miembros'),
        'servers': ('servidor', 'servidores', 'servidores')
    },
    'pt': {
        'template': "Conectando {members} {members_word} de {servers} {servers_word}",
        'members': ('membro', 'membros', 'membros'),
        'servers': ('servidor', 'servidores', 'servidores')
    }
}

_status_lang_cycle = itertools.cycle(['ru', 'uk', 'pl', 'en', 'es', 'pt'])


def get_next_status_text(total_members, total_servers):
    """
    Возвращает текст статуса на следующем языке в цикле.
    """
    lang_code = next(_status_lang_cycle)
    data = STATUS_LANGUAGES.get(lang_code, STATUS_LANGUAGES['en'])
    
    if lang_code in ('ru', 'uk'):
        plural_func = plural_ru
    elif lang_code == 'pl':
        plural_func = plural_pl
    else:
        plural_func = plural_en

    m_word = plural_func(total_members, data['members'])
    s_word = plural_func(total_servers, data['servers'])

    return data['template'].format(
        members=total_members,
        members_word=m_word,
        servers=total_servers,
        servers_word=s_word
    )

SUPPORTED_LANGS = {"ru", "uk", "pl", "en", "es", "pt"}
DEFAULT_LANG = "en"

# Заменить текущее определение _LOCALE на этот блок
_LOCALE = {
    "replying": {
        "ru": "(отвечая {name})",
        "uk": "(відповідаючи {name})",
        "pl": "(odpowiadając {name})",
        "en": "(replying to {name})",
        "es": "(respondiendo a {name})",
        "pt": "(respondendo a {name})",
    },
    "forward_from_chat": {
        "ru": "(переслано из {name})",
        "uk": "(переслано з {name})",
        "pl": "(przesłane z {name})",
        "en": "(forwarded from {name})",
        "es": "(reenviado desde {name})",
        "pt": "(encaminhado de {name})",
    },
    "forward_from_user": {
        "ru": "(переслано от {name})",
        "uk": "(переслано від {name})",
        "pl": "(przesłane od {name})",
        "en": "(forwarded from {name})",
        "es": "(reenviado de {name})",
        "pt": "(encaminhado de {name})",
    },
    "forward_unknown": {
        "ru": "(переслано из неизвестного источника)",
        "uk": "(переслано з невідомого джерела)",
        "pl": "(przesłane z nieznanego źródła)",
        "en": "(forwarded from unknown source)",
        "es": "(reenviado desde una fuente desconocida)",
        "pt": "(encaminhado de uma fonte desconhecida)",
    },
    "file_count": {
        "ru": "{count} {files} из Telegram",
        "uk": "{count} {files} з Telegram",
        "pl": "{count} {files} z Telegram",
        "en": "{count} {files} from Telegram",
        "es": "{count} {files} de Telegram",
        "pt": "{count} {files} do Telegram",
    },
    "bridge_join": {
        "ru": "{channel} из {server} присоединился(ась) к мосту.",
        "uk": "{channel} з {server} приєднався(лася) до мосту.",
        "pl": "{channel} z {server} dołączył(a) do mostu.",
        "en": "{channel} from {server} joined the bridge.",
        "es": "{channel} de {server} se unió al puente.",
        "pt": "{channel} de {server} entrou na ponte."
    },
    "bridge_leave": {
        "ru": "{channel} из {server} исключён(а) из моста.",
        "uk": "{channel} з {server} виключено з мосту.",
        "pl": "{channel} z {server} został(a) usunięty(a) z mostu.",
        "en": "{channel} from {server} was removed from the bridge.",
        "es": "{channel} de {server} fue eliminado(a) del puente.",
        "pt": "{channel} de {server} foi removido(a) da ponte."
    },
    "bot_joined": {
        "ru": "Бот присоединился к мосту.",
        "uk": "Бот приєднався до мосту.",
        "pl": "Bot dołączył(a) do mostu.",
        "en": "Bot joined the bridge.",
        "es": "El bot se unió al puente.",
        "pt": "O bot entrou na ponte."
    },
}

_PLURALS = {
    "ru": ["файл", "файла", "файлов"],
    "uk": ["файл", "файли", "файлів"],
    "pl": ["plik", "pliki", "plików"],
    "en": ["file", "files"],
    "es": ["archivo", "archivos"],
    "pt": ["arquivo", "arquivos"],
}

def get_chat_lang(chat_id):
    lang = db.get_chat_lang(chat_id)
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG

def set_chat_lang(chat_id, lang_code):
    if lang_code not in SUPPORTED_LANGS:
        raise ValueError("unsupported_lang")
    db.set_chat_lang(chat_id, lang_code)

def plural_ru(n, forms):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_en(n, forms):
    return forms[0] if n == 1 else forms[1]

def plural_pl(n, forms):
    n = abs(int(n))
    if n == 1:
        return forms[0]
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return forms[1]
    return forms[2]

def plural_for(lang, n):
    if lang == "ru":
        return plural_ru(n, _PLURALS["ru"])
    if lang == "uk":
        return plural_ru(n, _PLURALS["uk"])
    if lang == "pl":
        return plural_pl(n, _PLURALS["pl"])
    if lang in ("es", "pt"):
        return plural_en(n, _PLURALS[lang])
    return plural_en(n, _PLURALS["en"])

def localized_file_count_text(n, lang):
    template = _LOCALE["file_count"].get(lang, _LOCALE["file_count"][DEFAULT_LANG])
    word = plural_for(lang, n)
    return template.format(count=n, files=word)

def localized_forward_from_chat(name, lang):
    return _LOCALE["forward_from_chat"].get(lang, _LOCALE["forward_from_chat"][DEFAULT_LANG]).format(name=name)

def localized_forward_from_user(name, lang):
    return _LOCALE["forward_from_user"].get(lang, _LOCALE["forward_from_user"][DEFAULT_LANG]).format(name=name)

def localized_forward_unknown(lang):
    return _LOCALE["forward_unknown"].get(lang, _LOCALE["forward_unknown"][DEFAULT_LANG])

def localized_replying(name, lang):
    return _LOCALE["replying"].get(lang, _LOCALE["replying"][DEFAULT_LANG]).format(name=name)

def localized_bridge_join(channel, server, lang):
    template = _LOCALE["bridge_join"].get(lang, _LOCALE["bridge_join"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bridge_leave(channel, server, lang):
    template = _LOCALE["bridge_leave"].get(lang, _LOCALE["bridge_leave"][DEFAULT_LANG])
    return template.format(channel=channel, server=server)

def localized_bot_joined(lang):
    return _LOCALE["bot_joined"].get(lang, _LOCALE["bot_joined"][DEFAULT_LANG])
