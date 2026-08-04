"""The wiki-relay commands on Discord: `/setwikifeed` and `/remwikifeed`.

Kept apart from commands/feeds.py on purpose. A wiki subscription answers to
a different permission rule — Bot Admins and Bridge Admins rather than chat
admins (utils.bridge_feed_permission) — and the filter and format settings
that come with it will live here too, next to the commands they configure.
The attach machinery underneath is the shared one, so a wiki behaves like
every other followed source once connected.
"""
import discord
from discord import app_commands

import db
import wiki_events
from utils import bridge_feed_permission, feed_scope_name, get_chat_lang, localized

from discord_bot.client import bot
from discord_bot.feeds import attach_feed, feed_module

WIKIFEED_KEYS = {
    "exists": "wikifeed_already_attached",
    "invalid": "wikifeed_invalid_url",
    "not_mediawiki": "wikifeed_not_mediawiki",
    "private": "wikifeed_private",
    "throttled": "wikifeed_throttled",
    "unreachable": "wikifeed_unreachable",
}

@bot.tree.command(name="setwikifeed",
                  description="relay a MediaWiki's activity into this bridge (bot & bridge admins)")
@app_commands.describe(wiki="a link to the wiki — any page of it, its main page, or its api.php")
async def setwikifeed_cmd(interaction: discord.Interaction, wiki: str):
    """Follow a wiki's recent changes in this chat's bridge.

    Any MediaWiki works and any link to it will do: the endpoint is found
    from the address (sources/wiki.py). Relaying starts from the moment of
    attaching — the history is not replayed — and reaches every chat of the
    bridge, including chats that join later."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    allowed, in_bridge = bridge_feed_permission("discord", chat_id, interaction.user.id)
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    status, source, title, stale_since = await attach_feed(
        "wiki", wiki, "discord", chat_id, interaction.user.id)

    if status == "ok":
        text = localized("wikifeed_attached", lang, wiki=source, name=title or source,
                         where=feed_scope_name(chat_id, lang))
        if await attach_discussions_if_any(source, "discord", chat_id, interaction.user.id):
            text += "\n\n" + localized("wikifeed_discussions_note", lang)
        if not in_bridge:
            text += "\n\n" + localized("wikifeed_chat_only_note", lang)
        if stale_since:
            text += "\n\n" + localized("feed_stale_note", lang, account=source, date=stale_since)
    else:
        key = WIKIFEED_KEYS.get(status) or WIKIFEED_KEYS["unreachable"]
        text = localized(key, lang, wiki=source or wiki)
    await interaction.followup.send(text, ephemeral=True)

@bot.tree.command(name="remwikifeed",
                  description="stop relaying a MediaWiki's activity here (bot & bridge admins)")
@app_commands.describe(wiki="the same link, or any other link to that wiki")
async def remwikifeed_cmd(interaction: discord.Interaction, wiki: str):
    """Unfollow a wiki. Any link to it is accepted, not only the one it was
    attached with — both reduce to the same subscription key."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    allowed, _ = bridge_feed_permission("discord", chat_id, interaction.user.id)
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    source = feed_module("wiki").normalize_source(wiki)
    if not source:
        await interaction.response.send_message(
            localized("wikifeed_invalid_url", lang, wiki=wiki), ephemeral=True)
        return

    existing = db.find_feed("wiki", source, chat_id)
    if existing and db.remove_feed("wiki", source, existing["chat_id"]):
        detach_discussions(source, existing["chat_id"])
        await interaction.response.send_message(
            localized("wikifeed_removed", lang, wiki=source), ephemeral=True)
    else:
        await interaction.response.send_message(
            localized("wikifeed_not_attached", lang, wiki=source), ephemeral=True)

async def attach_discussions_if_any(source, platform, chat_id, added_by):
    """Also follow the wiki's Discussions when it has them.

    Fandom's forum is a stream of its own — a different service and a
    different id counter — so it is a second subscription under the same wiki
    key, sharing that wiki's settings row and removed with it. A wiki that is
    not on Fandom, or a Fandom wiki with the feature switched off, simply
    gets no second row and the admin is told nothing about a feature they do
    not have. Returns True when a subscription was made.

    The wiki's name is copied onto the Discussions subscription: its own
    reader has no site name to report, and without this the forum's relayed
    messages — and the webhook they arrive through — would be labelled with
    the wiki's address instead of its name."""
    from sources import fandom

    if not fandom.looks_like_fandom(source):
        return False
    if db.find_feed("wikidisc", source, chat_id):
        return False
    status, _, _, _ = await attach_feed("wikidisc", source, platform, chat_id, added_by)
    if status != "ok":
        return False

    wiki_feed = db.find_feed("wiki", source, chat_id)
    disc_feed = db.find_feed("wikidisc", source, chat_id)
    if wiki_feed and disc_feed and wiki_feed["title"]:
        db.set_feed_last_post("wikidisc", source, disc_feed["chat_id"],
                              disc_feed["last_post_id"] or "0", title=wiki_feed["title"])
    return True

