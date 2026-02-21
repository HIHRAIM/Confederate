import discord
from discord import app_commands
import db, message_relay
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, set_chat_lang,
    get_next_status_text, localized_bridge_join, localized_bridge_leave, localized_bot_joined, get_chat_lang
)
import time
import asyncio
import json

def replace_mentions(message: discord.Message, text: str) -> str:
    if not message.guild or not text:
        return text

    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")

    for user in message.mentions:
        text = text.replace(f"<@{user.id}>", f"@{user.display_name}")
        text = text.replace(f"<@!{user.id}>", f"@{user.display_name}")

    return text

class DiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        self.loop.create_task(self.deadchat_loop())
        self.loop.create_task(self.status_loop())

    async def deadchat_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = int(time.time())
            rows = db.cur.execute("SELECT * FROM dead_chats").fetchall()

            for r in rows:
                timeout = r["hours"] * 3600
                if now - r["last_message_ts"] >= timeout:
                    try:
                        guild_id, channel_id = r["chat_id"].split(":")
                        channel = self.get_channel(int(channel_id))
                        if channel:
                            await channel.send(f"<@&{r['role_id']}>")
                    except Exception:
                        pass

                    db.cur.execute(
                        "UPDATE dead_chats SET last_message_ts=? WHERE chat_id=?",
                        (now, r["chat_id"])
                    )
                    db.conn.commit()

            await asyncio.sleep(300)

    async def status_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                total_members = sum(g.member_count for g in self.guilds)

                discord_servers = len(self.guilds)
                telegram_chats = db.get_telegram_chat_count()
                total_servers = discord_servers + telegram_chats

                status_text = get_next_status_text(total_members, total_servers)

                await self.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=status_text
                    )
                )
            except Exception as e:
                print(f"Status update error: {e}")

            await asyncio.sleep(60)

bot = DiscordBot()

@bot.tree.command(name="atb")
async def atb(interaction: discord.Interaction, bridge_id: int):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    if db.chat_exists(chat_id):
        await interaction.response.send_message("Chat already attached to a bridge", ephemeral=True)
        return

    db.attach_chat("discord", chat_id, bridge_id)

    # send message to this channel in its language
    lang = get_chat_lang(chat_id) or "en"
    try:
        await interaction.channel.send(localized_bot_joined(lang))
    except Exception:
        pass

    await interaction.response.send_message(
        f"Chat attached to bridge {bridge_id}",
    )

    # notify other chats in the bridge
    channel_or_topic = interaction.channel.name or f"channel:{interaction.channel_id}"
    server_name = interaction.guild.name or f"server:{interaction.guild_id}"

    rows = db.get_bridge_chats(bridge_id)
    for c in rows:
        if c["platform"] == "discord" and c["chat_id"] == chat_id:
            continue

        target_lang = get_chat_lang(c["chat_id"]) or "en"
        notify = localized_bridge_join(channel_or_topic, server_name, target_lang)

        if c["platform"] == "discord":
            try:
                chan_id = int(c["chat_id"].split(":")[1])
                ch = bot.get_channel(chan_id)
                if ch:
                    await ch.send(notify)
            except Exception:
                pass
        elif c["platform"] == "telegram":
            try:
                from telegram_bot import bot as tg_bot
                chat_id_str, th = c["chat_id"].split(":")
                await tg_bot.send_message(
                    chat_id=int(chat_id_str),
                    message_thread_id=int(th) or None,
                    text=notify
                )
            except Exception:
                pass

