import re
from config import ADMINS, SERVICE_CHATS
import db
import itertools

def is_admin(platform, user_id):
    return user_id in ADMINS.get(platform, set())

def extract_username_from_bot_message(text: str):
    if not text:
        return None

    try:
        for raw_line in str(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue

            m = re.match(r"^\[[^\]]+\]\s*(.+?)\s*:\s*$", line)
            if m:
                name = m.group(1).strip()
                return name or None

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
        "ru": "Внимание: мост сообщений",
        "uk": "Увага: міст повідомлень",
        "pl": "Uwaga: mostek wiadomości",
        "en": "Notice: Message Bridge",
        "es": "Atención: puente de mensajes",
        "pt": "Atenção: ponte de mensagens"
    },
    "consent_body": {
        "ru": "Этот чат соединён с другими. Твои сообщения будут автоматически пересылаться в связанные чаты. Если ты изменишь или удалишь своё сообщение в течение 30 дней с его отправки, оно автоматически обновится или исчезнет и в других чатах. Нажми «Принимаю», чтобы согласиться с пересылкой и продолжить общение. Подробнее — в описании чата или закреплённом сообщении.",
        "uk": "Цей чат з'єднаний з іншими. Твої повідомлення автоматично надсилатимуться до пов'язаних чатів. Якщо ти зміниш або видалиш своє повідомлення протягом 30 днів з моменту його надсилання, воно автоматично оновиться або зникне й в інших чатах. Натисни «Погоджуюсь», щоб погодитися з пересиланням і продовжити спілкування. Детальніше — в описі чату або у закріпленому повідомленні.",
        "pl": "Ten czat jest połączony z innymi. Twoje wiadomości będą automatycznie przesyłane do powiązanych czatów. Jeśli edytujesz lub usuniesz swoją wiadomość w ciągu 30 dni od jej wysłania, automatycznie zaktualizuje się ona lub zniknie również na innych czatach. Kliknij „Akceptuję”, aby wyrazić zgodę na przesyłanie i kontynuować rozmowę. Więcej informacji znajdziesz w opisie czatu lub w przypiętej wiadomości.",
        "en": "This chat is connected to others. Your messages will be automatically forwarded to linked chats. If you edit or delete your message within 30 days of sending, it will automatically update or disappear in the other chats as well. Click \"Accept\" to agree to the forwarding and continue chatting. For more details, check the chat description or the pinned message.",
        "es": "Este chat está conectado con otros. Tus mensajes se reenviarán automáticamente a los chats vinculados. Si editas o eliminas tu mensaje dentro de los 30 días posteriores a su envío, también se actualizará o desaparecerá automáticamente en los demás chats. Haz clic en «Aceptar» para dar tu consentimiento y seguir chateando. Para más detalles, consulta la descripción del chat o el mensaje fijado.",
        "pt": "Este chat está conectado a outros. As suas mensagens serão encaminhadas automaticamente para os chats vinculados. Se você editar ou excluir a sua mensagem em até 30 dias após o envio, ela também será atualizada ou desaparecerá automaticamente nos outros chats. Clique em \"Aceitar\" para concordar com o encaminhamento e continuar conversando. Para mais detalhes, verifique a descrição do chat ou a mensagem fixada."
    },
    "consent_button": {
        "ru": "Принимаю",
        "uk": "Погоджуюсь",
        "pl": "Akceptuję",
        "en": "Accept",
        "es": "Aceptar",
        "pt": "Aceitar"
    },
    "sticker": {
        "ru": "[Стикер]",
        "uk": "[Стікер]",
        "pl": "[Naklejka]",
        "en": "[Sticker]",
        "es": "[Sticker]",
        "pt": "[Sticker]"
    },
    "voice_message": {
        "ru": "[Голосовое сообщение]",
        "uk": "[Голосове повідомлення]",
        "pl": "[Wiadomość głosowa]",
        "en": "[Voice message]",
        "es": "[Mensaje de voz]",
        "pt": "[Mensagem de voz]"
    },
    "video_message": {
        "ru": "[Видеосообщение]",
        "uk": "[Відеоповідомлення]",
        "pl": "[Wiadomość wideo]",
        "en": "[Video message]",
        "es": "[Mensaje de video]",
        "pt": "[Mensagem de vídeo]"
    },
    "reply_unknown": {
        "ru": "(ответ на неизвестное сообщение)",
        "uk": "(відповідь на невідоме повідомлення)",
        "pl": "(odpowiedź na nieznaną wiadomość)",
        "en": "(reply to an unknown message)",
        "es": "(respuesta a un mensaje desconocido)",
        "pt": "(resposta a uma mensagem desconhecida)",
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
        },
        "daily_auto_removed_chat": {
            "ru": "Автоудаление из моста: чат {chat_key} ({platform}) был недоступен более 24 часов.",
            "uk": "Автовидалення з мосту: чат {chat_key} ({platform}) був недоступний понад 24 години.",
            "pl": "Automatyczne usunięcie z mostu: czat {chat_key} ({platform}) był niedostępny ponad 24 godziny.",
            "en": "Auto-removed from bridge: chat {chat_key} ({platform}) was inaccessible for over 24 hours.",
            "es": "Eliminado automáticamente del puente: el chat {chat_key} ({platform}) estuvo inaccesible por más de 24 horas.",
            "pt": "Removido automaticamente da ponte: o chat {chat_key} ({platform}) ficou inacessível por mais de 24 horas."
        },
        "daily_auto_removed_bridge": {
            "ru": "Мост {bridge_id} удалён из базы данных: в нём не осталось чатов.",
            "uk": "Міст {bridge_id} видалено з бази даних: у ньому не залишилося чатів.",
            "pl": "Most {bridge_id} został usunięty z bazy danych: nie pozostały w nim żadne czaty.",
            "en": "Bridge {bridge_id} was deleted from the database because no chats remained.",
            "es": "El puente {bridge_id} se eliminó de la base de datos porque no quedaron chats.",
            "pt": "A ponte {bridge_id} foi removida do banco de dados porque não restaram chats."
        }
    },
    "deadtopic": {
        "enabled": {
            "ru": "Авто-сохранение темы включено. Каждые 6 дней без активности в полночь UTC бот пришлёт и удалит фантомное сообщение.",
            "uk": "Авто-збереження теми увімкнено. Щожних 6 днів без активності опівночі UTC бот надішле та видалить фантомне повідомлення.",
            "pl": "Automatyczne zachowanie tematu włączone. Co 6 dni braku aktywności o północy UTC bot wyśle i usunie wiadomość widmo.",
            "en": "Dead topic prevention enabled. Every 6 days of inactivity at midnight UTC the bot will send and delete a phantom message.",
            "es": "Prevención de tema muerto activada. Cada 6 días de inactividad a medianoche UTC el bot enviará y eliminará un mensaje fantasma.",
            "pt": "Prevenção de tópico inativo ativada. A cada 6 dias de inatividade à meia-noite UTC o bot enviará e excluirá uma mensagem fantasma.",
        },
        "disabled": {
            "ru": "Авто-сохранение темы отключено.",
            "uk": "Авто-збереження теми вимкнено.",
            "pl": "Automatyczne zachowanie tematu wyłączone.",
            "en": "Dead topic prevention disabled.",
            "es": "Prevención de tema muerto desactivada.",
            "pt": "Prevenção de tópico inativo desativada.",
        },
        "phantom_message": {
            "ru": "Фантомное сообщение для сохранения темы.",
            "uk": "Фантомне повідомлення для збереження теми.",
            "pl": "Wiadomość widmo dla zachowania tematu.",
            "en": "Phantom message to preserve the topic.",
            "es": "Mensaje fantasma para mantener el tema.",
            "pt": "Mensagem fantasma para manter o tópico.",
        },
    },
    "bridge_info": {
        "title": {
            "ru": "Информация о мосте",
            "uk": "Інформація про міст",
            "pl": "Informacje o moście",
            "en": "Bridge information",
            "es": "Información del puente",
            "pt": "Informações da ponte"
        },
        "field_number": {
            "ru": "Номер",
            "uk": "Номер",
            "pl": "Numer",
            "en": "Number",
            "es": "Número",
            "pt": "Número"
        },
        "field_chats": {
            "ru": "Подключённые чаты",
            "uk": "Підключені чати",
            "pl": "Podłączone czaty",
            "en": "Connected chats",
            "es": "Chats conectados",
            "pt": "Chats conectados"
        },
        "not_in_bridge": {
            "ru": "Этот чат не подключён к мосту.",
            "uk": "Цей чат не підключений до мосту.",
            "pl": "Ten czat nie jest podłączony do mostu.",
            "en": "This chat is not connected to a bridge.",
            "es": "Este chat no está conectado a ningún puente.",
            "pt": "Este chat não está conectado a nenhuma ponte."
        },
        "tg_template": {
            "ru": "Информация о мосте\n\nНомер: {bridge_id}\nПодключённые чаты:\n{chats}",
            "uk": "Інформація про міст\n\nНомер: {bridge_id}\nПідключені чати:\n{chats}",
            "pl": "Informacje o moście\n\nNumer: {bridge_id}\nPodłączone czaty:\n{chats}",
            "en": "Bridge information\n\nNumber: {bridge_id}\nConnected chats:\n{chats}",
            "es": "Información del puente\n\nNúmero: {bridge_id}\nChats conectados:\n{chats}",
            "pt": "Informações da ponte\n\nNúmero: {bridge_id}\nChats conectados:\n{chats}"
        },
        "unknown": {
            "ru": "Неизвестно",
            "uk": "Невідомо",
            "pl": "Nieznane",
            "en": "Unknown",
            "es": "Desconocido",
            "pt": "Desconhecido"
        },
        "topic": {
            "ru": "тема {thread_id}",
            "uk": "тема {thread_id}",
            "pl": "temat {thread_id}",
            "en": "topic {thread_id}",
            "es": "tema {thread_id}",
            "pt": "tópico {thread_id}"
        }
    },
    "help": {
        "title": {
            "ru": "Команды бота",
            "uk": "Команди бота",
            "pl": "Komendy bota",
            "en": "Bot commands",
            "es": "Comandos del bot",
            "pt": "Comandos do bot",
        },
        "section_everyone": {
            "ru": "Для всех",
            "uk": "Для всіх",
            "pl": "Dla wszystkich",
            "en": "For everyone",
            "es": "Para todos",
            "pt": "Para todos",
        },
        "section_admins": {
            "ru": "Для админов моста",
            "uk": "Для адмінів моста",
            "pl": "Dla adminów mostów",
            "en": "For Bridge Admins",
            "es": "Para admins de puentes",
            "pt": "Para admines de ponte",
        },
        "cmd_bridge": {
            "ru": "/bridge — информация о мосте и подключённых чатах",
            "uk": "/bridge — інформація про міст і підключені чати",
            "pl": "/bridge — informacje o moście i podłączonych czatach",
            "en": "/bridge — info about the bridge and connected chats",
            "es": "/bridge — información sobre el puente y los chats conectados",
            "pt": "/bridge — informações sobre a ponte e os chats conectados",
        },
        "cmd_whois": {
            "ru": "/whois — информация об авторе сообщения (ответом на relay-сообщение бота)",
            "uk": "/whois — інформація про автора повідомлення (відповіддю на relay-повідомлення бота)",
            "pl": "/whois — informacje o autorze wiadomości (w odpowiedzi na wiadomość relay bota)",
            "en": "/whois — info about the message author (reply to a bot relay message)",
            "es": "/whois — información sobre el autor del mensaje (responde a un mensaje relay del bot)",
            "pt": "/whois — informações sobre o autor da mensagem (responda a uma mensagem relay do bot)",
        },
        "cmd_verify": {
            "ru": "/verify — подтвердить согласие на пересылку сообщений",
            "uk": "/verify — підтвердити згоду на пересилання повідомлень",
            "pl": "/verify — potwierdzenie zgody na przesyłanie wiadomości",
            "en": "/verify — confirm consent to message forwarding",
            "es": "/verify — confirmar el consentimiento para el reenvío de mensajes",
            "pt": "/verify — confirmar o consentimento para o encaminhamento de mensagens",
        },
        "cmd_rfb": {
            "ru": "/rfb — отключить этот чат от моста",
            "uk": "/rfb — відключити цей чат від мосту",
            "pl": "/rfb — odłączyć ten czat od mostu",
            "en": "/rfb — remove this chat from the bridge",
            "es": "/rfb — desconectar este chat del puente",
            "pt": "/rfb — remover este chat da ponte",
        },
        "cmd_setadmin": {
            "ru": "/setadmin <user> — добавить Bridge Admin",
            "uk": "/setadmin <user> — додати Bridge Admin",
            "pl": "/setadmin <user> — dodaj Bridge Admin",
            "en": "/setadmin <user> — add a Bridge Admin",
            "es": "/setadmin <user> — añadir un Bridge Admin",
            "pt": "/setadmin <user> — adicionar um Bridge Admin",
        },
        "cmd_lang": {
            "ru": "/lang <код> — установить язык бота (ru, uk, pl, en, es, pt)",
            "uk": "/lang <код> — встановити мову бота (ru, uk, pl, en, es, pt)",
            "pl": "/lang <kod> — ustawić język bota (ru, uk, pl, en, es, pt)",
            "en": "/lang <code> — set bot language (ru, uk, pl, en, es, pt)",
            "es": "/lang <código> — establecer el idioma del bot (ru, uk, pl, en, es, pt)",
            "pt": "/lang <código> — definir o idioma do bot (ru, uk, pl, en, es, pt)",
        },
        "cmd_remindrules": {
            "ru": "/remindrules <время> [сообщений] — периодически публиковать правила во всех чатах моста (например: 2h, 30m)",
            "uk": "/remindrules <час> [повідомлень] — періодично публікувати правила в усіх чатах мосту (напр.: 2h, 30m)",
            "pl": "/remindrules <czas> [wiadomości] — cyklicznie publikować regulamin we wszystkich czatach mostu (np.: 2h, 30m)",
            "en": "/remindrules <time> [messages] — periodically post rules to all bridge chats (e.g.: 2h, 30m)",
            "es": "/remindrules <tiempo> [mensajes] — publicar reglas periódicamente en todos los chats del puente (ej.: 2h, 30m)",
            "pt": "/remindrules <tempo> [mensagens] — publicar regras periodicamente em todos os chats da ponte (ex.: 2h, 30m)",
        },
        "cmd_shadowban": {
            "ru": "/shadow-ban <user> — скрыть сообщения пользователя от пересылки",
            "uk": "/shadow-ban <user> — приховати повідомлення користувача від пересилання",
            "pl": "/shadow-ban <user> — ukryć wiadomości użytkownika przed przekazywaniem",
            "en": "/shadow-ban <user> — hide user's messages from relay",
            "es": "/shadow-ban <user> — ocultar los mensajes del usuario del relay",
            "pt": "/shadow-ban <user> — ocultar mensagens do usuário do relay",
        },
        "cmd_deadtopic": {
            "ru": "/deadtopic enable|disable — раз в 6 дней без активности отправлять фантомное сообщение для сохранения темы",
            "uk": "/deadtopic enable|disable — раз на 6 днів без активності надсилати фантомне повідомлення для збереження теми",
            "pl": "/deadtopic enable|disable — co 6 dni braku aktywności wysyłać wiadomość widmo w celu zachowania tematu",
            "en": "/deadtopic enable|disable — send a phantom message every 6 days of inactivity to keep the topic alive",
            "es": "/deadtopic enable|disable — enviar un mensaje fantasma cada 6 días de inactividad para mantener el tema activo",
            "pt": "/deadtopic enable|disable — enviar mensagem fantasma a cada 6 dias de inatividade para manter o tópico ativo",
        },
        "cmd_deadchat": {
            "ru": "/deadchat <роль> <часы> | disable — пинговать роль при неактивности (только Discord)",
            "uk": "/deadchat <роль> <години> | disable — пінгувати роль при неактивності (тільки Discord)",
            "pl": "/deadchat <rola> <godziny> | disable — pingować rolę przy braku aktywności (tylko Discord)",
            "en": "/deadchat <role> <hours> | disable — ping a role when chat is inactive (Discord only)",
            "es": "/deadchat <rol> <horas> | disable — mencionar un rol cuando el chat está inactivo (solo Discord)",
            "pt": "/deadchat <cargo> <horas> | disable — mencionar um cargo quando o chat está inativo (somente Discord)",
        },
        "cmd_newschat": {
            "ru": "/newschat add <эмодзи> | disable — авто-реакция на сообщения в канале новостей (только Discord)",
            "uk": "/newschat add <емодзі> | disable — авто-реакція на повідомлення в каналі новин (тільки Discord)",
            "pl": "/newschat add <emoji> | disable — auto-reakcja na wiadomości w kanale newsów (tylko Discord)",
            "en": "/newschat add <emoji> | disable — auto-react to messages in a news channel (Discord only)",
            "es": "/newschat add <emoji> | disable — reaccionar automáticamente a mensajes en un canal de noticias (solo Discord)",
            "pt": "/newschat add <emoji> | disable — reagir automaticamente a mensagens em um canal de notícias (somente Discord)",
        },
    },
    "whois": {
        "use_reply": {
            "ru": "Используйте эту команду ответом на relay-сообщение бота.",
            "uk": "Використайте цю команду у відповіді на relay-повідомлення бота.",
            "pl": "Użyj tej komendy w odpowiedzi na wiadomość relay bota.",
            "en": "Use this command in reply to a bot relay message.",
            "es": "Usa este comando respondiendo a un mensaje relay del bot.",
            "pt": "Use este comando respondendo a uma mensagem relay do bot."
        },
        "use_context_menu": {
            "ru": "Slash-команда не может определить сообщение. Используйте контекстное меню: ПКМ на relay-сообщении → Apps → whois.",
            "uk": "Slash-команда не може визначити повідомлення. Використайте контекстне меню: ПКМ на relay-повідомленні → Apps → whois.",
            "pl": "Slash-komenda nie może określić wiadomości. Użyj menu kontekstowego: PPM na wiadomości relay → Apps → whois.",
            "en": "Slash commands cannot detect the replied message. Use the context menu instead: right-click the relay message → Apps → whois.",
            "es": "Los slash commands no pueden detectar el mensaje. Usa el menú contextual: clic derecho en el mensaje relay → Apps → whois.",
            "pt": "Slash commands não conseguem detectar a mensagem. Use o menu de contexto: clique direito na mensagem relay → Apps → whois."
        },
        "origin_not_found": {
            "ru": "Не удалось определить источник сообщения.",
            "uk": "Не вдалося визначити джерело повідомлення.",
            "pl": "Nie udało się ustalić źródła wiadomości.",
            "en": "Could not find the origin for that message.",
            "es": "No se pudo encontrar el origen de ese mensaje.",
            "pt": "Não foi possível encontrar a origem dessa mensagem."
        },
        "origin_missing": {
            "ru": "Исходная запись отсутствует в базе данных.",
            "uk": "Початковий запис відсутній у базі даних.",
            "pl": "Brakuje wpisu źródłowego w bazie danych.",
            "en": "Origin entry is missing in the database.",
            "es": "Falta el registro de origen en la base de datos.",
            "pt": "O registro de origem está ausente no banco de dados."
        },
        "origin_not_telegram": {
            "ru": "Источник не Telegram; используйте /whois в соответствующей платформе.",
            "uk": "Джерело не Telegram; використайте /whois на відповідній платформі.",
            "pl": "Źródło nie jest z Telegrama; użyj /whois na odpowiedniej platformie.",
            "en": "Origin is not Telegram; use /whois on the corresponding platform.",
            "es": "El origen no es Telegram; usa /whois en la plataforma correspondiente.",
            "pt": "A origem não é do Telegram; use /whois na plataforma correspondente."
        },
        "fetch_error": {
            "ru": "Не удалось получить данные пользователя: {error}",
            "uk": "Не вдалося отримати дані користувача: {error}",
            "pl": "Nie udało się pobrać danych użytkownika: {error}",
            "en": "Could not fetch user data: {error}",
            "es": "No se pudieron obtener los datos del usuario: {error}",
            "pt": "Não foi possível obter os dados do usuário: {error}"
        },
        "discord_left_guild": {
            "ru": "Неизвестно (пользователь покинул сервер)",
            "uk": "Невідомо (користувач покинув сервер)",
            "pl": "Nieznane (użytkownik opuścił serwer)",
            "en": "Unknown (user left the server)",
            "es": "Desconocido (el usuario abandonó el servidor)",
            "pt": "Desconhecido (usuário saiu do servidor)"
        },
        "discord_no_bio": {
            "ru": "Биография отсутствует",
            "uk": "Біографія відсутня",
            "pl": "Brak bio",
            "en": "No bio",
            "es": "Sin bio",
            "pt": "Sem bio"
        },
        "title": {
            "ru": "Информация о пользователе",
            "uk": "Інформація про користувача",
            "pl": "Informacje o użytkowniku",
            "en": "User information",
            "es": "Información del usuario",
            "pt": "Informações do usuário"
        },
        "field_nickname": {
            "ru": "Никнейм",
            "uk": "Нікнейм",
            "pl": "Pseudonim",
            "en": "Nickname",
            "es": "Apodo",
            "pt": "Apelido"
        },
        "field_username": {
            "ru": "Юзернейм",
            "uk": "Юзернейм",
            "pl": "Nazwa użytkownika",
            "en": "Username",
            "es": "Nombre de usuario",
            "pt": "Nome de usuário"
        },
        "field_id": {
            "ru": "ID",
            "uk": "ID",
            "pl": "ID",
            "en": "ID",
            "es": "ID",
            "pt": "ID"
        },
        "field_status": {
            "ru": "Статус",
            "uk": "Статус",
            "pl": "Status",
            "en": "Status",
            "es": "Estado",
            "pt": "Status"
        },
        "field_mode": {
            "ru": "Режим",
            "uk": "Режим",
            "pl": "Tryb",
            "en": "Mode",
            "es": "Modo",
            "pt": "Modo"
        },
        "mode_online": {
            "ru": "В сети",
            "uk": "У мережі",
            "pl": "Online",
            "en": "Online",
            "es": "En línea",
            "pt": "Online"
        },
        "mode_idle": {
            "ru": "Неактивен",
            "uk": "Неактивний",
            "pl": "Bezczynny",
            "en": "Idle",
            "es": "Ausente",
            "pt": "Ausente"
        },
        "mode_dnd": {
            "ru": "Не беспокоить",
            "uk": "Не турбувати",
            "pl": "Nie przeszkadzać",
            "en": "Do Not Disturb",
            "es": "No molestar",
            "pt": "Não perturbe"
        },
        "mode_offline": {
            "ru": "Оффлайн",
            "uk": "Офлайн",
            "pl": "Offline",
            "en": "Offline",
            "es": "Desconectado",
            "pt": "Offline"
        },
        "field_bio": {
            "ru": "Био",
            "uk": "Біо",
            "pl": "Bio",
            "en": "Bio",
            "es": "Bio",
            "pt": "Bio"
        },
        "field_registered": {
            "ru": "Дата регистрации Discord",
            "uk": "Дата реєстрації Discord",
            "pl": "Data rejestracji Discord",
            "en": "Discord registration date",
            "es": "Fecha de registro en Discord",
            "pt": "Data de registro no Discord"
        },
        "field_joined_server": {
            "ru": "Дата вступления на сервер",
            "uk": "Дата вступу на сервер",
            "pl": "Data dołączenia do serwera",
            "en": "Server join date",
            "es": "Fecha de ingreso al servidor",
            "pt": "Data de entrada no servidor"
        },
        "tg_template": {
            "ru": "Никнейм: {nickname}\nЮзернейм: {username}\nID: {id}\nБио: {bio}",
            "uk": "Нікнейм: {nickname}\nЮзернейм: {username}\nID: {id}\nБіо: {bio}",
            "pl": "Pseudonim: {nickname}\nNazwa użytkownika: {username}\nID: {id}\nBio: {bio}",
            "en": "Nickname: {nickname}\nUsername: {username}\nID: {id}\nBio: {bio}",
            "es": "Apodo: {nickname}\nUsuario: {username}\nID: {id}\nBio: {bio}",
            "pt": "Apelido: {nickname}\nUsuário: {username}\nID: {id}\nBio: {bio}"
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

def localized_voice_message(lang):
    return _LOCALE["voice_message"].get(lang, _LOCALE["voice_message"][DEFAULT_LANG])

def localized_video_message(lang):
    return _LOCALE["video_message"].get(lang, _LOCALE["video_message"][DEFAULT_LANG])

def localized_reply_unknown(lang):
    return _LOCALE["reply_unknown"].get(lang, _LOCALE["reply_unknown"][DEFAULT_LANG])

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

def localized_bridge_info(event_key, lang, **kwargs):
    table = _LOCALE.get("bridge_info", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_whois(event_key, lang, **kwargs):
    table = _LOCALE.get("whois", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_help(event_key, lang, **kwargs):
    table = _LOCALE.get("help", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template

def localized_deadtopic(event_key, lang, **kwargs):
    table = _LOCALE.get("deadtopic", {}).get(event_key, {})
    template = table.get(lang, table.get(DEFAULT_LANG, event_key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template
