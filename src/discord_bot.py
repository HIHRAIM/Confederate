import discord
from discord import app_commands
from discord.utils import get
import db, message_relay
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, set_chat_lang,
    get_next_status_text, get_chat_lang,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    localized_sticker, localized_discord_system_event
)
import time
import asyncio
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
        title = getattr(e, "title", None)
        description = getattr(e, "description", None)
        if title:
            parts.append(str(title))
        if description:
            parts.append(str(description))
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
        return

    system_event_key = _discord_system_event_key(message)

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    prefix = str(message.guild.id)
    user_id_str = str(message.author.id)

    if db.is_shadow_banned("discord", user_id_str):
        try:
            await message.delete()
        except Exception:
            pass
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
                    db.add_verified_user("discord", self.user_id, "*", days_valid=365)
                    db.remove_pending_consent("discord", self.prefix, self.user_id)
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass
                    await interaction.response.send_message("Thanks — verified", ephemeral=True)

            try:
                mention = f"<@{message.author.id}>"
                consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
                sent = await message.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
                bot_msg_id = str(sent.id)
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent("discord", prefix, user_id_str, bot_msg_id, chat_key)
            except Exception:
                pass
            return

    reply_to_name = None
    forward_type = None
    forward_name = None
    forward_text = ""

    if message.reference and message.reference.resolved:
        replied = message.reference.resolved
        
        relay_bot_ids = (1295454829883298023, 888314689824636998)

        if replied.author.id in relay_bot_ids:
            reply_to_name = extract_username_from_bot_message(replied.content)
        else:
            reply_to_name = replied.author.display_name

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

    if (not any((t or "").strip() for t in texts)):
        embed_texts = _discord_embed_texts(message)
        if embed_texts:
            texts = ["\n\n".join(embed_texts)]

    if forward_type and not any((t or "").strip() for t in texts):
        texts = [forward_text or ""]

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line):
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if not channel:
                return None
            body = body_discord
            if reply_line:
                body = f"{reply_line}\n{body}"
            sent = await channel.send(f"{header}\n{body}".strip())
            return str(sent.id)

        if chat["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id_str, thread = chat["chat_id"].split(":")
            body_html = body_telegram_html or escape_html(body_plain)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            text_html = f"{escape_html(header)}\n{body_html}".strip()
            sent = await tg_bot.send_message(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text_html,
                parse_mode="HTML"
            )
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
            reply_to_name=None,
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
            reply_to_name=reply_to_name,
            send_to_chat_func=send_to_chat,
            forward_type=forward_type,
            forward_name=forward_name,
    )

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

    try:
        hours = int(hours_or_disable)
        if hours <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "Usage: /remindrules <hours|disable> [messages] [message_id] [text]",
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
            hours,
            messages,
            int(time.time()) - (hours * 3600),
            0
        )
    )
    db.conn.commit()

    await interaction.response.send_message(
        "Rules saved and will be posted across the whole bridge",
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
    lines.append("**Discord — серверы, где бот состоит:**")
    for g in bot.guilds:
        lines.append(f"- {g.name} — id: {g.id}")

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
            db.add_verified_user("discord", self.user_id, "*", days_valid=365)
            db.remove_pending_consent("discord", self.prefix, self.user_id)
            try:
                await interaction2.message.delete()
            except Exception:
                pass
            await interaction2.response.send_message("Thanks — verified", ephemeral=True)

    mention = f"<@{interaction.user.id}>"
    consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
    sent = await interaction.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
    db.add_pending_consent("discord", prefix, user_id_str, str(sent.id), f"{interaction.guild_id}:{interaction.channel_id}")
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

@bot.tree.command(name="whois")
async def whois_command(interaction: discord.Interaction):
    def _extract_message_id_from_data(payload):
        if isinstance(payload, dict):
            for key in ("target_id", "message_id"):
                value = payload.get(key)
                if value:
                    return str(value)

            resolved = payload.get("resolved")
            if isinstance(resolved, dict):
                messages = resolved.get("messages")
                if isinstance(messages, dict) and messages:
                    return str(next(iter(messages.keys())))

            options = payload.get("options")
            if isinstance(options, list):
                for opt in options:
                    found = _extract_message_id_from_data(opt)
                    if found:
                        return found

        elif isinstance(payload, list):
            for item in payload:
                found = _extract_message_id_from_data(item)
                if found:
                    return found

        return None

    replied_id = None

    if interaction.message and interaction.message.reference:
        replied = interaction.message.reference.resolved
        if replied:
            replied_id = str(replied.id)

    if not replied_id:
        replied_id = _extract_message_id_from_data(interaction.data or {})

    if not replied_id:
        try:
            relay_bot_ids = {int(bot.user.id)} if bot.user else set()
            relay_bot_ids.update({1295454829883298023, 888314689824636998})
            async for m in interaction.channel.history(limit=30):
                if m.author and m.author.id in relay_bot_ids:
                    replied_id = str(m.id)
                    break
        except Exception:
            pass

    if not replied_id:
        await interaction.response.send_message("Use this command in reply to a bot-relay message", ephemeral=True)
        return

    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    row = db.cur.execute(
        "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
        ("discord", chat_key, replied_id)
    ).fetchone()
    if not row:
        await interaction.response.send_message("Origin not found", ephemeral=True)
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await interaction.response.send_message("Origin missing", ephemeral=True)
        return

    origin_platform = msg_row["origin_platform"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    try:
        nick = "—"
        username = "—"

        if origin_platform == "discord":
            guild_id, _ = msg_row["origin_chat_id"].split(":")
            guild = bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None
            if member:
                nick = member.display_name or "—"
                username = f"{member.name}#{member.discriminator}"
        elif origin_platform == "telegram":
            from telegram_bot import bot as tg_bot
            prefix = msg_row["origin_chat_id"].split(":", 1)[0]
            try:
                member = await tg_bot.get_chat_member(int(prefix), int(origin_sender_id))
                u = member.user
                nick = u.full_name or (u.first_name or "—")
                username = f"@{u.username}" if u.username else "—"
            except Exception:
                pass

        await interaction.response.send_message(
            f"Nickname: {nick}\nUsername: {username}\nID: {origin_sender_id}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"Error fetching user data: {e}", ephemeral=True)

async def status_loop():
    """Меняет статус бота раз в минуту, чередуя языки."""
    await bot.wait_until_ready()
    from telegram_bot import bot as tg_bot
    
    while not bot.is_closed():
        try:
            discord_members = sum((g.member_count or 0) for g in bot.guilds)
            telegram_members = 0
            for gid in db.get_telegram_group_ids():
                try:
                    members_count = await tg_bot.get_chat_member_count(int(gid))
                    telegram_members += int(members_count or 0)
                except Exception:
                    continue
            total_members = discord_members + telegram_members

            discord_servers = len(bot.guilds)
            telegram_groups = db.get_telegram_group_count()
            total_servers = discord_servers + telegram_groups

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
