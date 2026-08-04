"""Delivering wiki activity: filtering, Discord embeds, and the merging that
keeps a busy wiki from burying a chat.

The wiki feed is the only source whose message is composed per reader rather
than once: the chats of a bridge read in different languages, and a Discord
channel wants an embed where a Telegram topic wants the plain line. That is
what `relay_message`'s `body_renderer` hook exists for, and this module is
what it calls. Whether an event is wanted at all is decided once for the whole
bridge — the filters belong to the subscription, not to the reader — so a
change either reaches every chat of the bridge or none of them.

Not this module's zone: reading the wiki (sources/wiki.py), the wording of
an event (wiki_events.py), or the commands that write the settings
(discord_bot/commands/wiki.py).
"""
import logging

import discord

import db
import message_relay
import wiki_events
from message_relay import clip_text, escape_html

from discord_bot.feeds import avatar_asset_url, feed_send_to_chat

logger = logging.getLogger("bridge.discord")

MERGE_THRESHOLD = 3
EMBED_DESCRIPTION_LIMIT = 400

def group_wiki_events(posts, merge_threshold=MERGE_THRESHOLD):
    """Turn a batch of changes into the items to actually post.

    Nothing is ever dropped: every change fetched is delivered, either as its
    own message or inside a merged one. Below `merge_threshold` the changes go
    out one message each, which is what a quiet wiki should look like. Above
    it, changes to the same page collapse into one line naming the page once —
    a page being worked on hard reads as one event, not fifteen, and that is
    what keeps a busy wiki from becoming a wall of near-identical lines.

    Returns a list of ``(kind, payload)``: ``('single', post)`` or
    ``('merged', [posts])``, ordered by the newest change each covers so the
    chat reads chronologically."""
    if not posts:
        return []
    if len(posts) < merge_threshold:
        return [("single", post) for post in posts]

    by_page = {}
    order = []
    for post in posts:
        title = str((post.get("event") or {}).get("title") or "")
        if title not in by_page:
            by_page[title] = []
            order.append(title)
        by_page[title].append(post)

    items = []
    for title in order:
        bunch = by_page[title]
        items.append(("single", bunch[0]) if len(bunch) == 1 else ("merged", bunch))

    def newest(item):
        """The id of the newest change an item covers, for ordering."""
        post = item[1] if item[0] == "single" else item[1][-1]
        return int(post["id"])

    items.sort(key=newest)
    return items

def wiki_display_name(feed):
    """The wiki's own name, as everything user-facing must show it.

    The relayed message, the Discord webhook's name and its username all use
    this. The stored title is the wiki's `sitename`; the address is only ever
    a last resort for a wiki that refused to say what it is called, since an
    address in place of a name is exactly what a reader does not want to
    see."""
    return str(feed["title"] or "").strip() or str(feed["source"])

def _embed_author(event):
    """The wiki user an embed is attributed to, with a link to their page."""
    user = str(event.get("user") or "")
    if not user:
        return None, None
    site = str(event.get("site_url") or "")
    return user, (f"{site}/wiki/User:{user.replace(' ', '_')}" if site else None)

def build_wiki_embed(event, lang, sentence):
    """A Discord embed for one change.

    The stripe colour comes from the event's group, so a channel can be
    skimmed by colour alone; the title is the sentence and links to the diff
    (or to the page, for events that have no diff), the wiki's own name sits
    in the footer, and the edit summary — the part a reader most wants — gets
    the body to itself instead of trailing the title."""
    embed = discord.Embed(
        title=clip_text(sentence, 250),
        url=event.get("diff_url") or event.get("page_url") or None,
        colour=discord.Colour(wiki_events.event_colour(event)),
    )
    comment = str(event.get("comment_clean") or "").strip()
    if comment:
        embed.description = clip_text(comment, EMBED_DESCRIPTION_LIMIT)

    author, author_url = _embed_author(event)
    if author:
        embed.set_author(name=clip_text(author, 250), url=author_url)

    site_name = event.get("site_name")
    if site_name:
        embed.set_footer(text=clip_text(str(site_name), 200))
    return embed

def build_merged_embed(events, lang, sentence):
    """An embed for several changes to one page: same shape as a single
    change, but pointing at the page rather than at any one diff, and with no
    single author or summary to show."""
    first = events[0]
    embed = discord.Embed(
        title=clip_text(sentence, 250),
        url=first.get("page_url") or None,
        colour=discord.Colour(wiki_events.event_colour(first)),
    )
    site_name = first.get("site_name")
    if site_name:
        embed.set_footer(text=clip_text(str(site_name), 200))
    return embed