def detach_discussions(source, chat_id):
    """Remove the Discussions subscription that rides along with a wiki."""
    existing = db.find_feed("wikidisc", source, chat_id)
    if existing:
        db.remove_feed("wikidisc", source, existing["chat_id"])

def has_discussions_feed(source, chat_id):
    """Whether this chat is also receiving the wiki's Discussions."""
    return db.find_feed("wikidisc", source, chat_id) is not None

def visible_wiki_feeds(chat_id):
    """The wiki subscriptions this chat can see and configure.

    A chat in a bridge sees the bridge's subscriptions, wherever in it they
    were attached — the activity arrives here either way, so hiding a
    subscription made in a neighbouring chat would only make it unmanageable.
    A chat outside a bridge sees its own."""
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if row and row["bridge_id"] is not None:
        return [f for f in db.get_bridge_feeds(row["bridge_id"]) if f["kind"] == "wiki"]
    return list(db.get_chat_feeds("wiki", chat_id))

def format_wiki_feed_list(chat_id, lang):
    """The `/wikifeeds` answer: every followed wiki with its settings."""
    feeds = visible_wiki_feeds(chat_id)
    if not feeds:
        return localized("wikifeed_list_empty", lang)

    lines = [localized("wikifeed_list_header", lang)]
    for feed in feeds:
        settings = db.get_wiki_feed_settings(feed["source"], feed["chat_id"])
        shown = wiki_events.describe_settings(settings, lang)
        lines.append(localized(
            "wikifeed_list_entry", lang,
            name=feed["title"] or feed["source"], wiki=feed["source"], **shown))
        if has_discussions_feed(feed["source"], feed["chat_id"]):
            lines.append("   " + localized("wikifeed_discussions_note", lang))
    return "\n".join(lines)

def apply_wiki_settings(chat_id, wiki_link, assignments, lang, updated_by):
    """Validate and store a settings change. Returns the reply text.

    The subscription is looked up the way the listing shows them, so a bridge
    admin can configure a wiki that was attached in a neighbouring chat of the
    same bridge — and the change is written against that subscription, so it
    takes effect in every chat the wiki is relayed to. A wiki is followed once
    per bridge and configured once per bridge: the alternative, settings per
    reading chat, meant an admin had to repeat the same command in every chat
    of the bridge and silently lost the activity in the ones they forgot."""
    source = feed_module("wiki").normalize_source(wiki_link)
    if not source:
        return localized("wikifeed_invalid_url", lang, wiki=wiki_link)

    feed = db.find_feed("wiki", source, chat_id)
    if not feed:
        return localized("wikifeed_no_subscription", lang, wiki=source)

    changes, problems = wiki_events.parse_settings_assignments(assignments)
    if problems:
        return "\n".join(wiki_events.settings_problem_text(problems, lang))

    if changes:
        db.set_wiki_feed_settings(source, feed["chat_id"], changes, updated_by=updated_by)

    settings = db.get_wiki_feed_settings(source, feed["chat_id"])
    shown = wiki_events.describe_settings(settings, lang)
    entry = localized("wikifeed_list_entry", lang,
                      name=feed["title"] or source, wiki=source, **shown)
    if not changes:
        return entry
    return localized("wikifeed_settings_saved", lang, wiki=source) + "\n" + entry

@bot.tree.command(name="wikifeeds", description="list the wikis followed here and their settings")
async def wikifeeds_cmd(interaction: discord.Interaction):
    """Show every wiki this chat receives activity from, with its filters and
    output settings. Readable by anyone — it describes what the chat is
    already being shown."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    await interaction.response.send_message(
        format_wiki_feed_list(chat_id, lang), ephemeral=True)

@bot.tree.command(name="wikifeed-settings",
                  description="filters and output format for a followed wiki (bot & bridge admins)")
@app_commands.describe(
    wiki="a link to the followed wiki",
    events="event types to relay, comma-separated — or all/none",
    namespaces="namespace numbers to relay, comma-separated — or all",
    bots="relay edits marked as made by a bot: on/off",
    minor="relay edits marked as minor: on/off",
    format="how Discord shows the activity: embed or text",
    delivery="auto follows the bridge's /webhooks setting; message always uses a plain bot message",
    webhook="own gives this wiki its own webhook with its hosting's logo; shared uses the bridge's common one",
)
async def wikifeed_settings_cmd(interaction: discord.Interaction, wiki: str,
                                events: str = None, namespaces: str = None,
                                bots: str = None, minor: str = None,
                                format: str = None, delivery: str = None,
                                webhook: str = None):
    """Configure one wiki subscription; with no options, show its current
    settings. The typed options are turned back into the same `key=value`
    text the Telegram command takes, so both platforms validate and store
    settings through exactly one code path."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    allowed, _ = bridge_feed_permission("discord", chat_id, interaction.user.id)
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    given = {"events": events, "namespaces": namespaces, "bots": bots,
             "minor": minor, "format": format, "delivery": delivery,
             "webhook": webhook}
    assignments = " ".join(f"{name}={value.strip()}"
                           for name, value in given.items()
                           if value and value.strip())
    await interaction.response.send_message(
        apply_wiki_settings(chat_id, wiki, assignments, lang, interaction.user.id),
        ephemeral=True)
