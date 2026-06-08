import discord
from discord import app_commands
from discord.utils import get
import db, message_relay
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, set_chat_lang,
    get_next_status_text, get_chat_lang,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    localized_sticker, localized_discord_system_event, localized_whois,
    localized_bridge_info, localized_deadtopic, localized_help
)
import time
import asyncio
import datetime
import json
from message_relay import discord_to_telegram_html, escape_html

async def resolve_discord_user(guild: discord.Guild, identifier: str):
    identifier = identifier.strip()
    if identifier.startswith("<@") and identifier.endswith(">"):
        nums = ''.join(ch for ch in identifier if ch.isdigit())
        if nums:
            return int(nums)
    if identifier.isdigit():
        return int(identifier)
    if "#" in identifier:
        member = get(guild.members, name=identifier.split("#",1)[0], discriminator=identifier.split("#",1)[1])
        if member:
            return member.id
    member = get(guild.members, name=identifier)
    if member:
        return member.id
    try:
        async for m in guild.fetch_members(limit=1000):
            if m.name == identifier or m.display_name == identifier:
                return m.id
    except Exception:
        pass
    return None

def replace_mentions(message: discord.Message, text: str) -> str:
    if not message.guild or not text:
        return text

    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")

    for user in message.mentions:
        text = text.replace(f"<@{user.id}>", f"@{user.display_name}")
        text = text.replace(f"<@!{user.id}>", f"@{user.display_name}")

    return text

def _discord_embed_texts(message: discord.Message):
    texts = []
    for e in getattr(message, "embeds", []) or []:
        parts = []

        author = getattr(e, "author", None)
        if author:
            author_name = getattr(author, "name", None)
            author_url = getattr(author, "url", None)
            if author_name:
                parts.append(f"[{author_name}]({author_url})" if author_url else author_name)

        title = getattr(e, "title", None)
        url = getattr(e, "url", None)
        if title:
            parts.append(f"[{title}]({url})" if url else f"**{title}**")
        elif url:
            parts.append(url)

        description = getattr(e, "description", None)
        if description:
            parts.append(str(description))

        for field in getattr(e, "fields", []) or []:
            fname = getattr(field, "name", None)
            fvalue = getattr(field, "value", None)
            if fname and fvalue:
                parts.append(f"**{fname}**\n{fvalue}")
            elif fvalue:
                parts.append(str(fvalue))

        image = getattr(e, "image", None)
        if image:
            img_url = getattr(image, "url", None)
            if img_url:
                parts.append(img_url)

        thumbnail = getattr(e, "thumbnail", None)
        if thumbnail:
            thumb_url = getattr(thumbnail, "url", None)
            if thumb_url:
                parts.append(thumb_url)

        footer = getattr(e, "footer", None)
        if footer:
            footer_text = getattr(footer, "text", None)
            if footer_text:
                parts.append(f"_{footer_text}_")

        if parts:
            texts.append("\n".join(parts))
    return texts

def _discord_system_event_key(message: discord.Message):
    mt = getattr(message, "type", None)
    mapping = {
        discord.MessageType.premium_guild_subscription: "boosted_server",
        discord.MessageType.premium_guild_tier_1: "boosted_server",
        discord.MessageType.premium_guild_tier_2: "boosted_server",
        discord.MessageType.premium_guild_tier_3: "boosted_server",
        discord.MessageType.thread_created: "created_thread",
        discord.MessageType.pins_add: "pinned_message",
        discord.MessageType.new_member: "joined_server",
    }
    return mapping.get(mt)

def extract_discord_forward_payload(message: discord.Message):
    forward_type = None
    forward_name = None
    forward_text = ""

    snapshots = getattr(message, "message_snapshots", None) or []
    if snapshots:
        snap = snapshots[0]
        body = (getattr(snap, "content", "") or "").strip()
        snap_attachments = []
        for a in getattr(snap, "attachments", []) or []:
            url = getattr(a, "url", None)
            if url:
                snap_attachments.append(url)
        if snap_attachments:
            body = "\n".join([body] + snap_attachments) if body else "\n".join(snap_attachments)

        snap_channel = getattr(snap, "channel", None)
        snap_author = getattr(snap, "author", None)
        if snap_channel and getattr(snap_channel, "name", None):
            forward_type = "chat"
            forward_name = snap_channel.name
        elif snap_author:
            forward_type = "user"
            forward_name = getattr(snap_author, "display_name", None) or getattr(snap_author, "name", None)
        else:
            forward_type = "unknown"

        return forward_type, forward_name, body

    if getattr(message, "type", None) == discord.MessageType.reply:
        return None, None, ""

    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None)
    if resolved and isinstance(resolved, discord.Message):
        body = replace_mentions(resolved, resolved.content or "").strip()
        ref_attachments = [a.url for a in getattr(resolved, "attachments", []) if getattr(a, "url", None)]
        if ref_attachments:
            body = "\n".join([body] + ref_attachments) if body else "\n".join(ref_attachments)
        if resolved.channel and getattr(resolved.channel, "name", None):
            forward_type = "chat"
            forward_name = resolved.channel.name
        elif resolved.author:
            forward_type = "user"
            forward_name = resolved.author.display_name or str(resolved.author)
        else:
            forward_type = "unknown"
        return forward_type, forward_name, body

    if ref and not resolved:
        return "unknown", None, ""

    return None, None, ""

