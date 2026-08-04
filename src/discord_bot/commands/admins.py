"""Role-management commands (/setadmin, /remadmin, /setlocaladmin,
/remlocaladmin, /localizer-add, /localizer-rem) plus the bot-admin /backup.

The roles themselves are described in db/admins.py and ARCHITECTURE.md; the
commands here only resolve the target user, check the caller's own rights
and call the db layer.
"""
import io
import logging

import discord
from discord import app_commands

import db
from utils import get_chat_lang, is_admin, is_chat_admin, localized, localized_bridge_info

from discord_bot.client import bot
from discord_bot.mentions import resolve_discord_user

logger = logging.getLogger("bridge.discord")

async def _dm_discord_user(interaction: discord.Interaction, uid, text):
    """Best-effort DM to a user just granted something — closed DMs are not
    an error, the grant stands either way."""
    try:
        member = (interaction.guild.get_member(uid) if interaction.guild else None) \
            or await bot.fetch_user(uid)
        if member:
            await member.send(text)
    except Exception:
        pass

@bot.tree.command(name="setadmin", description="add a Bridge Admin")
@app_commands.describe(user="user ID, mention or name",
                       scope="`local` for this bridge only; omit for every bridge of this server")
async def setadmin(interaction: discord.Interaction, user: str, scope: str | None = None):
    """Bridge Admin rights, in the same two scopes as `/allow-files` and
    `/webhooks`: every bridge this server takes part in — including ones it
    joins later — or, with `scope: local`, the bridge of this channel alone."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    scope = (scope or "").strip().lower()
    if scope not in ("", "local"):
        await interaction.response.send_message(localized("setadmin_usage", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("group_only", lang), ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(user)

    place = interaction.guild.name if interaction.guild else str(interaction.guild_id)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await interaction.response.send_message(localized_bridge_info("not_in_bridge", lang), ephemeral=True)
            return
        bridge_id = row["bridge_id"]
        db.add_bridge_admin(bridge_id, uid)
        await interaction.response.send_message(
            localized("setadmin_bridge_done", lang, user_id=uid, bridge_id=bridge_id), ephemeral=True)
        await _dm_discord_user(interaction, uid, localized(
            "setadmin_bridge_dm", lang, bridge_id=bridge_id, place=place))
        return

    db.add_server_bridge_admin("discord", interaction.guild_id, uid,
                               added_by=interaction.user.id)
    await interaction.response.send_message(
        localized("setadmin_server_done", lang, user_id=uid, place=place), ephemeral=True)
    await _dm_discord_user(interaction, uid, localized("setadmin_server_dm", lang, place=place))

@bot.tree.command(name="remadmin", description="remove a Bridge Admin")
@app_commands.describe(user="user ID, mention or name",
                       scope="`local` for this bridge only; omit for every bridge of this server")
async def remadmin(interaction: discord.Interaction, user: str, scope: str | None = None):
    """Revoke Bridge Admin rights in either scope. Bot Admins only — unlike
    the grant, which chat admins may also perform: removal reaches across
    grants the caller may not see."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    scope = (scope or "").strip().lower()
    if scope not in ("", "local"):
        await interaction.response.send_message(localized("remadmin_usage", lang), ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(user)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            db.remove_bridge_admin(row["bridge_id"], uid)
        db.cur.execute(
            "DELETE FROM chat_admins WHERE platform=? AND chat_id=? AND user_id=?",
            ("discord", chat_id, str(uid))
        )
        db.conn.commit()
    else:
        db.remove_server_bridge_admin("discord", interaction.guild_id, uid)
        db.cur.execute(
            "DELETE FROM chat_admins WHERE platform=? AND chat_id LIKE ? AND user_id=?",
            ("discord", f"{interaction.guild_id}:%", str(uid))
        )
        db.conn.commit()

    await interaction.response.send_message(localized("remadmin_done", lang, user_id=uid), ephemeral=True)

@bot.tree.command(name="setlocaladmin", description="delegate server-wide Local Admin rights to a user (bot admins)")
async def setlocaladmin(interaction: discord.Interaction, user: str):
    """Grant server-wide Local Admin — the control-panel scoped-login role
    (see db/admins.py). The username is captured when resolvable, for the
    panel's username login."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("group_only", lang), ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(user)

    server_id = str(interaction.guild_id)
    if db.is_server_admin("discord", server_id, uid):
        await interaction.response.send_message(
            localized("setlocaladmin_already", lang, user_id=uid), ephemeral=True)
        return

    username = None
    member = None
    try:
        member = interaction.guild.get_member(uid) or await bot.fetch_user(uid)
        username = getattr(member, "name", None)
    except Exception:
        pass
    db.add_server_admin("discord", server_id, uid,
                        username=username, added_by=interaction.user.id)
    await interaction.response.send_message(
        localized("setlocaladmin_done", lang, user_id=uid), ephemeral=True)

    try:
        if member:
            await member.send(localized("setlocaladmin_dm", lang, server=interaction.guild.name))
    except Exception:
        pass

@bot.tree.command(name="remlocaladmin", description="revoke a user's server-wide Local Admin rights (bot admins)")
async def remlocaladmin(interaction: discord.Interaction, user: str):
    """Revoke a Local Admin grant made with /setlocaladmin."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(localized("group_only", lang), ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(user)

    server_id = str(interaction.guild_id)
    if not db.is_server_admin("discord", server_id, uid):
        await interaction.response.send_message(
            localized("remlocaladmin_not_admin", lang, user_id=uid), ephemeral=True)
        return

    db.remove_server_admin("discord", server_id, uid)
    await interaction.response.send_message(
        localized("remlocaladmin_done", lang, user_id=uid), ephemeral=True)

async def _resolve_localizer_target(interaction, user):
    """Ping/ID always work; usernames are matched within the current guild."""
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        if interaction.guild is None:
            return None
        return await resolve_discord_user(interaction.guild, user)
    return int(user)

@bot.tree.command(name="localizer-add", description="grant Localizer status: lets the user edit this bot's localization in the control panel")
async def localizer_add(interaction: discord.Interaction, user: str):
    """Grant the Localizer role (control-panel localization editing)."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = await _resolve_localizer_target(interaction, user)
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if db.is_localizer("discord", uid):
        await interaction.response.send_message(
            localized("localizer_add_already", lang, user_id=uid), ephemeral=True)
        return

    username = None
    member = None
    try:
        member = (interaction.guild.get_member(uid) if interaction.guild else None) \
            or await bot.fetch_user(uid)
        username = getattr(member, "name", None)
    except Exception:
        pass
    db.add_localizer("discord", uid, username=username, added_by=interaction.user.id)
    await interaction.response.send_message(
        localized("localizer_add_done", lang, user_id=uid), ephemeral=True)
    try:
        if member:
            await member.send(localized("localizer_add_dm", lang))
    except Exception:
        pass

@bot.tree.command(name="localizer-rem", description="revoke a delegated Localizer status")
async def localizer_rem(interaction: discord.Interaction, user: str):
    """Revoke a Localizer grant (admins, who are localizers implicitly,
    have no row to revoke — that reads as 'not a localizer')."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = await _resolve_localizer_target(interaction, user)
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if not db.remove_localizer("discord", uid):
        await interaction.response.send_message(
            localized("localizer_rem_not", lang, user_id=uid), ephemeral=True)
        return

    await interaction.response.send_message(
        localized("localizer_rem_done", lang, user_id=uid), ephemeral=True)

@bot.tree.command(name="backup", description="get a database backup (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def backup_discord_cmd(interaction: discord.Interaction):
    """Hand the caller an on-demand encrypted database snapshot (same format
    as the 12-hourly automatic backups; decrypt with restore_backup.py and
    the BACKUP_KEY). Allowed in DMs so the file need not land in a server."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        from backup_crypto import build_encrypted_backup, encrypted_filename
        data = build_encrypted_backup("bridge.db")
        await interaction.followup.send(
            file=discord.File(io.BytesIO(data), filename=encrypted_filename("bridge.db")),
            ephemeral=True,
        )
    except Exception as e:
        logger.warning("Failed to build/send database backup: %s", e)
        try:
            await interaction.followup.send(localized("backup_failed", lang, error=str(e)), ephemeral=True)
        except Exception:
            pass
