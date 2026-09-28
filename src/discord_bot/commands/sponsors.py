"""The sponsorship commands on Discord: linking a Telegram account,
breaking that link, and the four commands a sponsor runs on their own
subscription.

The rules they enforce are not here -- they are in src/sponsors.py, which
both platforms share, so that a claim made from Telegram and a claim made
from Discord cannot come to different conclusions. What is here is the
Discord wording of them.

`/add-telegram` is the Discord half of a handshake whose other half is
`/add_discord` on Telegram; neither does anything on its own, deliberately
(db/sponsors.py: register_link_attempt).
"""
import discord
from discord import app_commands

import db
import sponsors
from config import SPONSOR_URL
from utils import get_chat_lang, is_admin, is_chat_admin, localized

from discord_bot.client import bot

def _chat_key(interaction):
    """This bot's chat key for the channel a command was run in."""
    return f"{interaction.guild_id}:{interaction.channel_id}"

def _is_community_admin(interaction):
    """Whether the caller may speak for this Discord server.

    Deliberately not `is_chat_admin` alone: claiming a community is the very
    first thing a sponsor does in it, before any grant of this bot's own
    exists there, so Discord's own Manage Server permission has to count.
    Bot Admins pass everything, here as everywhere."""
    if is_admin("discord", interaction.user.id):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    return is_chat_admin("discord", _chat_key(interaction), interaction.user.id)

@bot.tree.command(name="add-telegram",
                  description="link your Telegram account to this Discord account")
@app_commands.describe(nickname="your Telegram @username")
async def add_telegram_cmd(interaction: discord.Interaction, nickname: str):
    """Start linking your Telegram account: name your Telegram @username
    here, then run `/add_discord` there within thirty minutes naming your
    Discord username.

    Both halves are required and each must name the other, so nobody can
    attach an account they do not hold by naming it loudly enough. The link
    is what lends a Telegram account the sponsor status bought on Discord --
    and the two then share ONE set of rights and quotas, not one each."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    telegram_nick = (nickname or "").strip().lstrip("@")
    if not telegram_nick:
        await interaction.response.send_message(
            localized("link_usage_discord", lang), ephemeral=True)
        return

    result, _discord_id, _telegram_id = db.register_link_attempt(
        "discord", interaction.user.id, interaction.user.name, telegram_nick)
    key = "link_success" if result == "linked" else "link_pending_discord"
    await interaction.response.send_message(
        localized(key, lang, nick=telegram_nick), ephemeral=True)

@bot.tree.command(name="unlink-accounts",
                  description="break the link between your Discord and Telegram accounts")
async def unlink_accounts_cmd(interaction: discord.Interaction):
    """Break the Discord <-> Telegram link.

    Takes effect at once: the Telegram account holds sponsor rights only
    through the link and stops holding them the moment it is gone. Nothing
    else is touched -- claimed communities and owned bridges belong to the
    Discord account and stay with it."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    row = db.unlink_accounts("discord", interaction.user.id)
    key = "unlink_done" if row else "unlink_none"
    await interaction.response.send_message(localized(key, lang), ephemeral=True)

def sponsor_status_text(overview, lang):
    """The body of `/sponsor`, written once for both platforms.

    Shows the tier in force and, when they differ, the tier actually being
    paid for; every claimed community; every bridge the sponsor owns with the
    sources counted against its limits; and, while a grace period runs, how
    many days are left and what happens at the end of them."""
    limits = overview["limits"]
    lines = [localized("sponsor_status_tier", lang, tier=overview["tier"],
                       communities=len(overview["communities"]),
                       community_limit=limits["communities"])]

    if overview["frozen"]:
        lines.append(localized("sponsor_status_frozen", lang,
                               days=overview["grace_days"] or 0))
    elif overview["paid_tier"] < overview["tier"]:
        lines.append(localized("sponsor_status_downgraded", lang,
                               tier=overview["paid_tier"],
                               days=overview["grace_days"] or 0,
                               communities=overview["quota_limits"]["communities"]))

    if overview["communities"]:
        lines.append("")
        lines.append(localized("sponsor_status_communities", lang))
        for community in overview["communities"]:
            lines.append(localized("sponsor_status_community", lang,
                                   platform=community["platform"],
                                   server_id=community["server_id"]))

    if overview["bridges"]:
        lines.append("")
        lines.append(localized("sponsor_status_bridges", lang))
        quota = overview["quota_limits"]
        for bridge in overview["bridges"]:
            key = "sponsor_status_bridge" if bridge["alive"] else "sponsor_status_bridge_empty"
            lines.append(localized(key, lang, bridge_id=bridge["bridge_id"],
                                   wikis=bridge["wikis"], wiki_limit=quota["wikis"],
                                   socials=bridge["socials"],
                                   social_limit=quota["socials"]))

    link = overview["link"]
    lines.append("")
    lines.append(localized("sponsor_status_link", lang,
                           telegram_id=link["telegram_id"]) if link
                 else localized("sponsor_status_no_link", lang))
    return "\n".join(lines)