async def _relay_verified_discord_message(message: discord.Message, bridge_id, system_event_key=None, is_bot_sender=False):
    reply_to_msg_db_id = None
    forward_type = None
    forward_name = None
    forward_text = ""

    if message.type == discord.MessageType.reply and message.reference:
        ref_msg_id = getattr(message.reference, "message_id", None)
        replied = message.reference.resolved
        if not replied and ref_msg_id:
            try:
                replied = await message.channel.fetch_message(ref_msg_id)
            except Exception:
                replied = None

        origin_chat_id = f"{message.guild.id}:{message.channel.id}"

        if replied and getattr(replied, "author", None):
            if replied.author.bot:
                copy_row = db.cur.execute(
                    "SELECT message_id FROM message_copies WHERE platform='discord' AND message_id_platform=?",
                    (str(replied.id),)
                ).fetchone()
                reply_to_msg_db_id = copy_row["message_id"] if copy_row else -1
            else:
                msg_row = db.cur.execute(
                    "SELECT id FROM messages WHERE origin_platform='discord' AND origin_chat_id=? AND origin_message_id=?",
                    (origin_chat_id, str(replied.id))
                ).fetchone()
                reply_to_msg_db_id = msg_row["id"] if msg_row else -1
        elif ref_msg_id:
            copy_row = db.cur.execute(
                "SELECT message_id FROM message_copies WHERE platform='discord' AND message_id_platform=?",
                (str(ref_msg_id),)
            ).fetchone()
            if copy_row:
                reply_to_msg_db_id = copy_row["message_id"]
            else:
                msg_row = db.cur.execute(
                    "SELECT id FROM messages WHERE origin_platform='discord' AND origin_chat_id=? AND origin_message_id=?",
                    (origin_chat_id, str(ref_msg_id))
                ).fetchone()
                reply_to_msg_db_id = msg_row["id"] if msg_row else -1

    forward_type, forward_name, forward_text = extract_discord_forward_payload(message)
    content = replace_mentions(message, message.content or "")

    if message.stickers:
        texts = ["__DC_STICKER__"]
    else:
        attachments = [a.url for a in message.attachments]
        if attachments:
            texts = [content + "\n" + attachments[0] if content else attachments[0]]
            for a in attachments[1:]:
                texts.append(a)
        else:
            texts = [content]

    embed_texts = _discord_embed_texts(message)
    if embed_texts:
        embed_block = "\n\n".join(embed_texts)
        if any((t or "").strip() for t in texts):
            texts[0] = (texts[0] or "").rstrip() + "\n\n" + embed_block
        else:
            texts = [embed_block]

    if forward_type and not any((t or "").strip() for t in texts):
        texts = [forward_text or ""]

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_to_platform_message_id=None):
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    return None
            body = body_discord
            if reply_line:
                body = f"{reply_line}\n{body}"
            send_kwargs = {}
            if reply_to_platform_message_id:
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=int(reply_to_platform_message_id),
                    channel_id=channel_id,
                    fail_if_not_exists=False,
                )
                send_kwargs["mention_author"] = False
            try:
                sent = await channel.send(f"{header}\n{body}".strip(), **send_kwargs)
                return str(sent.id)
            except Exception:
                return None

        if chat["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id_str, thread = chat["chat_id"].split(":")
            body_html = body_telegram_html or escape_html(body_plain)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            text_html = f"{escape_html(header)}\n{body_html}".strip()
            send_kwargs = dict(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text_html,
                parse_mode="HTML",
            )
            if reply_to_platform_message_id:
                send_kwargs["reply_to_message_id"] = int(reply_to_platform_message_id)
            try:
                sent = await tg_bot.send_message(**send_kwargs)
            except Exception:
                if reply_to_platform_message_id:
                    send_kwargs.pop("reply_to_message_id", None)
                    sent = await tg_bot.send_message(**send_kwargs)
                else:
                    raise
            return str(sent.message_id)

    if system_event_key:
        origin_lang = get_chat_lang(f"{message.guild.id}:{message.channel.id}")
        event_text = localized_discord_system_event(
            message.author.display_name or str(message.author),
            system_event_key,
            origin_lang,
        )
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=f"{message.guild.id}:{message.channel.id}",
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=message.guild.name or message.channel.name,
            sender_name=message.author.display_name or str(message.author),
            text=event_text,
            discord_text=event_text,
            telegram_html=discord_to_telegram_html(event_text),
            reply_to_msg_db_id=None,
            send_to_chat_func=send_to_chat,
        )
        return

    for text in texts:
        target_lang = get_chat_lang(f"{message.guild.id}:{message.channel.id}")
        localized_text = text.replace("__DC_STICKER__", localized_sticker(target_lang))
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=f"{message.guild.id}:{message.channel.id}",
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=message.guild.name or message.channel.name,
            sender_name=message.author.display_name or str(message.author),
            text=localized_text,
            discord_text=localized_text,
            telegram_html=discord_to_telegram_html(localized_text),
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            forward_type=forward_type,
            forward_name=forward_name,
            is_bot_sender=is_bot_sender,
        )

async def _send_db_backup_discord(client):
    import io
    from config import BACKUP_CHATS
    try:
        with open("bridge.db", "rb") as f:
            data = f.read()
    except Exception:
        return
    for channel_id in BACKUP_CHATS.get("discord", set()):
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(channel_id)
                except Exception:
                    continue
            if ch:
                await ch.send(file=discord.File(io.BytesIO(data), filename="bridge.db"))
        except Exception:
            pass

