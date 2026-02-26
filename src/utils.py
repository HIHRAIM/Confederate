from config import ADMINS, SERVICE_CHATS
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
        for chat_key in SERVICE_CHATS.get("discord", set()):
            try:
                key = str(chat_key)
                guild_id = None
                channel_id = int(key.split(":", 1)[1]) if ":" in key else int(key)
                if ":" in key:
                    try:
                        guild_id = int(key.split(":", 1)[0])
                    except Exception:
                        guild_id = None
                channel = bot.get_channel(channel_id)
                if not channel:
                    channel = await bot.fetch_channel(channel_id)
                if guild_id is None and channel and getattr(channel, "guild", None):
                    guild_id = channel.guild.id
                lang_key = f"{guild_id}:{channel_id}" if guild_id is not None else str(channel_id)
                lang = get_chat_lang(lang_key)
                localized_text = localized_service_event("daily_loop_error", lang, error=text)
                if channel:
                    await channel.send(f"⚠️ {localized_text}")
            except Exception:
                pass
    except Exception:
        pass

STATUS_LANGUAGES = {
    'ru': {
        'template': "Объединяет {members} {members_word} из {servers} {servers_word}",
        'members': ('участника', 'участника', 'участников'),
        'servers': ('сообщества', 'сообществ', 'сообществ')
    },
    'uk': {
        'template': "Об'єднує {members} {members_word} із {servers} {servers_word}",
        'members': ('учасника', 'учасника', 'учасників'),
        'servers': ('спільноти', 'спільнот', 'спільнот')
    },
    'pl': {
        'template': "Łączy {members} {members_word} z {servers} {servers_word}",
        'members': ('uczestnika', 'uczestników', 'uczestników'),
        'servers': ('społeczności', 'społeczności', 'społeczności')
    },
    'en': {
        'template': "Connects {members} {members_word} across {servers} {servers_word}",
        'members': ('member', 'members', 'members'),
        'servers': ('community', 'communities', 'communities')
    },
    'es': {
        'template': "Une a {members} {members_word} de {servers} {servers_word}",
        'members': ('miembro', 'miembros', 'miembros'),
        'servers': ('comunidad', 'comunidades', 'comunidades')
    },
    'pt': {
        'template': "Conecta {members} {members_word} de {servers} {servers_word}",
        'members': ('membro', 'membros', 'membros'),
        'servers': ('comunidade', 'comunidades', 'comunidades')
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
    "file_forms": {
        "ru": ["файл", "файла", "файлов"],
        "uk": ["файл", "файли", "файлів"],
        "pl": ["plik", "pliki", "plików"],
        "en": ["file", "files"],
        "es": ["archivo", "archivos"],
        "pt": ["arquivo", "arquivos"],
    },
    "bridge_join": {
        "ru": "Чат {channel} из {server} присоединился к мосту.",
        "uk": "Чат {channel} з {server} приєднався до мосту.",
        "pl": "Czat {channel} z {server} został podłączony do mostu.",
        "en": "Chat {channel} from {server} was connected to the bridge.",
        "es": "El chat {channel} de {server} fue conectado al puente.",
        "pt": "O chat {channel} de {server} foi conectado à ponte."
    },
    "bridge_leave": {
        "ru": "Чат {channel} из {server} исключён из моста.",
        "uk": "Чат {channel} з {server} виключено з мосту.",
        "pl": "Czat {channel} z {server} został odłączony od mostu.",
        "en": "Chat {channel} from {server} was disconnected from the bridge.",
        "es": "El chat {channel} de {server} fue desconectado del puente.",
        "pt": "O chat {channel} de {server} foi desconectado da ponte."
    },
    "bot_joined": {
        "ru": "Бот присоединился к мосту.",
        "uk": "Бот приєднався до мосту.",
        "pl": "Bot dołączył(a) do mostu.",
        "en": "Bot joined the bridge.",
        "es": "El bot se unió al puente.",
        "pt": "O bot entrou na ponte."
    },
    "consent_title": {
        "ru": "Внимание — мост сообщений",
        "uk": "Увага — міст повідомлень",
        "pl": "Uwaga — most wiadomości",
        "en": "Notice — message bridge",
        "es": "Aviso — puente de mensajes",
        "pt": "Aviso — ponte de mensagens"
    },
    "consent_body": {
        "ru": "Этот чат связан с другими. Все твои сообщения будут автоматически пересылаться в связанные чаты. Ты можешь изменить или удалить любое своё сообщение через оригинал в течение 30 дней после отправки. Нажми «Принимаю», чтобы согласиться с пересылкой и продолжить общение. Подробнее — в описании чата или закреплённом сообщении.",
        "uk": "Цей чат пов’язано з іншими. Усі твої повідомлення будуть автоматично пересилатися до пов’язаних чатів. Ти можеш змінити або видалити будь-яке своє повідомлення через оригінал протягом 30 днів після відправлення. Натисни «Приймаю», щоб погодитися з пересиланням і продовжити спілкування. Деталі — в описі чату або закріпленому повідомленні.",
        "pl": "Ten czat jest połączony z innymi. Wszystkie Twoje wiadomości będą automatycznie przesyłane do powiązanych czatów. Możesz zmienić lub usunąć dowolną swoją wiadomość poprzez oryginał w ciągu 30 dni od wysłania. Kliknij „Akceptuję”, aby zgodzić się na przesyłanie i dalej rozmawiać. Szczegóły w opisie czatu lub w przypiętej wiadomości.",
        "en": "This chat is linked with other chats. All your messages will be automatically forwarded to linked chats. You can edit or delete any of your messages through the original message within 30 days after sending. Tap “I accept” to agree to forwarding and continue chatting. More details are in the chat description or the pinned message.",
        "es": "Este chat está vinculado con otros. Todos tus mensajes serán reenviados automáticamente a los chats vinculados. Puedes editar o eliminar cualquiera de tus mensajes desde el mensaje original durante 30 días después del envío. Pulsa «Acepto» para aceptar el reenvío y seguir conversando. Más información en la descripción del chat o en el mensaje fijado.",
        "pt": "Este chat está ligado a outros. Todas as suas mensagens serão encaminhadas automaticamente para os chats ligados. Pode editar ou apagar qualquer uma das suas mensagens através da mensagem original durante 30 dias após o envio. Toque em «Aceito» para concordar com o encaminhamento e continuar a conversar. Mais detalhes na descrição do chat ou na mensagem fixada."
    },
    "consent_button": {
        "ru": "Принимаю",
        "uk": "Приймаю",
        "pl": "Akceptuję",
        "en": "I accept",
        "es": "Acepto",
        "pt": "Aceito"
    },
    "sticker": {
        "ru": "[Стикер]",
        "uk": "[Стікер]",
        "pl": "[Naklejka]",
        "en": "[Sticker]",
        "es": "[Sticker]",
        "pt": "[Sticker]"
    },
    "discord_system_event": {
        "ru": "{name} {action}",
        "uk": "{name} {action}",
        "pl": "{name} {action}",
        "en": "{name} {action}",
        "es": "{name} {action}",
        "pt": "{name} {action}",
    },
    "discord_system_event_action": {
        "boosted_server": {
            "ru": "дал буст серверу",
            "uk": "дав буст серверу",
            "pl": "dał boost serwerowi",
            "en": "boosted the server",
            "es": "dio un impulso al servidor",
            "pt": "deu boost no servidor"
        },
        "created_thread": {
            "ru": "создал ветку",
            "uk": "створив гілку",
            "pl": "utworzył wątek",
            "en": "created a thread",
            "es": "creó un hilo",
            "pt": "criou uma thread"
        },
        "pinned_message": {
            "ru": "закрепил сообщение",
            "uk": "закріпив повідомлення",
            "pl": "przypiął wiadomość",
            "en": "pinned a message",
            "es": "fijó un mensaje",
            "pt": "fixou uma mensagem"
        },
        "joined_server": {
            "ru": "присоединился к серверу",
            "uk": "приєднався до сервера",
            "pl": "dołączył do serwera",
            "en": "joined the server",
            "es": "se unió al servidor",
            "pt": "entrou no servidor"
        }
    },
    "service_event": {
        "bot_started": {
            "ru": "🤖 Бот запущен.",
            "uk": "🤖 Бота запущено.",
            "pl": "🤖 Bot uruchomiony.",
            "en": "🤖 Bot started.",
            "es": "🤖 Bot iniciado.",
            "pt": "🤖 Bot iniciado."
        },
        "bot_stopped": {
            "ru": "🛑 Бот остановлен.",
            "uk": "🛑 Бота зупинено.",
            "pl": "🛑 Bot zatrzymany.",
            "en": "🛑 Bot stopped.",
            "es": "🛑 Bot detenido.",
            "pt": "🛑 Bot parado."
        },
        "daily_missing_tg_chat": {
            "ru": "Не вижу чат Telegram {chat_key}.",
            "uk": "Не бачу чат Telegram {chat_key}.",
            "pl": "Nie widzę czatu Telegram {chat_key}.",
            "en": "Cannot access Telegram chat {chat_key}.",
            "es": "No puedo acceder al chat de Telegram {chat_key}.",
            "pt": "Não consigo acessar o chat do Telegram {chat_key}."
        },
        "daily_no_tg_delete_perm": {
            "ru": "Недостаточно прав удалять сообщения в Telegram чате {chat_key}.",
            "uk": "Недостатньо прав видаляти повідомлення в Telegram-чаті {chat_key}.",
            "pl": "Brak uprawnień do usuwania wiadomości w czacie Telegram {chat_key}.",
            "en": "Missing permission to delete messages in Telegram chat {chat_key}.",
            "es": "Faltan permisos para eliminar mensajes en el chat de Telegram {chat_key}.",
            "pt": "Permissão ausente para apagar mensagens no chat do Telegram {chat_key}."
        },
        "daily_tg_perm_check_error": {
            "ru": "Ошибка проверки прав в Telegram чате {chat_key}: {error}",
            "uk": "Помилка перевірки прав у Telegram-чаті {chat_key}: {error}",
            "pl": "Błąd sprawdzania uprawnień w czacie Telegram {chat_key}: {error}",
            "en": "Error checking permissions in Telegram chat {chat_key}: {error}",
            "es": "Error al comprobar permisos en el chat de Telegram {chat_key}: {error}",
            "pt": "Erro ao verificar permissões no chat do Telegram {chat_key}: {error}"
        },
        "daily_missing_dc_channel": {
            "ru": "Не вижу Discord-канал {chat_key}.",
            "uk": "Не бачу Discord-канал {chat_key}.",
            "pl": "Nie widzę kanału Discord {chat_key}.",
            "en": "Cannot access Discord channel {chat_key}.",
            "es": "No puedo acceder al canal de Discord {chat_key}.",
            "pt": "Não consigo acessar o canal do Discord {chat_key}."
        },
        "daily_no_dc_manage_perm": {
            "ru": "Недостаточно прав manage_messages в Discord чате {chat_key}.",
            "uk": "Недостатньо прав manage_messages у Discord-чаті {chat_key}.",
            "pl": "Brak uprawnień manage_messages na czacie Discord {chat_key}.",
            "en": "Missing manage_messages permission in Discord chat {chat_key}.",
            "es": "Falta el permiso manage_messages en el chat de Discord {chat_key}.",
            "pt": "Permissão manage_messages ausente no chat do Discord {chat_key}."
        },
        "daily_loop_error": {
            "ru": "Ошибка цикла daily_check_loop: {error}",
            "uk": "Помилка циклу daily_check_loop: {error}",
            "pl": "Błąd pętli daily_check_loop: {error}",
            "en": "daily_check_loop error: {error}",
            "es": "Error de daily_check_loop: {error}",
            "pt": "Erro no daily_check_loop: {error}"
        }
    },
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
    file_forms = _LOCALE["file_forms"]
    if lang == "ru":
        return plural_ru(n, file_forms["ru"])
    if lang == "uk":
        return plural_ru(n, file_forms["uk"])
    if lang == "pl":
        return plural_pl(n, file_forms["pl"])
    if lang in ("es", "pt"):
        return plural_en(n, file_forms[lang])
    return plural_en(n, file_forms["en"])

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

def localized_consent_title(lang):
    return _LOCALE["consent_title"].get(lang, _LOCALE["consent_title"][DEFAULT_LANG])

def localized_consent_body(lang):
    return _LOCALE["consent_body"].get(lang, _LOCALE["consent_body"][DEFAULT_LANG])

def localized_consent_button(lang):
    return _LOCALE["consent_button"].get(lang, _LOCALE["consent_button"][DEFAULT_LANG])

def localized_sticker(lang):
    return _LOCALE["sticker"].get(lang, _LOCALE["sticker"][DEFAULT_LANG])

def localized_discord_system_event(name, event_key, lang):
    action_table = _LOCALE.get("discord_system_event_action", {}).get(event_key, {})
    action = action_table.get(lang, action_table.get(DEFAULT_LANG, event_key))
    template = _LOCALE.get("discord_system_event", {}).get(lang, _LOCALE["discord_system_event"][DEFAULT_LANG])
    return template.format(name=name, action=action)

def localized_service_event(event_key, lang, **kwargs):
    table = _LOCALE.get("service_event", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
