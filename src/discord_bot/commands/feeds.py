"""The /set*feed and /rem*feed command pairs for the three source kinds.

Each pair shares one body; the per-kind localization keys live in the
*FEED_KEYS dicts, which the Telegram command mirrors import too. The actual
attach logic is discord_bot/feeds.py: attach_feed. A future wiki kind gets
one more key dict and one more command pair here — note its permission model
is planned to differ (bridge admins, not chat admins).
"""
import discord
from discord import app_commands

import db
from utils import feed_scope_name, get_chat_lang, is_admin, is_chat_admin, localized

from discord_bot.client import bot
from discord_bot.feeds import attach_feed, feed_module

async def _feed_command(interaction: discord.Interaction, kind, account, keys):
    """Shared body of `/setbskyfeed`, `/setytfeed` and `/settgfeed`."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    if not (is_admin("discord", interaction.user.id)
            or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    status, source, title, stale_since = await attach_feed(
        kind, account, "discord", chat_id, interaction.user.id)
    if status in ("ok", "live"):
        key = keys["attached_live"] if status == "live" else keys["attached"]
        text = localized(key, lang, account=source, name=title,
                         where=feed_scope_name(chat_id, lang))
        if stale_since:
            text += "\n\n" + localized("feed_stale_note", lang, account=source, date=stale_since)
    else:
        text = localized(keys.get(status) or keys["unreachable"], lang, account=source)
    await interaction.followup.send(text, ephemeral=True)

async def _rem_feed_command(interaction: discord.Interaction, kind, account, keys):
    """Shared body of `/rembskyfeed`, `/remytfeed` and `/remtgfeed`."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    if not (is_admin("discord", interaction.user.id)
            or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    source = feed_module(kind).normalize_source(account)
    if not source:
        await interaction.response.send_message(localized(keys["invalid"], lang), ephemeral=True)
        return

    existing = db.find_feed(kind, source, chat_id)
    if existing and db.remove_feed(kind, source, existing["chat_id"]):
        await interaction.response.send_message(
            localized(keys["removed"], lang, account=source), ephemeral=True)
    else:
        await interaction.response.send_message(
            localized(keys["not_attached"], lang, account=source), ephemeral=True)

YTFEED_KEYS = {
    "attached": "ytfeed_attached", "attached_live": "ytfeed_attached",
    "exists": "ytfeed_already_attached", "invalid": "ytfeed_invalid_channel",
    "throttled": "ytfeed_throttled", "unreachable": "ytfeed_unreachable",
    "removed": "ytfeed_removed", "not_attached": "ytfeed_not_attached",
}

BSKYFEED_KEYS = {
    "attached": "bskyfeed_attached", "attached_live": "bskyfeed_attached",
    "exists": "bskyfeed_already_attached", "invalid": "bskyfeed_invalid_account",
    "throttled": "bskyfeed_throttled", "unreachable": "bskyfeed_unreachable",
    "removed": "bskyfeed_removed", "not_attached": "bskyfeed_not_attached",
}

TGFEED_KEYS = {
    "attached": "tgfeed_attached", "attached_live": "tgfeed_attached_live",
    "exists": "tgfeed_already_attached", "invalid": "tgfeed_invalid_channel",
    "throttled": "tgfeed_throttled", "unreachable": "tgfeed_unreachable",
    "removed": "tgfeed_removed", "not_attached": "tgfeed_not_attached",
}

@bot.tree.command(name="setytfeed", description="relay a public YouTube channel into this bridge (bot admins)")
@app_commands.describe(channel="YouTube channel: @handle, a link to the channel or a UC… channel id")
async def setytfeed_cmd(interaction: discord.Interaction, channel: str):
    """Follow a YouTube channel's uploads in this chat's bridge."""
    await _feed_command(interaction, "youtube", channel, YTFEED_KEYS)

@bot.tree.command(name="remytfeed", description="stop relaying a YouTube channel into this bridge (bot admins)")
@app_commands.describe(channel="YouTube channel: @handle, a link to the channel or a UC… channel id")
async def remytfeed_cmd(interaction: discord.Interaction, channel: str):
    """Unfollow a YouTube channel."""
    await _rem_feed_command(interaction, "youtube", channel, YTFEED_KEYS)

@bot.tree.command(name="setbskyfeed", description="relay a public Bluesky account into this bridge (bot admins)")
@app_commands.describe(account="Bluesky account: handle, @handle, a link to the profile or a DID")
async def setbskyfeed_cmd(interaction: discord.Interaction, account: str):
    """Follow a Bluesky account's posts in this chat's bridge."""
    await _feed_command(interaction, "bluesky", account, BSKYFEED_KEYS)

@bot.tree.command(name="rembskyfeed", description="stop relaying a Bluesky account into this bridge (bot admins)")
@app_commands.describe(account="Bluesky account: handle, @handle, a link to the profile or a DID")
async def rembskyfeed_cmd(interaction: discord.Interaction, account: str):
    """Unfollow a Bluesky account."""
    await _rem_feed_command(interaction, "bluesky", account, BSKYFEED_KEYS)

@bot.tree.command(name="settgfeed", description="relay a public Telegram channel into this bridge (bot admins)")
@app_commands.describe(channel="Telegram channel: the name after t.me/, @name or a link")
async def settgfeed_cmd(interaction: discord.Interaction, channel: str):
    """Follow a public Telegram channel in this chat's bridge."""
    await _feed_command(interaction, "telegram", channel, TGFEED_KEYS)

@bot.tree.command(name="remtgfeed", description="stop relaying a Telegram channel into this bridge (bot admins)")
@app_commands.describe(channel="Telegram channel: the name after t.me/, @name or a link")
async def remtgfeed_cmd(interaction: discord.Interaction, channel: str):
    """Unfollow a Telegram channel."""
    await _rem_feed_command(interaction, "telegram", channel, TGFEED_KEYS)