async def _send_db_backup_telegram():
    import io
    from config import BACKUP_CHATS
    from telegram_bot import bot as tg_bot
    try:
        with open("bridge.db", "rb") as f:
            data = f.read()
    except Exception:
        return
    for chat_entry in BACKUP_CHATS.get("telegram", set()):
        try:
            chat_id_str, thread_str = chat_entry.split(":")
            from aiogram.types import BufferedInputFile
            doc = BufferedInputFile(data, filename="bridge.db")
            await tg_bot.send_document(
                chat_id=int(chat_id_str),
                document=doc,
                message_thread_id=int(thread_str) or None,
            )
        except Exception:
            pass

async def _relay_pending_discord_first_message(pend_row):
    try:
        chat_key = pend_row["chat_key"]
        first_message_id = pend_row["first_message_id"]
    except Exception:
        return
    if not chat_key or not first_message_id:
        return
    try:
        _, channel_id = chat_key.split(":")
        channel = bot.get_channel(int(channel_id))
        if not channel:
            channel = await bot.fetch_channel(int(channel_id))
        first_message = await channel.fetch_message(int(first_message_id))
    except Exception:
        return
    if not first_message or first_message.author.bot:
        return
    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        return
    await _relay_verified_discord_message(first_message, row["bridge_id"], _discord_system_event_key(first_message))
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
        self.loop.create_task(self.bridge_rules_loop())
        self.loop.create_task(self.deadtopic_loop())
        self.loop.create_task(self.backup_loop())

    async def backup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(12 * 3600)
            await _send_db_backup_discord(self)
            await _send_db_backup_telegram()

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
        from telegram_bot import bot as tg_bot
        while not self.is_closed():
            try:
                discord_members = sum((g.member_count or 0) for g in self.guilds)
                telegram_members = 0
                for gid in db.get_telegram_group_ids():
                    try:
                        members_count = await tg_bot.get_chat_member_count(int(gid))
                        telegram_members += int(members_count or 0)
                    except Exception:
                        continue
                total_members = discord_members + telegram_members
                discord_servers = len(self.guilds)
                telegram_groups = db.get_telegram_group_count()
                total_servers = discord_servers + telegram_groups

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

    async def bridge_rules_loop(self):
        """Periodically posts bridge rules to ALL chats in the bridge (Discord + Telegram)."""
        await self.wait_until_ready()
        from telegram_bot import bot as tg_bot

        while not self.is_closed():
            try:
                now = int(time.time())
                rows = db.cur.execute("SELECT * FROM bridge_rules").fetchall()

                for r in rows:
                    interval_minutes = r["hours"]
                    if not interval_minutes or interval_minutes <= 0:
                        continue

                    interval_seconds = interval_minutes * 60
                    elapsed_since_post = now - (r["last_post_ts"] or 0)

                    time_due = elapsed_since_post >= interval_seconds

                    msg_count = r["messages"]
                    count_due = (msg_count is None) or (r["message_counter"] or 0) >= msg_count

                    if not (time_due and count_due):
                        continue

                    content = r["content"] or ""
                    if not content:
                        continue

                    bridge_id = r["bridge_id"]
                    chats = db.get_bridge_chats(bridge_id)

                    for chat in chats:
                        try:
                            if chat["platform"] == "discord":
                                channel_id = int(chat["chat_id"].split(":")[1])
                                channel = self.get_channel(channel_id)
                                if not channel:
                                    try:
                                        channel = await self.fetch_channel(channel_id)
                                    except Exception:
                                        continue
                                await channel.send(content)

                            elif chat["platform"] == "telegram":
                                chat_id_str, thread = chat["chat_id"].split(":")
                                await tg_bot.send_message(
                                    chat_id=int(chat_id_str),
                                    message_thread_id=int(thread) or None,
                                    text=content,
                                )
                        except Exception as e:
                            print(f"bridge_rules_loop: failed to send to {chat['chat_id']}: {e}")

                    db.cur.execute(
                        "UPDATE bridge_rules SET last_post_ts=?, message_counter=0 WHERE bridge_id=?",
                        (now, bridge_id)
                    )
                    db.conn.commit()

            except Exception as e:
                print(f"bridge_rules_loop error: {e}")

            await asyncio.sleep(60)

    async def deadtopic_loop(self):
        """
        Ровно в 00:00 UTC проверяет deadtopic_chats.
        Если с последнего сообщения (или последней отправки бота) прошло >= 6 дней —
        отправляет фантомное сообщение и сразу удаляет его.
        Засыпает до следующей полуночи, чтобы перезапуск бота не влиял на расписание.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            next_midnight = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_seconds = (next_midnight - now_utc).total_seconds()
            await asyncio.sleep(sleep_seconds)

            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                now_ts = int(time.time())
                today_str = now_utc.strftime("%Y-%m-%d")
                rows = db.cur.execute("SELECT * FROM deadtopic_chats").fetchall()

                for r in rows:
                    try:
                        chat_id = r["chat_id"]

                        bot_last_ts = r["bot_last_sent_ts"] or 0
                        if bot_last_ts > 0:
                            last_sent_day = datetime.datetime.fromtimestamp(
                                bot_last_ts, tz=datetime.timezone.utc
                            ).strftime("%Y-%m-%d")
                            if last_sent_day == today_str:
                                continue

                        last_msg_ts = r["last_message_ts"] or 0
                        ref_ts = max(last_msg_ts, bot_last_ts)

                        if now_ts - ref_ts < 6 * 86400:
                            continue

                        _, channel_id_str = chat_id.split(":")
                        channel = self.get_channel(int(channel_id_str))
                        if not channel:
                            try:
                                channel = await self.fetch_channel(int(channel_id_str))
                            except Exception:
                                continue

                        lang = get_chat_lang(chat_id) or "en"
                        phantom_text = localized_deadtopic("phantom_message", lang)

                        sent = await channel.send(phantom_text)
                        await asyncio.sleep(1)
                        try:
                            await sent.delete()
                        except Exception:
                            pass

                        db.cur.execute(
                            "UPDATE deadtopic_chats SET bot_last_sent_ts=? WHERE chat_id=?",
                            (now_ts, chat_id)
                        )
                        db.conn.commit()

                    except Exception as e:
                        print(f"deadtopic_loop: error for {r['chat_id']}: {e}")

            except Exception as e:
                print(f"deadtopic_loop error: {e}")

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

    lang = get_chat_lang(chat_id) or "en"
    try:
        await interaction.channel.send(localized_bot_joined(lang))
    except Exception:
        pass

    await interaction.response.send_message(
        f"Chat attached to bridge {bridge_id}",
    )

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
    if not target:
        target_chat_id = chat_key
        target_platform = "discord"
    else:
        raw = target.strip()
        if raw.startswith("<#") and raw.endswith(">"):
            raw = raw[2:-1]

        if ":" in raw:
            target_chat_id = raw
            target_platform = "discord"
        elif raw.isdigit():
            target_chat_id = f"{interaction.guild_id}:{raw}"
            target_platform = "discord"
            if not db.cur.execute("SELECT 1 FROM chats WHERE chat_id=?", (target_chat_id,)).fetchone():
                row_any = db.cur.execute(
                    "SELECT chat_id FROM chats WHERE platform='discord' AND chat_id LIKE ?",
                    (f"%:{raw}",)
                ).fetchone()
                if row_any:
                    target_chat_id = row_any["chat_id"]
        else:
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

    if target_chat_id == chat_key:
        channel_or_topic = interaction.channel.name or f"channel:{interaction.channel_id}"
        server_name = interaction.guild.name or f"server:{interaction.guild_id}"
    else:
        try:
            guild_id, ch_id = target_chat_id.split(":")
            ch = bot.get_channel(int(ch_id))
            g = bot.get_guild(int(guild_id))
            channel_or_topic = ch.name if ch else target_chat_id
            server_name = g.name if g else guild_id
        except Exception:
            channel_or_topic = target_chat_id
            server_name = target_chat_id

    db.cur.execute("DELETE FROM chats WHERE chat_id=?", (target_chat_id,))
    db.conn.commit()

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
        if message.author == bot.user:
            return
        if db.get_allow_bots(chat_id) and not db.is_relay_copy("discord", chat_id, str(message.id)):
            row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if row:
                await _relay_verified_discord_message(message, row["bridge_id"], is_bot_sender=True)
        return

    db.cur.execute(
        "UPDATE deadtopic_chats SET last_message_ts=? WHERE chat_id=?",
        (int(time.time()), chat_id)
    )
    db.conn.commit()

    system_event_key = _discord_system_event_key(message)

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    db.cur.execute(
        "UPDATE bridge_rules SET message_counter = COALESCE(message_counter, 0) + 1 WHERE bridge_id=?",
        (bridge_id,)
    )
    db.conn.commit()

    prefix = str(message.guild.id)
    user_id_str = str(message.author.id)

    if db.is_shadow_banned("discord", user_id_str):
        try:
            await message.delete()
        except Exception:
            pass
        return

    if system_event_key and not db.is_user_verified("discord", user_id_str, prefix):
        await _relay_verified_discord_message(message, bridge_id, system_event_key)
        return

    if not db.is_user_verified("discord", user_id_str, prefix):
        pend = db.get_pending_consent("discord", prefix, user_id_str)
        if pend:
            try:
                await message.delete()
            except Exception:
                pass
            return
        else:
            lang = get_chat_lang(chat_id) or "en"
            from discord import ui, ButtonStyle

            class _VerifyView(ui.View):
                def __init__(self, prefix, user_id):
                    super().__init__(timeout=None)
                    self.prefix = str(prefix)
                    self.user_id = str(user_id)

                @ui.button(label=localized_consent_button(lang), style=ButtonStyle.primary)
                async def accept(self, interaction: discord.Interaction, button: ui.Button):
                    if str(interaction.user.id) != str(self.user_id):
                        await interaction.response.send_message("This button is not for you", ephemeral=True)
                        return
                    db.add_verified_user("discord", self.user_id, self.prefix, days_valid=365)
                    all_pendings = db.get_all_pending_consents_for_user("discord", self.user_id)
                    for p in all_pendings:
                        db.remove_pending_consent("discord", p["prefix"], self.user_id)
                        p_bot_msg_id = p["bot_message_id"]
                        if p_bot_msg_id:
                            try:
                                p_guild_id, p_channel_id = p["chat_key"].split(":")
                                p_ch = bot.get_channel(int(p_channel_id))
                                if p_ch:
                                    try:
                                        p_msg = await p_ch.fetch_message(int(p_bot_msg_id))
                                        await p_msg.delete()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass
                    await interaction.response.send_message("Thanks — verified", ephemeral=True)
                    for p in all_pendings:
                        await _relay_pending_discord_first_message(p)

            try:
                mention = f"<@{message.author.id}>"
                consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
                sent = await message.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
                bot_msg_id = str(sent.id)
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    bot_msg_id,
                    chat_key,
                    first_message_id=str(message.id)
                )
            except Exception:
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    "",
                    chat_key,
                    first_message_id=str(message.id)
                )
            return

    await _relay_verified_discord_message(message, bridge_id, system_event_key)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    await process_discord_message_edit(
        guild=after.guild,
        channel=after.channel,
        message_id=after.id,
        author_display_name=after.author.display_name or str(after.author),
        text=replace_mentions(after, after.content or ""),
    )

async def process_discord_message_edit(*, guild, channel, message_id, author_display_name, text):
    if not guild or not channel:
        return

    row = db.cur.execute(
        """
        SELECT id FROM messages
        WHERE origin_platform='discord' AND origin_chat_id=? AND origin_message_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (f"{guild.id}:{channel.id}", str(message_id))
    ).fetchone()
    if not row:
        return

    header = f"[Discord | {guild.name or channel.name}] {author_display_name}:"
    text_html = discord_to_telegram_html(text)

    copies = db.cur.execute("SELECT * FROM message_copies WHERE message_id=?", (row["id"],)).fetchall()
    for c in copies:
        try:
            if c["platform"] == "discord":
                channel_id = int(c["chat_id"].split(":")[1])
                ch = bot.get_channel(channel_id)
                if not ch:
                    try:
                        ch = await bot.fetch_channel(channel_id)
                    except Exception:
                        continue
                m = await ch.fetch_message(int(c["message_id_platform"]))
                await m.edit(content=f"{header}\n{text}".strip())
            elif c["platform"] == "telegram":
                from telegram_bot import bot as tg_bot
                chat_id, _ = c["chat_id"].split(":")
                await tg_bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(c["message_id_platform"]),
                    text=f"{escape_html(header)}\n{text_html}",
                    parse_mode="HTML"
                )
        except Exception:
            pass

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    data = payload.data or {}
    if str(data.get("author", {}).get("bot", "")).lower() == "true":
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            channel = None

    if not guild and channel and getattr(channel, "guild", None):
        guild = channel.guild

    if not guild or not channel:
        return

    author_display_name = None
    content = data.get("content")
    try:
        msg = await channel.fetch_message(payload.message_id)
        if msg.author.bot:
            return
        author_display_name = msg.author.display_name or str(msg.author)
        content = msg.content
        content = replace_mentions(msg, content or "")
    except Exception:
        pass

    if author_display_name is None:
        author_data = data.get("author") or {}
        author_display_name = author_data.get("global_name") or author_data.get("username") or "Unknown"
        content = content or ""

    await process_discord_message_edit(
        guild=guild,
        channel=channel,
        message_id=payload.message_id,
        author_display_name=author_display_name,
        text=content,
    )