@bot.tree.command(name="sponsor", description="your sponsor status, slots and limits")
async def sponsor_cmd(interaction: discord.Interaction):
    """Show what your subscription currently entitles you to."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    overview = sponsors.sponsor_overview("discord", interaction.user.id)
    if overview is None:
        await interaction.response.send_message(
            localized("sponsor_none", lang), ephemeral=True)
        return
    await interaction.response.send_message(
        sponsor_status_text(overview, lang), ephemeral=True)

@bot.tree.command(name="sponsor-claim",
                  description="claim this server as one of your sponsor communities")
async def sponsor_claim_cmd(interaction: discord.Interaction):
    """Take one of your community slots for this server.

    Claiming is explicit on purpose: a slot spent by accident, on the first
    server where the sponsor happened to run something, would be a slot they
    then have to wait a month to move."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    if interaction.guild_id is None:
        await interaction.response.send_message(
            localized("sponsor_claim_not_a_community", lang), ephemeral=True)
        return
    if not _is_community_admin(interaction):
        await interaction.response.send_message(
            localized("sponsor_claim_not_admin", lang), ephemeral=True)
        return

    server_id = str(interaction.guild_id)
    status, extra = sponsors.community_slot_status("discord", interaction.user.id, server_id)
    if status != "ok":
        await interaction.response.send_message(
            localized(f"sponsor_claim_{status}", lang, **extra), ephemeral=True)
        return

    db.claim_community("discord", server_id, extra["discord_id"])
    await interaction.response.send_message(
        localized("sponsor_claim_ok", lang, used=extra["used"] + 1,
                  limit=extra["limit"]), ephemeral=True)

@bot.tree.command(name="sponsor-release",
                  description="give back one of your sponsor community slots")
@app_commands.describe(community="the community's id — this server when left out")
async def sponsor_release_cmd(interaction: discord.Interaction, community: str | None = None):
    """Give a community slot back.

    Nothing in the community is undone: its bridges, chats and sources stay
    exactly as they are, and only the sponsor's standing rights there end.
    Taking a new slot afterwards has to wait out the rotation limit, so that
    one subscription cannot serve a queue of communities a month at a time."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    discord_id = db.canonical_sponsor_id("discord", interaction.user.id)
    if not discord_id:
        await interaction.response.send_message(localized("sponsor_none", lang), ephemeral=True)
        return

    target = (community or "").strip() or str(interaction.guild_id or "")
    released = False
    for platform in ("discord", "telegram"):
        if db.release_community(platform, target, discord_id):
            released = True
    if not released:
        await interaction.response.send_message(
            localized("sponsor_release_none", lang), ephemeral=True)
        return

    db.touch_sponsor_slot_change(discord_id)
    remaining = len(db.get_sponsor_communities(discord_id))
    await interaction.response.send_message(
        localized("sponsor_release_ok", lang, server_id=target, used=remaining,
                  days=sponsors.SPONSOR_SLOT_CHANGE_SECONDS // 86400), ephemeral=True)

@bot.tree.command(name="sponsor-subscribe", description="how to become a sponsor")
async def sponsor_subscribe_cmd(interaction: discord.Interaction):
    """Explain both tiers and how to get one.

    Including the part everybody trips over: the subscription has to be
    connected to Discord on Patreon's site, because the role Patreon grants
    for it is the only thing this bot can see."""
    lang = get_chat_lang(_chat_key(interaction)) or "en"
    tier1 = sponsors.sponsor_limits(1)
    tier2 = sponsors.sponsor_limits(2)
    await interaction.response.send_message(
        localized("sponsor_subscribe", lang, url=SPONSOR_URL,
                  communities1=tier1["communities"], wikis1=tier1["wikis"],
                  socials1=tier1["socials"], communities2=tier2["communities"],
                  wikis2=tier2["wikis"], socials2=tier2["socials"]),
        ephemeral=True)