def _sender_for(settings, webhook_name):
    """The delivery callback for one batch, bound to the subscription's
    settings and to the webhook the wiki should post through."""
    async def wiki_send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html,
                                reply_line, reply_link_line=None, reply_to_platform_message_id=None,
                                sender_name=None, place_name=None, messenger_name=None,
                                avatar_url=None, is_bot_sender=False, embed=None):
        """Deliver one rendered item.

        Discord gets whatever the renderer produced, embed included, and may
        be told to skip the webhook path; Telegram gets the plain sentence and
        never an embed — it has no such thing, and the sentence already
        carries the same information rather than being a stripped-down
        version of it."""
        if chat["platform"] == "discord":
            from discord_bot.relay import deliver_discord_relay
            return await deliver_discord_relay(
                chat, header=header, body_discord=body_discord, reply_line=reply_line,
                reply_link_line=reply_link_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender, embed=embed,
                prefer_plain=settings.get("delivery") == "message",
                webhook_name=(webhook_name if settings.get("webhook", "own") == "own"
                              else None),
            )
        return await feed_send_to_chat(
            chat, header=header, body_plain=body_plain, body_discord=body_discord,
            body_telegram_html=body_telegram_html, reply_line=reply_line,
            reply_link_line=reply_link_line,
            reply_to_platform_message_id=reply_to_platform_message_id,
            sender_name=sender_name, place_name=place_name,
            messenger_name=messenger_name, avatar_url=avatar_url,
            is_bot_sender=is_bot_sender,
        )

    return wiki_send_to_chat

def _renderer_for(item_kind, payload, settings):
    """Build the per-chat renderer `relay_message` calls for one item.

    The filters are the subscription's own and so give the same answer for
    every chat of the bridge — an event either reaches all of them or none.
    What genuinely differs per chat is decided inside: which language to write
    the sentence in, and whether Discord gets an embed or the same plain line
    Telegram gets."""
    def render(chat, lang):
        """Render this item for one chat, or None when the subscription's
        filters reject the event."""
        if item_kind == "merged":
            events = [post["event"] for post in payload]
            if not any(wiki_events.event_matches(e, settings) for e in events):
                return None
            kept = [e for e in events if wiki_events.event_matches(e, settings)]
            sentence = wiki_events.render_merged(kept, lang)
            rendered = {"text": sentence, "discord": sentence,
                        "telegram_html": escape_html(sentence)}
            if chat["platform"] == "discord" and settings.get("discord_format") != "text":
                rendered["discord"] = ""
                rendered["embed"] = build_merged_embed(kept, lang, sentence)
            return rendered

        event = payload["event"]
        if not wiki_events.event_matches(event, settings):
            return None
        sentence = wiki_events.render_event(event, lang)
        link = event.get("diff_url") or event.get("page_url") or ""
        plain = f"{sentence}\n{link}".strip()
        rendered = {"text": plain, "discord": plain,
                    "telegram_html": escape_html(plain)}
        if chat["platform"] == "discord" and settings.get("discord_format") != "text":
            rendered["discord"] = ""
            rendered["embed"] = build_wiki_embed(event, lang, sentence)
        return rendered

    return render

async def relay_wiki_posts(feed, posts):
    """Relay a batch of wiki changes into the chats of a subscription.

    Grouping, the message cap and the filters all apply once to the whole
    batch: a wiki is followed once per bridge and configured once per bridge,
    so every chat of it sees the same events, and the bot reads the wiki once
    and posts a bounded number of messages. The settings are read once per
    batch rather than once per process, so a `/wikifeed-settings` change takes
    effect on the next poll instead of on the next restart."""
    from discord_bot.feeds import wiki_avatar_asset
    from discord_bot.relay import safe_webhook_name

    targets = db.feed_targets(feed["platform"], feed["chat_id"])
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (feed["chat_id"],)
    ).fetchone()
    avatar = await avatar_asset_url(wiki_avatar_asset(feed["source"]))
    title = wiki_display_name(feed)
    settings = db.get_wiki_feed_settings(feed["source"], feed["chat_id"])
    send_to_chat = _sender_for(settings, safe_webhook_name(title))

    for item_kind, payload in group_wiki_events(posts):
        marker = (payload["id"] if item_kind == "single"
                  else payload[-1]["id"])
        try:
            await message_relay.relay_message(
                bridge_id=row["bridge_id"] if row else None,
                origin_platform="feed:wiki",
                origin_chat_id=f"wiki:{feed['source']}",
                origin_message_id=str(marker),
                origin_sender_id=feed["source"],
                messenger_name=wiki_events.hosting_label(feed["source"]),
                place_name=None,
                sender_name=title,
                text="",
                discord_text="",
                telegram_html="",
                send_to_chat_func=send_to_chat,
                avatar_url=avatar,
                targets=targets,
                body_renderer=_renderer_for(item_kind, payload, settings),
            )
        except Exception as e:
            logger.warning("wiki relay failed (%s -> %s): %s",
                           feed["source"], feed["chat_id"], e)