def try_remove_bridge_rule(origin_platform, origin_chat_id, origin_message_id):
    # If the deleted message is a relay copy (not the original rule source), skip
    row = db.cur.execute(
        """
        SELECT 1 FROM message_copies
        WHERE platform=? AND chat_id=? AND message_id_platform=?
        LIMIT 1
        """,
        (origin_platform, origin_chat_id, str(origin_message_id))
    ).fetchone()

    if row:
        return

    db.cur.execute(
        """
        DELETE FROM bridge_rules
        WHERE origin_platform=? AND origin_chat_id=? AND origin_message_id=?
        """,
        (origin_platform, origin_chat_id, str(origin_message_id))
    )
    db.conn.commit()

@bot.event
async def on_message_delete(message: discord.Message):
    await process_discord_message_delete(
        guild_id=message.guild.id if message.guild else None,
        channel_id=message.channel.id if message.channel else None,
        message_id=message.id,
    )

async def process_discord_message_delete(*, guild_id, channel_id, message_id):
    row = db.cur.execute(
        """
        SELECT id FROM messages
        WHERE origin_platform='discord'
          AND origin_message_id=?
        """,
        (str(message_id),)
    ).fetchone()

    if not row:
        await handle_delete_of_copy("discord", str(message_id))
        return

    await delete_all_copies_and_origin(row["id"])

    try_remove_bridge_rule(
        "discord",
        f"{guild_id}:{channel_id}",
        str(message_id)
    )

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await process_discord_message_delete(
        guild_id=payload.guild_id,
        channel_id=payload.channel_id,
        message_id=payload.message_id,
    )

