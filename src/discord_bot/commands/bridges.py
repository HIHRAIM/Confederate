"""Bridge-membership commands: /atb, /rfb, /bridge, plus the bot-admin
service commands /list_chats and /force_leave that manage where the bot is
at all. Also home of resolve_bridge_admins, which the /bridge command of
both platforms shares.
"""
import io
import logging

import discord
from discord import app_commands

import db
from utils import (
    get_chat_lang, is_admin, is_chat_admin, localized, localized_bot_joined,
    localized_bridge_info, localized_bridge_join, localized_bridge_leave,
)

from discord_bot.client import bot
from discord_bot.feeds import feed_module

logger = logging.getLogger("bridge.discord")

@bot.tree.command(name="atb", description="attach this chat to a bridge, existing or new (bot admins)")
@app_commands.describe(
    bridge_id="the bridge's number, or `new` to open one on the lowest free number"
)
async def atb(interaction: discord.Interaction, bridge_id: str | None = None):
    """Attach the current channel to a bridge and announce the join in every
    chat of that bridge, each in its own language.

    `bridge_id` is a number — the bridge is created if it does not exist yet —
    or the word `new`, which opens a bridge on the lowest free number (see
    db.attach_chat_to_new_bridge). The parameter is typed as a string rather
    than an int precisely so that `new` fits; `/atb 5` keeps working exactly
    as before.

    Bot Admins only, for both forms. A chat already in a bridge is refused —
    one chat belongs to at most one bridge — and that check runs before a
    number is allocated, so a refused `/atb new` does not burn one."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id) or "en"

    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    raw = (bridge_id or "").strip()
    if not raw:
        await interaction.response.send_message(localized("atb_usage", lang), ephemeral=True)
        return

    if db.chat_exists(chat_id):
        await interaction.response.send_message(localized("atb_already_attached", lang), ephemeral=True)
        return

    if raw.lower() == "new":
        bridge_id = db.attach_chat_to_new_bridge("discord", chat_id)
        if bridge_id is None:
            await interaction.response.send_message(
                localized("atb_no_free_id", lang, limit=db.APPEAL_BRIDGE_ID_FLOOR),
                ephemeral=True,
            )
            return
        reply_key = "atb_attached_new"
    else:
        try:
            bridge_id = int(raw)
        except ValueError:
            await interaction.response.send_message(localized("atb_invalid_id", lang), ephemeral=True)
            return
        db.attach_chat("discord", chat_id, bridge_id)
        reply_key = "atb_attached"

    try:
        await interaction.channel.send(localized_bot_joined(lang))
    except Exception:
        pass

    await interaction.response.send_message(
        localized(reply_key, lang, bridge_id=bridge_id),
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

@bot.tree.command(name="rfb", description="remove this chat from the bridge")
async def rfb(interaction: discord.Interaction, target: str | None = None):
    """Detach a chat from its bridge and announce the leave everywhere.

    Without `target` it is the current chat; bot admins may also pass another
    chat as '<#channel>', a bare channel id (searched across servers when not
    in this one) or a full 'guild:channel' key. Chat admins may only detach
    chats they administer. Unlike the daily sweep this deletes only the chats
    row — settings survive a manual detach."""
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

    lang = get_chat_lang(chat_key) or "en"
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (target_chat_id,)).fetchone()
    if not row:
        await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
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

    await interaction.response.send_message(localized("rfb_removed", lang), ephemeral=True)

async def resolve_bridge_admins(bridge_id):
    """Return (discord_admins, telegram_admins) for a bridge's admins, each sorted
    alphabetically. discord_admins items are (uid:int, username:str|None);
    telegram_admins items are display strings (@username or name).

    Platform is guessed from the id's magnitude: Discord snowflakes are 64-bit
    (>= 10^13 in practice), Telegram user ids far smaller. Used by /bridge on
    both platforms."""
    admin_ids = db.get_bridge_admins(bridge_id)
    discord_admins = []
    telegram_admins = []
    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None

    for uid in admin_ids:
        try:
            iuid = int(uid)
        except (TypeError, ValueError):
            continue
        if iuid >= 10 ** 13:
            u = bot.get_user(iuid)
            if u is None:
                try:
                    u = await bot.fetch_user(iuid)
                except Exception:
                    u = None
            uname = u.name if u else None
            discord_admins.append(((uname or str(iuid)).lower(), iuid, uname))
            continue
        if tg_bot is not None:
            try:
                ch = await tg_bot.get_chat(iuid)
                uname = getattr(ch, "username", None)
                if uname:
                    telegram_admins.append((uname.lower(), f"@{uname}"))
                else:
                    nm = getattr(ch, "full_name", None) or str(iuid)
                    telegram_admins.append((nm.lower(), nm))
            except Exception:
                pass

    discord_admins.sort()
    telegram_admins.sort()
    return [(uid, uname) for _, uid, uname in discord_admins], [d for _, d in telegram_admins]

@bot.tree.command(name="bridge", description="info about the bridge and connected chats")
async def bridge_command(interaction: discord.Interaction):
    """Show the current chat's bridge: number, member chats with resolved
    names, attached feeds (as links) and the bridge admins of both platforms.
    Ephemeral — it is a lookup, not an announcement."""
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

    attached_feeds = db.get_bridge_feeds(bridge_id)
    if attached_feeds:
        embed.add_field(
            name=localized_bridge_info("field_feeds", lang),
            value="\n".join(
                f"[{f['title'] or f['source']}]({feed_module(f['kind']).source_url(f['source'])})"
                for f in attached_feeds
            ),
            inline=False,
        )

    discord_admins, telegram_pings = await resolve_bridge_admins(bridge_id)
    if discord_admins or telegram_pings:
        admin_lines = []
        if discord_admins:
            discord_str = ", ".join(
                f"<@{uid}> ({uname})" if uname else f"<@{uid}>" for uid, uname in discord_admins
            )
            admin_lines.append(localized_bridge_info("admins_discord", lang, admins=discord_str))
        if telegram_pings:
            admin_lines.append(localized_bridge_info("admins_telegram", lang, admins=", ".join(telegram_pings)))
        embed.add_field(name=localized_bridge_info("admins_title", lang), value="\n".join(admin_lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="list_chats", description="list all chats the bot is in (bot admins)")
async def list_chats(interaction: discord.Interaction):
    """Bot-admin inventory: every Discord guild the bot is in, and every
    Telegram group known to the bridge tables (titles resolved via the Bot
    API where possible). Falls back to a file attachment past Discord's
    message limit."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    lines = []
    lines.append(localized("list_chats_discord_header", lang))
    for g in bot.guilds:
        lines.append(f"- {g.name} — id: {g.id}")

    rows = db.cur.execute("SELECT chat_id FROM chats WHERE platform='telegram'").fetchall()
    prefixes = {}
    for r in rows:
        prefix = r["chat_id"].split(":", 1)[0]
        prefixes[prefix] = True

    if prefixes:
        lines.append("\n" + localized("list_chats_telegram_header", lang))
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
        lines.append("\n" + localized("list_chats_no_telegram", lang))

    msg = "\n".join(lines)

    if len(msg) > 1900:
        bio = io.BytesIO(msg.encode("utf-8"))
        bio.seek(0)
        await interaction.response.send_message(localized("list_chats_too_long", lang), ephemeral=True)
        await interaction.followup.send(file=discord.File(bio, filename="chat_list.txt"))
    else:
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="force_leave", description="make the bot leave a chat (bot admins)")
async def force_leave(interaction: discord.Interaction, platform: str, target_id: str):
    """Bot-admin eviction: leave a Discord server or Telegram group by id and
    scrub its per-chat configuration (admins, dead/news/deadtopic settings,
    chats rows). The Telegram branch proceeds with the cleanup even when
    leave_chat fails — the bot may already have been removed there."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    platform = platform.strip().lower()
    target_id = target_id.strip()

    if platform == "discord":
        try:
            gid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        guild = bot.get_guild(gid)
        if not guild:
            await interaction.response.send_message(localized("force_leave_not_member", lang), ephemeral=True)
            return

        try:
            await guild.leave()
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
            return

        db.cur.execute("DELETE FROM chat_admins WHERE platform='discord' AND chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.cur.execute("DELETE FROM chats WHERE chat_id LIKE ?", (f"{gid}:%",))
        db.conn.commit()

        await interaction.response.send_message(localized("force_leave_success_discord", lang, guild_id=gid), ephemeral=True)
        return

    if platform == "telegram":
        try:
            tid = int(target_id)
        except ValueError:
            await interaction.response.send_message(localized("force_leave_invalid_id", lang), ephemeral=True)
            return

        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.leave_chat(tid)
        except Exception as e:
            await interaction.response.send_message(localized("force_leave_failed", lang, error=e), ephemeral=True)
        db.cur.execute("DELETE FROM chat_admins WHERE platform='telegram' AND chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM dead_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM news_chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chat_settings WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.cur.execute("DELETE FROM chats WHERE chat_id LIKE ?", (f"{tid}:%",))
        db.conn.commit()

        await interaction.response.send_message(localized("force_leave_success_telegram", lang, chat_id=tid), ephemeral=True)
        return

    await interaction.response.send_message(localized("force_leave_unsupported_platform", lang), ephemeral=True)