@bot.tree.command(name="rfb")
async def rfb(interaction: discord.Interaction, target: str | None = None):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    # determine target
# --- replace target-detection block in rfb handler ---
    # determine target
    if not target:
        target_chat_id = chat_key
        target_platform = "discord"
    else:
        raw = target.strip()
        # support channel mention format <#1234567890>
        if raw.startswith("<#") and raw.endswith(">"):
            raw = raw[2:-1]

        if ":" in raw:
            # already in guild:channel form
            target_chat_id = raw
            target_platform = "discord"
        elif raw.isdigit():
            # single numeric id — assume channel in current guild
            target_chat_id = f"{interaction.guild_id}:{raw}"
            target_platform = "discord"
        else:
            # ambiguous string — require bot admin (leave as-is)
            target_chat_id = raw
            target_platform = None

    user_id = interaction.user.id
    if is_admin("discord", user_id):
        allowed = True
    else:
        if target_chat_id == chat_key and is_chat_admin("discord", chat_key, user_id):
            allowed = True
        elif target_platform == "discord" and is_chat_admin("discord", target_chat_id, user_id):
            allowed = True
        else:
            allowed = False

    if not allowed:
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (target_chat_id,)).fetchone()
    if not row:
        await interaction.response.send_message("Chat is not attached to any bridge", ephemeral=True)
        return

    bridge_id = row["bridge_id"]

    # build origin display names
    if target_chat_id == chat_key:
        channel_or_topic = interaction.channel.name or f"channel:{interaction.channel_id}"
        server_name = interaction.guild.name or f"server:{interaction.guild_id}"
    else:
        # best-effort fallback
        try:
            guild_id, ch_id = target_chat_id.split(":")
            ch = bot.get_channel(int(ch_id))
            g = bot.get_guild(int(guild_id))
            channel_or_topic = ch.name if ch else target_chat_id
            server_name = g.name if g else guild_id
        except Exception:
            channel_or_topic = target_chat_id
            server_name = target_chat_id

    # remove chat
    db.cur.execute("DELETE FROM chats WHERE chat_id=?", (target_chat_id,))
    db.conn.commit()

    # notify other chats
    rows = db.get_bridge_chats(bridge_id)
    for c in rows:
        target_lang = get_chat_lang(c["chat_id"]) or "en"
        notify = localized_bridge_leave(channel_or_topic, server_name, target_lang)

        if c["platform"] == "discord":
            try:
                chan_id = int(c["chat_id"].split(":")[1])
                ch = bot.get_channel(chan_id)
                if ch:
                    await ch.send(notify)
            except Exception:
                pass
        elif c["platform"] == "telegram":
            try:
                from telegram_bot import bot as tg_bot
                chat_id_str, th = c["chat_id"].split(":")
                await tg_bot.send_message(
                    chat_id=int(chat_id_str),
                    message_thread_id=int(th) or None,
                    text=notify
                )
            except Exception:
                pass

    await interaction.response.send_message("Chat removed from bridge", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    chat_id = f"{message.guild.id}:{message.channel.id}"

    row_news = db.cur.execute(
        "SELECT emojis FROM news_chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

    if row_news:
        emojis = json.loads(row_news["emojis"])
        for e in emojis:
            try:
                await message.add_reaction(e)
            except Exception:
                pass

    db.cur.execute(
        "UPDATE dead_chats SET last_message_ts=? WHERE chat_id=?",
        (int(time.time()), chat_id)
    )
    db.conn.commit()

    if message.author.bot:
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    reply_to_name = None
    if message.reference and message.reference.resolved:
        replied = message.reference.resolved
        
        relay_bot_ids = (1295454829883298023, 888314689824636998)

        if replied.author.id in relay_bot_ids:
            reply_to_name = extract_username_from_bot_message(replied.content)
        else:
            reply_to_name = replied.author.display_name

    content = replace_mentions(message, message.content or "")

    if message.stickers:
        texts = ["[Sticker]"]
    else:
        attachments = [a.url for a in message.attachments]
        if attachments:
            texts = [content + "\n" + attachments[0] if content else attachments[0]]
            for a in attachments[1:]:
                texts.append(a)
        else:
            texts = [content]

    async def send_to_chat(chat, text):
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if not channel:
                return None
            sent = await channel.send(text)
            return str(sent.id)

        if chat["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id_str, thread = chat["chat_id"].split(":")
            sent = await tg_bot.send_message(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text
            )
            return str(sent.message_id)

    for text in texts:
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=chat_id,
            origin_message_id=str(message.id),
            messenger_name="Discord",
            place_name=message.guild.name,
            sender_name=message.author.display_name,
            text=text,
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat
        )

def try_remove_bridge_rule(origin_platform, origin_chat_id, origin_message_id):
    row = db.cur.execute(
        """
        SELECT 1 FROM message_copies
        WHERE origin_platform=? AND origin_chat_id=? AND origin_message_id=?
        LIMIT 1
        """,
        (origin_platform, origin_chat_id, origin_message_id)
    ).fetchone()

    if row:
        return

    db.cur.execute(
        """
        DELETE FROM bridge_rules
        WHERE origin_platform=? AND origin_chat_id=? AND origin_message_id=?
        """,
        (origin_platform, origin_chat_id, origin_message_id)
    )
    db.conn.commit()

@bot.event
async def on_message_delete(message: discord.Message):
    row = db.cur.execute(
        """
        SELECT id FROM messages
        WHERE origin_platform='discord'
          AND origin_message_id=?
        """,
        (str(message.id),)
    ).fetchone()

    if not row:
        await handle_delete_of_copy("discord", str(message.id))
        return

    await delete_all_copies_and_origin(row["id"])

    try_remove_bridge_rule(
        "discord",
        f"{message.guild.id}:{message.channel.id}",
        str(message.id)
    )

@bot.tree.command(name="setadmin")
async def setadmin(interaction: discord.Interaction, user_id: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            "No permission to manage chat admins",
            ephemeral=True
        )
        return

    db.cur.execute(
        """
        INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id)
        VALUES (?,?,?)
        """,
        ("discord", chat_id, user_id)
    )
    db.conn.commit()

    await interaction.response.send_message(
        f"User `{user_id}` added as chat admin",
        ephemeral=True
    )


async def handle_delete_of_copy(platform, platform_message_id):
    row = db.cur.execute(
        """
        SELECT message_id FROM message_copies
        WHERE platform=? AND message_id_platform=?
        """,
        (platform, platform_message_id)
    ).fetchone()

    if row:
        await delete_all_copies_and_origin(row["message_id"])


async def delete_all_copies_and_origin(msg_id):
    msg = db.cur.execute(
        "SELECT * FROM messages WHERE id=?",
        (msg_id,)
    ).fetchone()
    if not msg:
        return

    copies = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=?",
        (msg_id,)
    ).fetchall()

    for c in copies:
        if c["platform"] == "discord":
            channel_id = int(c["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    m = await channel.fetch_message(int(c["message_id_platform"]))
                    await m.delete()
                except Exception:
                    pass

        elif c["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id, _ = c["chat_id"].split(":")
            try:
                await tg_bot.delete_message(
                    int(chat_id),
                    int(c["message_id_platform"])
                )
            except Exception:
                pass

    if msg["origin_platform"] == "discord":
        channel_id = int(msg["origin_chat_id"].split(":")[1])
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                m = await channel.fetch_message(int(msg["origin_message_id"]))
                await m.delete()
            except Exception:
                pass

    db.cur.execute("DELETE FROM message_copies WHERE message_id=?", (msg_id,))
    db.cur.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    db.conn.commit()

@bot.event
async def on_guild_remove(guild: discord.Guild):
    db.cur.execute(
        """
        DELETE FROM chat_admins
        WHERE platform='discord' AND chat_id LIKE ?
        """,
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        """
        DELETE FROM dead_chats
        WHERE chat_id LIKE ?
        """,
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        "DELETE FROM news_chats WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.conn.commit()

@bot.tree.command(name="deadchat")
async def deadchat(
    interaction: discord.Interaction,
    role_id: str,
    hours: int | None = None
):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            "No permission to manage deadchat here",
            ephemeral=True
        )
        return

    if role_id.lower() == "disable":
        db.cur.execute(
            "DELETE FROM dead_chats WHERE chat_id=?",
            (chat_id,)
        )
        db.conn.commit()
        await interaction.response.send_message(
            "Deadchat disabled for this channel",
            ephemeral=True
        )
        return

    if hours is None or hours <= 0:
        await interaction.response.send_message(
            "Specify duration in hours (>0)",
            ephemeral=True
        )
        return

    db.cur.execute(
        """
        INSERT OR REPLACE INTO dead_chats
        (chat_id, role_id, hours, last_message_ts)
        VALUES (?,?,?,?)
        """,
        (
            chat_id,
            role_id,
            hours,
            int(time.time())
        )
    )
    db.conn.commit()

    await interaction.response.send_message(
        f"Deadchat set: role <@&{role_id}>, {hours} hours",
        ephemeral=True
    )

async def deadchat_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = int(time.time())
        rows = db.cur.execute(
            "SELECT * FROM dead_chats"
        ).fetchall()

        for r in rows:
            timeout = r["hours"] * 3600
            if now - r["last_message_ts"] >= timeout:
                try:
                    guild_id, channel_id = r["chat_id"].split(":")
                    channel = bot.get_channel(int(channel_id))
                    if channel:
                        await channel.send(f"<@&{r['role_id']}>")
                except Exception:
                    pass

                db.cur.execute(
                    "UPDATE dead_chats SET last_message_ts=? WHERE chat_id=?",
                    (now, r["chat_id"])
                )
                db.conn.commit()

        await asyncio.sleep(300)

@bot.tree.command(name="newschat")
async def newschat(
    interaction: discord.Interaction,
    action: str,
    emoji: str | None = None
):
    """
    /newschat add <emoji>  - add reaction (unicode or <:name:id>)
    /newschat disable     - disable for this channel
    """
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            "No permission to manage newschat here",
            ephemeral=True
        )
        return

    if action.lower() == "disable":
        db.cur.execute(
            "DELETE FROM news_chats WHERE chat_id=?",
            (chat_id,)
        )
        db.conn.commit()

        await interaction.response.send_message(
            "Newschat disabled for this channel",
            ephemeral=True
        )
        return

    if action.lower() == "add":
        if emoji is None or emoji.strip() == "":
            await interaction.response.send_message(
                "Specify emoji. Examples:\n"
                "• Unicode: 😀\n"
                "• Custom: `<:Name:1234567890>`\n"
                "• Animated: `<a:Name:1234567890>`",
                ephemeral=True
            )
            return

        emoji_str = emoji.strip()

        try:
            test_msg = await interaction.channel.send("\u200b")
            await test_msg.add_reaction(emoji_str)
            await test_msg.delete()
        except Exception:
            await interaction.response.send_message(
                "That emoji cannot be used as a reaction. Try copying/pasting emoji or using `<:name:id>` format.",
                ephemeral=True
            )
            return

        row = db.cur.execute(
            "SELECT emojis FROM news_chats WHERE chat_id=?",
            (chat_id,)
        ).fetchone()

        emojis = json.loads(row["emojis"]) if row and row["emojis"] else []

        if emoji_str not in emojis:
            emojis.append(emoji_str)

        db.cur.execute(
            "INSERT OR REPLACE INTO news_chats (chat_id, emojis) VALUES (?,?)",
            (chat_id, json.dumps(emojis))
        )
        db.conn.commit()

        await interaction.response.send_message(
            f"Emoji `{emoji_str}` added for this channel",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Usage: /newschat add <emoji> | /newschat disable",
        ephemeral=True
    )

@bot.tree.command(name="remindrules")
async def remindrules(
    interaction: discord.Interaction,
    hours: int,
    messages: int | None = None
):
    ref = None
    if interaction.message and interaction.message.reference:
        ref = interaction.message.reference.resolved

    if not ref:
        await interaction.response.send_message(
            "Command must be a reply to a message with rules",
            ephemeral=True
        )
        return

    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    ref = interaction.message.reference.resolved
    if not ref:
        await interaction.response.send_message("Could not resolve referenced message", ephemeral=True)
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message("Chat is not attached to any bridge", ephemeral=True)
        return

    bridge_id = row["bridge_id"]

    db.cur.execute(
        """
        INSERT OR REPLACE INTO bridge_rules
        (bridge_id, content, format, origin_platform, origin_chat_id,
         origin_message_id, hours, messages, last_post_ts, message_counter)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            bridge_id,
            ref.content,
            "discord",
            "discord",
            chat_id,
            str(ref.id),
            hours,
            messages,
            int(time.time()),
            0
        )
    )
    db.conn.commit()

    await interaction.response.send_message(
        "Rules saved and will be posted automatically",
        ephemeral=True
    )

@bot.tree.command(name="lang")
async def lang_command(interaction: discord.Interaction, code: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_key, interaction.user.id)
    ):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    code = code.strip().lower()
    try:
        set_chat_lang(chat_key, code)
    except Exception:
        await interaction.response.send_message("Unsupported language. Supported: ru, uk, pl, en, es, pt", ephemeral=True)
        return

    await interaction.response.send_message(f"Language for this channel set: {code}", ephemeral=True)

@bot.tree.command(name="list_chats")
async def list_chats(interaction: discord.Interaction):
    """
    Показывает администратору (ADMINS discord) список:
     - Discord: все сервера (guild.name и guild.id),
     - Telegram: все найденные префиксы chat_id (group_id) и попытка получить их названия через Telegram API.
    Доступна только администраторам из config.ADMINS["discord"].
    """
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    lines = []
    # Discord guilds
    lines.append("**Discord — серверы, где бот состоит:**")
    for g in bot.guilds:
        lines.append(f"- {g.name} — id: {g.id}")

    # Telegram groups: собираем уникальные префиксы chat_id из таблицы chats
    rows = db.cur.execute("SELECT chat_id FROM chats WHERE platform='telegram'").fetchall()
    prefixes = {}
    for r in rows:
        prefix = r["chat_id"].split(":", 1)[0]
        prefixes[prefix] = True

    if prefixes:
        lines.append("\n**Telegram — чаты/группы (id):**")
        try:
            from telegram_bot import bot as tg_bot
            for pid in prefixes.keys():
                try:
                    chat = await tg_bot.get_chat(int(pid))
                    title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(pid)
                except Exception:
                    title = str(pid)
                lines.append(f"- {title} — id: {pid}")
        except Exception:
            # если получить названия не получилось — просто выводим id
            for pid in prefixes.keys():
                lines.append(f"- id: {pid}")
    else:
        lines.append("\nНет Telegram чатов в БД.")

    msg = "\n".join(lines)

    # если сообщение длинное — отправляем как файл, иначе обычный ответ (ephemeral чтобы видели только админы)
    if len(msg) > 1900:
        import io
        bio = io.BytesIO(msg.encode("utf-8"))
        bio.seek(0)
        await interaction.response.send_message("Список большой — загружаю файл.", ephemeral=True)
        await interaction.followup.send(file=discord.File(bio, filename="chat_list.txt"))
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="force_leave")
async def force_leave(interaction: discord.Interaction, platform: str, target_id: str):
    """
    Принудительно вывести бота из указанного сервера/чата.
    Примеры:
      /force_leave discord 123456789012345678
      /force_leave telegram -1001234567890
    Доступно только bot-ADMINS (config.ADMINS["discord"]).
    """
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    platform = platform.strip().lower()
    target_id = target_id.strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await interaction.response.send_message("Invalid guild id", ephemeral=True)
            return

        guild = bot.get_guild(gid)
        if not guild:
            await interaction.response.send_message("Bot is not a member of that guild", ephemeral=True)
            return

        try:
            # leave guild
            await guild.leave()
        except Exception as e:
            await interaction.response.send_message(f"Failed to leave guild: {e}", ephemeral=True)
            return

        # cleanup DB rows related to that guild
        db.cur.execute("DELETE FROM chat_admins WHERE platform='discord' AND chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.conn.commit()

        await interaction.response.send_message(f"Left guild {gid} and cleaned DB entries", ephemeral=True)
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await interaction.response.send_message("Invalid telegram chat id", ephemeral=True)
            return

        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.leave_chat(tid)
        except Exception as e:
            await interaction.response.send_message(f"Failed to leave telegram chat (maybe bot isn't in it or lacks rights): {e}", ephemeral=True)
            # всё равно попытаться почистить базу — в случае, если чат есть в БД
        # cleanup DB
        db.cur.execute("DELETE FROM chat_admins WHERE platform='telegram' AND chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.conn.commit()

        await interaction.response.send_message(f"Left Telegram chat {tid} (or cleaned DB).", ephemeral=True)
        return

    await interaction.response.send_message("Unsupported platform. Use 'discord' or 'telegram'.", ephemeral=True)

async def status_loop():
    """Меняет статус бота раз в минуту, чередуя языки."""
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        try:
            total_members = sum(g.member_count for g in bot.guilds)

            discord_servers = len(bot.guilds)
            telegram_chats = db.get_telegram_chat_count()
            total_servers = discord_servers + telegram_chats

            status_text = get_next_status_text(total_members, total_servers)

            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=status_text
                )
            )
        except Exception as e:
            print(f"Status update error: {e}")
        
        await asyncio.sleep(60)