@bot.tree.command(name="setadmin")
async def setadmin(interaction: discord.Interaction, user: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message("No permission to manage chat admins", ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message("Could not resolve user", ephemeral=True)
            return
    else:
        uid = int(user)

    db.cur.execute(
        """
        INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id)
        VALUES (?,?,?)
        """,
        ("discord", chat_id, str(uid))
    )
    db.conn.commit()

    await interaction.response.send_message(f"User `{uid}` added as chat admin", ephemeral=True)

@bot.tree.command(name="remadmin")
async def remadmin(interaction: discord.Interaction, user: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission to manage chat admins", ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message("Could not resolve user", ephemeral=True)
            return
    else:
        uid = int(user)

    db.cur.execute(
        "DELETE FROM chat_admins WHERE platform=? AND chat_id=? AND user_id=?",
        ("discord", chat_id, str(uid))
    )
    db.conn.commit()

    await interaction.response.send_message(f"User `{uid}` removed from chat admins", ephemeral=True)

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
            if not channel:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
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
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None
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

    db.cur.execute(
        "DELETE FROM deadtopic_chats WHERE chat_id LIKE ?",
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

@bot.tree.command(name="deadtopic")
async def deadtopic(
    interaction: discord.Interaction,
    action: str,
):
    """
    /deadtopic enable  — включить авто-сохранение темы (phantom message каждые 6 дней).
    /deadtopic disable — выключить.
    Доступно только Bridge Admins и Bot Admins.
    """
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    user_id = interaction.user.id

    allowed = is_admin("discord", user_id)
    if not allowed:
        row = db.cur.execute(
            "SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if row:
            bridge_admins = db.get_bridge_admins(row["bridge_id"])
            if str(user_id) in bridge_admins:
                allowed = True

    if not allowed:
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    lang = get_chat_lang(chat_id) or "en"
    action = action.strip().lower()

    if action == "disable":
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id=?", (chat_id,))
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("disabled", lang), ephemeral=True
        )
        return

    if action == "enable":
        now_ts = int(time.time())
        db.cur.execute(
            """
            INSERT INTO deadtopic_chats (chat_id, last_message_ts, bot_last_sent_ts)
            VALUES (?, ?, NULL)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_message_ts=excluded.last_message_ts,
                bot_last_sent_ts=NULL
            """,
            (chat_id, now_ts)
        )
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("enabled", lang), ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Usage: /deadtopic enable | /deadtopic disable",
        ephemeral=True
    )

@bot.tree.command(name="remindrules")
async def remindrules(
    interaction: discord.Interaction,
    hours_or_disable: str,
    messages: int | None = None,
    message_id: str | None = None,
    text: str | None = None,
):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message("Chat is not attached to any bridge", ephemeral=True)
        return

    bridge_id = row["bridge_id"]

    if hours_or_disable.strip().lower() == "disable":
        db.cur.execute("DELETE FROM bridge_rules WHERE bridge_id=?", (bridge_id,))
        db.conn.commit()
        await interaction.response.send_message("Rules reminder disabled for this bridge", ephemeral=True)
        return

    raw = hours_or_disable.strip().lower()
    try:
        if raw.endswith("h"):
            interval_minutes = int(raw[:-1]) * 60
        elif raw.endswith("m"):
            interval_minutes = int(raw[:-1])
        else:
            interval_minutes = int(raw) * 60
        if interval_minutes <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "Usage: /remindrules <5h|30m|disable> [messages] [message_id] [text]\n"
            "Examples: `2h` — every 2 hours, `30m` — every 30 minutes",
            ephemeral=True,
        )
        return

    content = (text or "").strip()
    source_message_id = ""

    if not content and message_id:
        try:
            ref_msg = await interaction.channel.fetch_message(int(message_id))
            content = (getattr(ref_msg, "content", "") or "").strip()
            source_message_id = str(ref_msg.id)
        except Exception:
            await interaction.response.send_message("Could not fetch message by message_id", ephemeral=True)
            return

    if not content:
        await interaction.response.send_message(
            "Provide rules text via `text` or pass `message_id` of a message in this channel.",
            ephemeral=True,
        )
        return

    db.cur.execute(
        """
        INSERT OR REPLACE INTO bridge_rules
        (bridge_id, content, format, origin_platform, origin_chat_id,
         origin_message_id, hours, messages, last_post_ts, message_counter)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            bridge_id,
            content,
            "discord",
            "discord",
            chat_id,
            source_message_id,
            interval_minutes,
            messages,
            int(time.time()) - (interval_minutes * 60),
            0
        )
    )
    db.conn.commit()

    human = f"{interval_minutes // 60}h {interval_minutes % 60}m".replace("0h ", "").replace(" 0m", "").strip()
    await interaction.response.send_message(
        f"Rules saved — will be posted to **all bridge chats** every {human}",
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
    lines.append("**Discord-серверы:**")
    for g in bot.guilds:
        lines.append(f"- {g.name} — id: {g.id}")

    rows = db.cur.execute("SELECT chat_id FROM chats WHERE platform='telegram'").fetchall()
    prefixes = {}
    for r in rows:
        prefix = r["chat_id"].split(":", 1)[0]
        prefixes[prefix] = True

    if prefixes:
        lines.append("\n**Telegram-группы:**")
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
            for pid in prefixes.keys():
                lines.append(f"- id: {pid}")
    else:
        lines.append("\nНет Telegram чатов в БД.")

    msg = "\n".join(lines)

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
            await guild.leave()
        except Exception as e:
            await interaction.response.send_message(f"Failed to leave guild: {e}", ephemeral=True)
            return

        db.cur.execute("DELETE FROM chat_admins WHERE platform='discord' AND chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
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
        db.cur.execute("DELETE FROM chat_admins WHERE platform='telegram' AND chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.conn.commit()

        await interaction.response.send_message(f"Left Telegram chat {tid} (or cleaned DB).", ephemeral=True)
        return

    await interaction.response.send_message("Unsupported platform. Use 'discord' or 'telegram'.", ephemeral=True)

@bot.tree.command(name="verify")
async def verify_slash(interaction: discord.Interaction):
    prefix = str(interaction.guild_id)
    user_id_str = str(interaction.user.id)

    if db.is_user_verified("discord", user_id_str, "*"):
        await interaction.response.send_message("You are already verified", ephemeral=True)
        return

    prev = db.get_pending_consent("discord", prefix, user_id_str)
    if prev:
        try:
            gid, cid = prev["chat_key"].split(":")
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    msg = await ch.fetch_message(int(prev["bot_message_id"]))
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass
        db.remove_pending_consent("discord", prefix, user_id_str)

    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"
    from discord import ui, ButtonStyle

    class _VerifyView(ui.View):
        def __init__(self, prefix, user_id):
            super().__init__(timeout=None)
            self.prefix = str(prefix)
            self.user_id = str(user_id)

        @ui.button(label=localized_consent_button(lang), style=ButtonStyle.primary)
        async def accept(self, interaction2: discord.Interaction, button: ui.Button):
            if str(interaction2.user.id) != str(self.user_id):
                await interaction2.response.send_message("This button is not for you", ephemeral=True)
                return
            db.add_verified_user("discord", self.user_id, self.prefix, days_valid=365)
            all_pendings = db.get_all_pending_consents_for_user("discord", self.user_id)
            for p in all_pendings:
                db.remove_pending_consent("discord", p["prefix"], self.user_id)
                p_bot_msg_id = p["bot_message_id"]
                if p_bot_msg_id:
                    try:
                        p_guild_id, p_channel_id = p["chat_key"].split(":")
                        p_ch = bot.get_channel(int(p_channel_id))
                        if p_ch:
                            try:
                                p_msg = await p_ch.fetch_message(int(p_bot_msg_id))
                                await p_msg.delete()
                            except Exception:
                                pass
                    except Exception:
                        pass
            try:
                await interaction2.message.delete()
            except Exception:
                pass
            await interaction2.response.send_message("Thanks — verified", ephemeral=True)
            for p in all_pendings:
                await _relay_pending_discord_first_message(p)

    mention = f"<@{interaction.user.id}>"
    consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
    sent = await interaction.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
    db.add_pending_consent(
        "discord",
        prefix,
        user_id_str,
        str(sent.id),
        f"{interaction.guild_id}:{interaction.channel_id}"
    )
    await interaction.response.send_message("Verification message sent (check the channel)", ephemeral=True)

@bot.tree.command(name="unverify")
async def unverify(interaction: discord.Interaction, target: str):
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    uid = None
    if target.startswith("<@") or not target.isdigit() or "#" in target:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            await interaction.response.send_message("Could not resolve user", ephemeral=True)
            return
    else:
        uid = int(target)

    db.cur.execute("DELETE FROM verified_users WHERE platform='discord' AND user_id=?", (str(uid),))
    db.conn.commit()
    await interaction.response.send_message(f"User {uid} unverified.", ephemeral=True)

@bot.tree.command(name="shadow-ban")
async def shadow_ban(interaction: discord.Interaction, target: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    allowed = False
    if is_admin("discord", interaction.user.id):
        allowed = True
    else:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row:
            bridge_admins = db.get_bridge_admins(row["bridge_id"])
            if str(interaction.user.id) in bridge_admins:
                allowed = True
    if not allowed:
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    uid = None
    if target.startswith("<@") or not target.isdigit() or "#" in target:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            await interaction.response.send_message("Could not resolve user", ephemeral=True)
            return
    else:
        uid = int(target)

    db.add_shadow_ban("discord", uid)
    await interaction.response.send_message(f"User {uid} shadow-banned on Discord.", ephemeral=True)

async def _whois_lookup(interaction: discord.Interaction, target_message: discord.Message | None, replied_id: str | None = None):
    """Общая логика для whois: ищет автора target_message или сообщения с replied_id."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    def _find_origin_row(message_id_platform: str | None):
        if not message_id_platform:
            return None
        return db.cur.execute(
            "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
            ("discord", chat_key, str(message_id_platform))
        ).fetchone()

    row = None
    if target_message:
        row = _find_origin_row(str(target_message.id))
    elif replied_id:
        row = _find_origin_row(replied_id)

    if not row:
        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await interaction.response.send_message(localized_whois("origin_missing", lang), ephemeral=True)
        return

    origin_platform = msg_row["origin_platform"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    try:
        if origin_platform == "discord":
            guild_id, _ = msg_row["origin_chat_id"].split(":")
            guild = bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None

            user_obj = None
            try:
                user_obj = await bot.fetch_user(int(origin_sender_id))
            except Exception:
                user_obj = getattr(member, "user", None)

            nick = member.display_name if member else "—"
            user_name = "—"
            if user_obj:
                user_name = f"{user_obj.name}#{user_obj.discriminator}"
            elif member:
                user_name = f"{member.name}#{member.discriminator}"

            mode_key = str(getattr(member, "status", "offline"))
            if mode_key not in ("online", "idle", "dnd", "offline", "invisible"):
                mode_key = "offline"
            if mode_key == "invisible":
                mode_key = "offline"
            mode = localized_whois(f"mode_{mode_key}", lang)

            custom_status = "—"
            if member:
                try:
                    custom = discord.utils.find(lambda a: isinstance(a, discord.CustomActivity), member.activities or [])
                    if custom and getattr(custom, "name", None):
                        custom_status = custom.name
                except Exception:
                    custom_status = "—"

            avatar_url = None
            banner_url = None
            created_at = "—"
            if user_obj:
                avatar_url = str(user_obj.display_avatar.url) if getattr(user_obj, "display_avatar", None) else None
                banner_url = str(user_obj.banner.url) if getattr(user_obj, "banner", None) else None
                if getattr(user_obj, "created_at", None):
                    created_at = discord.utils.format_dt(user_obj.created_at, style="F")

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick or "—", inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=user_name or "—", inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_status", lang), value=custom_status, inline=False)
            embed.add_field(name=localized_whois("field_mode", lang), value=mode, inline=False)
            embed.add_field(name=localized_whois("field_registered", lang), value=created_at, inline=False)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            if banner_url:
                embed.set_image(url=banner_url)

            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif origin_platform == "telegram":
            from telegram_bot import bot as tg_bot
            prefix = msg_row["origin_chat_id"].split(":", 1)[0]
            try:
                member = await tg_bot.get_chat_member(int(prefix), int(origin_sender_id))
                u = member.user
                nick = u.full_name or (u.first_name or "—")
                username = f"@{u.username}" if u.username else "—"
                full_user = await tg_bot.get_chat(int(origin_sender_id))
                bio = getattr(full_user, "bio", None) or "—"
            except Exception:
                nick, username, bio = "—", "—", "—"

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick, inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=username, inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_bio", lang), value=bio if len(bio) < 1000 else (bio[:997] + "..."), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(localized_whois("fetch_error", lang, error=e), ephemeral=True)


@bot.tree.context_menu(name="whois")
async def whois_context_menu(interaction: discord.Interaction, message: discord.Message):
    """Context menu (правая кнопка → Apps → whois): показывает автора пересланного сообщения."""
    await _whois_lookup(interaction, target_message=message)


@bot.tree.command(name="whois")
async def whois_command(interaction: discord.Interaction):
    """
    Slash-команда /whois. Поскольку Discord не передаёт контекст reply для slash-команд,
    используй лучше context menu: ПКМ на сообщении → Apps → whois.
    """
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    await interaction.response.send_message(
        localized_whois("use_context_menu", lang), ephemeral=True
    )

@bot.tree.command(name="bridge")
async def bridge_command(interaction: discord.Interaction):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)
    ).fetchone()

    if not row:
        await interaction.response.send_message(
            localized_bridge_info("not_in_bridge", lang), ephemeral=True
        )
        return

    bridge_id = row["bridge_id"]
    chats = db.get_bridge_chats(bridge_id)

    from telegram_bot import bot as tg_bot

    chat_lines = []
    for chat in chats:
        platform = chat["platform"]
        cid = chat["chat_id"]
        unknown = localized_bridge_info("unknown", lang)
        if platform == "discord":
            try:
                guild_id_str, channel_id_str = cid.split(":", 1)
                guild = bot.get_guild(int(guild_id_str))
                server_name = guild.name if guild else unknown
                channel = guild.get_channel(int(channel_id_str)) if guild else None
                chat_name = channel.name if channel else unknown
                display_id = channel_id_str
            except Exception:
                server_name, chat_name, display_id = unknown, unknown, cid
        elif platform == "telegram":
            try:
                tg_chat_id_str, thread_str = cid.split(":", 1)
                thread_id = int(thread_str)
                tg_chat = await tg_bot.get_chat(int(tg_chat_id_str))
                server_name = tg_chat.title or getattr(tg_chat, "full_name", None) or unknown
                if thread_id == 0:
                    chat_name = server_name
                    display_id = tg_chat_id_str
                else:
                    chat_name = localized_bridge_info("topic", lang, thread_id=thread_id)
                    display_id = None
            except Exception:
                server_name, chat_name, display_id = unknown, unknown, cid
        else:
            server_name, chat_name, display_id = platform, unknown, cid

        chat_lines.append(f"* {server_name}: {chat_name}" + (f" ({display_id})" if display_id is not None else ""))

    chats_value = "\n".join(chat_lines) if chat_lines else "—"

    embed = discord.Embed(
        title=localized_bridge_info("title", lang),
        color=discord.Color.blurple()
    )
    embed.add_field(name=localized_bridge_info("field_number", lang), value=str(bridge_id), inline=False)
    embed.add_field(name=localized_bridge_info("field_chats", lang), value=chats_value, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="allow-bots")
async def allow_bots_command(interaction: discord.Interaction, action: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message("No permission", ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_allow_bots(chat_id, True)
        await interaction.response.send_message("Bot and webhook messages will now be relayed from this channel", ephemeral=True)
    elif action == "disable":
        db.set_allow_bots(chat_id, False)
        await interaction.response.send_message("Bot and webhook messages will no longer be relayed from this channel", ephemeral=True)
    else:
        await interaction.response.send_message("Usage: /allow-bots enable | /allow-bots disable", ephemeral=True)

@bot.tree.command(name="help")
async def help_command(interaction: discord.Interaction):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")

    everyone_lines = "\n".join([
        localized_help("cmd_bridge", lang),
        localized_help("cmd_whois", lang),
        localized_help("cmd_verify", lang),
    ])

    admins_lines = "\n".join([
        localized_help("cmd_rfb", lang),
        localized_help("cmd_setadmin", lang),
        localized_help("cmd_remadmin", lang),
        localized_help("cmd_lang", lang),
        localized_help("cmd_remindrules", lang),
        localized_help("cmd_shadowban", lang),
        localized_help("cmd_unverify", lang),
        localized_help("cmd_allow_bots", lang),
        localized_help("cmd_deadtopic", lang),
        localized_help("cmd_deadchat", lang),
        localized_help("cmd_newschat", lang),
    ])

    embed = discord.Embed(
        title=localized_help("title", lang),
        color=discord.Color.blurple()
    )
    embed.add_field(name=localized_help("section_everyone", lang), value=everyone_lines, inline=False)
    embed.add_field(name=localized_help("section_admins", lang), value=admins_lines, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="backup", description="Send current database backup")
async def backup_discord_cmd(interaction: discord.Interaction):
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message("No permission", ephemeral=True)
        return
    import io
    try:
        with open("bridge.db", "rb") as f:
            data = f.read()
    except Exception as e:
        await interaction.response.send_message(f"Failed to read database: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        file=discord.File(io.BytesIO(data), filename="bridge.db")
    )


async def status_loop():
    """Меняет статус бота раз в минуту, чередуя языки."""
