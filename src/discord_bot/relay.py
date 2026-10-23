"""Delivery of relayed messages and the propagation of edits and deletes.

This module owns the Discord side of *sending*: the per-channel relay
webhooks, the plain-message fallback, the Telegram delivery used when a
Discord message crosses over, and the machinery that keeps copies in sync
with their origin afterwards. The platform-neutral fan-out (headers, reply
resolution, copy bookkeeping) lives in message_relay.py and calls back into
the deliver_* functions here.

Sibling imports that would form cycles (feeds' gallery helpers, appeals'
consul labels, the poll teardown) are done at the call site, in the
project's usual style.
"""
import logging
import re

import discord

import db
import message_relay
from message_relay import (
    build_telegram_text, clip_text, clean_display_name, convert_discord_timestamps,
    discord_to_telegram_html, escape_html, DISCORD_MSG_LIMIT,
)
from utils import get_chat_lang, localized, localized_discord_system_event, localized_sticker

from discord_bot.client import bot
from discord_bot.mentions import (
    _discord_system_event_key, _is_authored_embed, _split_attachment_texts,
    discord_structured_texts,
    extract_discord_forward_payload, replace_channel_mentions_for_telegram,
    replace_mentions,
)

logger = logging.getLogger("bridge.discord")

RELAY_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

RELAY_WEBHOOK_NAME = "Confederate Bridge"
_relay_webhooks = {}
_relay_webhook_ids = set()

def _remember_relay_webhook(channel_id, name, webhook):
    """Cache a relay webhook and record its id — the id set is what lets
    is_own_relay_webhook_message recognize the bot's own webhook posts."""
    _relay_webhooks[(channel_id, name)] = webhook
    _relay_webhook_ids.add(webhook.id)
    return webhook

_FORBIDDEN_WEBHOOK_WORDS = ("discord", "clyde")

def safe_webhook_name(name):
    """A webhook name Discord will accept, or None when it will not.

    Discord rejects names containing 'discord' or 'clyde' (HTTP 400, code
    50035) and caps them at 80 characters. A wiki whose name trips that rule
    keeps its own name in the message — only the webhook object falls back to
    the shared one, which the caller handles by asking for None."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
    if not text:
        return None
    lowered = text.lower()
    if any(word in lowered for word in _FORBIDDEN_WEBHOOK_WORDS):
        return None
    return text

async def _get_relay_webhook(channel, name=None):
    """Return (creating if needed and caching) a relay webhook for a channel.

    `name` asks for a webhook of the bot's own beside the shared one — a wiki
    gets its own so that its avatar and name belong to it rather than being
    per-message overrides on a webhook named after something else. A channel
    holds at most 15 webhooks, so creation can genuinely fail; the caller
    falls back to the shared webhook and, failing that, to a plain message."""
    name = name or RELAY_WEBHOOK_NAME
    cached = _relay_webhooks.get((channel.id, name))
    if cached is not None:
        return cached
    try:
        hooks = await channel.webhooks()
    except Exception:
        return None
    for w in hooks:
        if w.name == name and getattr(w, "token", None):
            return _remember_relay_webhook(channel.id, name, w)
    try:
        w = await channel.create_webhook(name=name)
    except Exception as e:
        logger.warning("Could not create the webhook %r in channel %s: %s",
                       name, channel.id, e)
        return None
    return _remember_relay_webhook(channel.id, name, w)

async def _relay_webhook_owning(channel, webhook_id):
    """The relay webhook that posted a given message, or None.

    With one webhook per wiki beside the shared one, an edit can no longer
    assume which webhook owns a copy; this looks the owner up by id so the
    edit path keeps working whichever of them sent it."""
    for (channel_id, _name), hook in _relay_webhooks.items():
        if channel_id == channel.id and hook.id == webhook_id:
            return hook
    try:
        for hook in await channel.webhooks():
            if hook.id == webhook_id and getattr(hook, "token", None):
                return _remember_relay_webhook(channel.id, hook.name, hook)
    except Exception:
        return None
    return None

def is_own_relay_webhook_message(message):
    """True if ``message`` is one the bot itself posted through a relay webhook.

    These must never be relayed again. We can't rely on the message_copies row
    here: on_message for the webhook message can fire before relay_message has
    recorded the copy, so we match on the webhook id (known synchronously, before
    the send) instead."""
    return getattr(message, "webhook_id", None) in _relay_webhook_ids

def is_dm_chat_id(chat_id):
    """Discord chats attached to a bridge are keyed 'guild:channel'; a user's DM
    (used by the Purgatorium appeal system) is keyed 'dm:<user_id>'."""
    return isinstance(chat_id, str) and chat_id.startswith("dm:")

async def resolve_discord_chat_channel(chat_id):
    """Resolve a Discord chat_id — 'guild:channel' or 'dm:<user_id>' — to a
    messageable channel, or None if it can't be reached."""
    try:
        if is_dm_chat_id(chat_id):
            uid = int(chat_id.split(":", 1)[1])
            user = bot.get_user(uid)
            if user is None:
                user = await bot.fetch_user(uid)
            return user.dm_channel or await user.create_dm()
        channel_id = int(chat_id.split(":")[1])
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        return channel
    except Exception:
        return None

_MD_ESCAPE = {c: "\\" + c for c in "\\*_~`|"}

def _esc_md(text):
    """Escape Discord markdown specials so names with * or _ don't format the header."""
    return "".join(_MD_ESCAPE.get(ch, ch) for ch in (text or ""))

_BOT_SENDER_EMOJI = "<:bot:1513502696953352363>"

def _discord_relay_header(messenger_name, place_name, sender_name, is_bot_sender):
    """The ``[Messenger | Place] Sender:`` first line of a plain (non-webhook)
    Discord copy, markdown-escaped, with the bot emoji appended for bot
    senders so readers can tell relayed bots from people."""
    prefix = message_relay.relay_header_prefix(
        _esc_md(messenger_name), _esc_md(place_name) if place_name else None
    )
    base = f"{prefix} {_esc_md(sender_name)}"
    if is_bot_sender:
        return f"{base} {_BOT_SENDER_EMOJI}:"
    return f"{base}:"

def _webhook_username(sender_name, place_name):
    """Webhook display name: ``Sender [Community]``.

    The source platform is intentionally left out of the name — it's already
    conveyed by the sender's avatar, and a literal ``Discord`` (or ``Clyde``)
    in a webhook username is rejected by Discord's API (HTTP 400, code 50035),
    which would silently drop the relay back to a plain bot message."""
    place = place_name or ""
    if sender_name and place:
        name = f"{sender_name} [{place}]"
    else:
        name = sender_name or place or RELAY_WEBHOOK_NAME
    return clip_text(name, 80)

def _untrusted_spoiler(content):
    """Hide an inbox writer's text and links until staff chooses to reveal it."""
    safe = str(content or "").replace("||", "|\u200b|")
    return f"||{clip_text(safe, DISCORD_MSG_LIMIT - 4)}||"

async def deliver_discord_relay(
    chat, *, header, body_discord, reply_line, reply_to_platform_message_id,
    sender_name=None, place_name=None, messenger_name=None, avatar_url=None,
    is_bot_sender=False, reply_link_line=None, embed=None, prefer_plain=False,
    webhook_name=None, embeds=None, untrusted=False,
):
    """Deliver a relayed message into a Discord channel.

    If the channel has /webhooks enabled (and isn't a thread/forum post), the
    message is sent through a per-channel webhook with the sender's name + platform
    + server as the username and the sender's avatar — otherwise it's a normal bot
    message with the usual ``[Messenger | Place] Sender:`` header. Also handles
    'dm:<user_id>' chats (appeal bridges), which never use webhooks.

    `embed` rides along on either path, for senders whose message is
    structured rather than prose — wiki activity. `prefer_plain` lets such a
    sender decline the webhook path even where the bridge enabled it; it can
    only narrow, never turn webhooks on where the bridge said no.
    `webhook_name` asks for a webhook of the sender's own — one per wiki — and
    falls back to the shared one when the channel has no room for another.
    """
    outbound_embeds = list(embeds or ())
    if embed is not None:
        outbound_embeds.insert(0, embed)
    channel = await resolve_discord_chat_channel(chat["chat_id"])
    if channel is None:
        return None
    channel_id = channel.id

    body = body_discord
    if reply_line:
        body = f"{reply_line}\n{body}"

    if (not prefer_plain and db.get_webhooks_enabled(chat["chat_id"])
            and not isinstance(channel, discord.Thread)):
        webhook = await _get_relay_webhook(channel, webhook_name)
        if webhook is None and webhook_name:
            webhook = await _get_relay_webhook(channel)
        if webhook is not None:
            username = _webhook_username(sender_name, place_name)
            webhook_body = f"{reply_link_line}\n{body}" if reply_link_line else body
            pieces = ([_untrusted_spoiler(webhook_body)] if untrusted else
                      message_relay.split_discord_content("", webhook_body))
            for attempt in range(2):
                try:
                    sent = await webhook.send(
                        pieces[0] or "​", username=username, avatar_url=avatar_url,
                        allowed_mentions=(discord.AllowedMentions.none() if untrusted
                                          else RELAY_ALLOWED_MENTIONS), wait=True,
                        suppress_embeds=untrusted,
                        **({"embeds": outbound_embeds} if outbound_embeds else {}),
                    )
                    parts = await _send_discord_parts(
                        pieces[1:], lambda text: webhook.send(
                            text, username=username, avatar_url=avatar_url,
                            allowed_mentions=RELAY_ALLOWED_MENTIONS, wait=True),
                        channel.id)
                    return message_relay.SentCopy(sent.id, parts) if parts else str(sent.id)
                except Exception as e:
                    _relay_webhooks.pop((channel.id, webhook_name or RELAY_WEBHOOK_NAME), None)
                    logger.warning("Relay webhook send failed (channel=%s, try %d): %s",
                                   channel.id, attempt + 1, e)
                    if attempt:
                        break
                    webhook = await _get_relay_webhook(channel, webhook_name)
                    if webhook is None:
                        break

    if sender_name is not None or place_name is not None or messenger_name is not None:
        disc_header = _discord_relay_header(messenger_name, place_name, sender_name, is_bot_sender)
    else:
        disc_header = header

    send_kwargs = {"allowed_mentions": (discord.AllowedMentions.none() if untrusted
                                        else RELAY_ALLOWED_MENTIONS),
                   "suppress_embeds": untrusted}
    if outbound_embeds:
        send_kwargs["embeds"] = outbound_embeds
    if reply_to_platform_message_id:
        send_kwargs["reference"] = discord.MessageReference(
            message_id=int(reply_to_platform_message_id),
            channel_id=channel_id,
            fail_if_not_exists=False,
        )
        send_kwargs["mention_author"] = False
    full_body = f"{disc_header}\n{body}".strip()
    pieces = ([_untrusted_spoiler(full_body)] if untrusted else
              message_relay.split_discord_content(disc_header, body))
    try:
        sent = await channel.send(pieces[0], **send_kwargs)
    except Exception:
        return None
    parts = await _send_discord_parts(
        pieces[1:], lambda text: channel.send(text, allowed_mentions=RELAY_ALLOWED_MENTIONS),
        channel.id)
    return message_relay.SentCopy(sent.id, parts) if parts else str(sent.id)

async def _send_discord_parts(pieces, send, channel_id):
    """Send the continuations of a long Discord copy, in order, through
    `send` (the channel or the webhook the first message went through).
    Returns their ids; a failure ends the sending and keeps what was sent."""
    parts = []
    for piece in pieces:
        try:
            sent = await send(piece)
        except Exception as e:
            logger.warning("A continuation of a relayed copy failed (channel=%s): %s", channel_id, e)
            break
        parts.append(sent.id)
    return parts

def _discord_native_rich_embeds(message):
    """Keep every rich embed a bot or webhook sent for Discord destinations.

    An embed URL may also appear in the ordinary message text. Dropping the
    embed on that basis loses its fields, image, author and footer. Humans
    cannot send authored embeds; their rich link previews remain excluded.
    Telegram rendering keeps its separate text-preview policy.
    """
    author = getattr(message, "author", None)
    if not (getattr(author, "bot", False) or getattr(message, "webhook_id", None)):
        return []
    return [discord.Embed.from_dict(embed.to_dict())
            for embed in (message.embeds or ()) if embed.type == "rich"][:10]

async def deliver_telegram_relay(
    chat, *, header, body_plain, body_telegram_html, reply_line,
    reply_to_platform_message_id, suppress_link_previews=False, untrusted=False,
):
    """Deliver a relayed message into a Telegram chat/topic.

    Discord-only markup that Telegram can't render (timestamps) is localized
    first; a reply is sent as a native reply where the replied-to copy still
    exists, and retried without the reference when it doesn't. A reply line
    that links to the message elsewhere (`message_relay.ReplyLine`) is
    rendered as a link, and the copy's preview stays on the body's own links.
    A copy longer than one message goes out in several; an inbox writer's
    untrusted text stays one message under its spoiler."""
    from telegram_bot.client import send_relay_texts

    ts_lang = get_chat_lang(chat["chat_id"])
    body_html = convert_discord_timestamps(body_telegram_html or escape_html(body_plain), ts_lang)
    body_plain_local = convert_discord_timestamps(body_plain, ts_lang)
    preview_kwargs = ({} if suppress_link_previews else
                      message_relay.telegram_link_preview_kwargs(reply_line, body_html))
    if reply_line:
        body_html = f"{message_relay.reply_line_html(reply_line)}\n{body_html}"
    if untrusted:
        texts = [build_telegram_text(header, body_html, body_plain_local, spoiler=True)]
    else:
        texts = message_relay.build_telegram_texts(header, body_html, body_plain_local)
    return await send_relay_texts(
        chat["chat_id"], texts, reply_to_message_id=reply_to_platform_message_id,
        **(preview_kwargs or {"disable_web_page_preview": suppress_link_previews}),
    )

async def edit_discord_relay_copy(ch, message_id_platform, header, body,
                                  message_db_id=None, chat=None, embeds=None,
                                  untrusted=False):
    """Edit a relayed Discord copy, handling both normal bot messages and the
    per-sender webhook messages produced when /webhooks is enabled.

    An edit replaces the whole text of a copy, so the "(replying to …)" and
    "(forwarded from …)" lines the first delivery put above the body have to be
    rebuilt here or the edit silently strips them; ``message_db_id``/``chat``
    are what makes that possible. A webhook copy needs it most — it cannot
    carry a native reply reference and holds a markdown link instead — but a
    plain copy carries the forward line inline just the same.

    A copy that took several messages is rewritten as several: the first
    with the start of the new text, its continuations with the rest, new
    ones sent and unneeded ones deleted (`message_relay.sync_copy_parts`)."""
    try:
        m = await ch.fetch_message(int(message_id_platform))
    except Exception as e:
        logger.warning("Relay edit target unreachable (channel=%s, message=%s): %s",
                       getattr(ch, "id", None), message_id_platform, e)
        return
    if getattr(m, "webhook_id", None):
        webhook = await _relay_webhook_owning(ch, m.webhook_id)
        if webhook is not None:
            content_body = body
            if message_db_id is not None and chat is not None:
                content_body = message_relay.build_discord_webhook_relay_body(
                    message_db_id, chat, get_chat_lang(chat["chat_id"]), body
                )
            pieces = ([_untrusted_spoiler(content_body)] if untrusted else
                      message_relay.split_discord_content("", content_body))
            try:
                await webhook.edit_message(
                    m.id,
                    content=pieces[0] or "​",
                    allowed_mentions=(discord.AllowedMentions.none() if untrusted
                                      else RELAY_ALLOWED_MENTIONS),
                    **({"embeds": embeds} if embeds is not None else {}),
                )
            except Exception as e:
                logger.warning("Relay webhook edit failed (channel=%s, message=%s): %s",
                               getattr(ch, "id", None), message_id_platform, e)
                return
            if message_db_id is not None and chat is not None and not untrusted:
                username = m.author.display_name if m.author else None
                avatar = str(m.author.display_avatar.url) if m.author else None

                async def send_part(text):
                    """Send a continuation under the copy's own webhook name."""
                    sent = await webhook.send(text, username=username, avatar_url=avatar,
                                              allowed_mentions=RELAY_ALLOWED_MENTIONS, wait=True)
                    return sent.id

                await message_relay.sync_copy_parts(
                    message_db_id, chat, pieces[1:],
                    edit_part=lambda part_id, text: webhook.edit_message(
                        int(part_id), content=text, allowed_mentions=RELAY_ALLOWED_MENTIONS),
                    send_part=send_part,
                    delete_part=lambda part_id: webhook.delete_message(int(part_id)),
                )
        return

    body_text = body
    if message_db_id is not None and chat is not None:
        prefix_lines = message_relay.relay_edit_prefix_lines(
            message_db_id, chat, get_chat_lang(chat["chat_id"]))
        if prefix_lines:
            body_text = "\n".join(prefix_lines + [body])
    pieces = ([_untrusted_spoiler(f"{header}\n{body_text}".strip())] if untrusted
              else message_relay.split_discord_content(header, body_text))
    try:
        await m.edit(
            content=pieces[0],
            allowed_mentions=(discord.AllowedMentions.none() if untrusted
                              else RELAY_ALLOWED_MENTIONS),
            suppress=untrusted,
            **({"embeds": embeds} if embeds is not None else {}),
        )
    except Exception as e:
        logger.warning("Relay edit failed (channel=%s, message=%s): %s",
                       getattr(ch, "id", None), message_id_platform, e)
        return
    if message_db_id is not None and chat is not None and not untrusted:

        async def edit_part(part_id, text):
            """Rewrite one continuation."""
            part = await ch.fetch_message(int(part_id))
            await part.edit(content=text, allowed_mentions=RELAY_ALLOWED_MENTIONS)

        async def send_part(text):
            """Send a continuation the longer edit needs."""
            sent = await ch.send(text, allowed_mentions=RELAY_ALLOWED_MENTIONS)
            return sent.id

        async def delete_part(part_id):
            """Delete a continuation the shorter edit no longer needs."""
            part = await ch.fetch_message(int(part_id))
            await part.delete()

        await message_relay.sync_copy_parts(
            message_db_id, chat, pieces[1:],
            edit_part=edit_part, send_part=send_part, delete_part=delete_part,
        )

async def send_bridge_mention(bridge_id, origin_platform, origin_chat_id, target_uid,
                              sender_name, place_name, messenger_name, avatar_url=None):
    """Post a relay-style message containing ``<@target_uid>`` into one Discord
    chat of the bridge (used by /mention to call a user from another community).

    The chat is picked at random among the bridge's Discord chats, excluding the
    origin chat when there are others; chats whose guild actually has the target
    as a member are preferred, so the ping lands where it can reach the user.
    Returns True if the message was sent.
    """
    import random

    chats = [c for c in db.get_bridge_chats(bridge_id) if c["platform"] == "discord"]
    if origin_platform == "discord" and len(chats) > 1:
        chats = [c for c in chats if c["chat_id"] != origin_chat_id]
    if not chats:
        return False

    def _has_member(chat):
        """Whether the chat's guild has the target as a cached member."""
        try:
            guild = bot.get_guild(int(chat["chat_id"].split(":")[0]))
            return bool(guild and guild.get_member(int(target_uid)))
        except Exception:
            return False

    preferred = [c for c in chats if _has_member(c)]
    chat = random.choice(preferred or chats)

    sender_name = clean_display_name(sender_name)
    place_name = clean_display_name(place_name)
    sent = await deliver_discord_relay(
        chat,
        header=_discord_relay_header(messenger_name, place_name, sender_name, False),
        body_discord=f"<@{target_uid}>",
        reply_line=None,
        reply_to_platform_message_id=None,
        sender_name=sender_name, place_name=place_name,
        messenger_name=messenger_name, avatar_url=avatar_url,
    )
    return sent is not None

async def _relay_verified_discord_message(message: discord.Message, bridge_id, system_event_key=None, is_bot_sender=False,
                                          origin_chat_id=None, place_name=None, sender_name=None, avatar_url=None):
    """Relay a Discord message into its bridge.

    The origin/place/sender/avatar default to the guild message's own values;
    the appeal system overrides them to relay from a DM (origin 'dm:<uid>',
    localized place) and to anonymize appeal-thread members as consuls (no
    avatar so nothing about them leaks into the webhook path).
    """
    from discord_bot.feeds import relay_avatar_url

    if origin_chat_id is None:
        origin_chat_id = f"{message.guild.id}:{message.channel.id}"
    if place_name is None:
        place_name = message.guild.name or message.channel.name
    if sender_name is None:
        sender_name = message.author.display_name or str(message.author)
    if avatar_url is None:
        avatar_url = str(message.author.display_avatar.url)
    avatar_url = await relay_avatar_url(message.author.id, avatar_url or None)

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

    forward_type, forward_name, forward_text, forward_attachments = \
        await extract_discord_forward_payload(message)
    content = replace_mentions(message, message.content or "")

    if message.stickers:
        texts = ["__DC_STICKER__"]
    else:
        texts = _split_attachment_texts(content, [a.url for a in message.attachments])

    rich_embeds = _discord_native_rich_embeds(message)
    discord_texts = list(texts)
    structured_texts = discord_structured_texts(message)
    if structured_texts:
        structured_block = "\n\n".join(structured_texts)
        if any((t or "").strip() for t in texts):
            texts[0] = (texts[0] or "").rstrip() + "\n\n" + structured_block
        else:
            texts = [structured_block]

    discord_structured = discord_structured_texts(message, include_rich=False)
    if discord_structured:
        block = "\n\n".join(discord_structured)
        discord_texts[0] = ((discord_texts[0] or "").rstrip() + "\n\n" + block).strip()

    if forward_type and not any((t or "").strip() for t in texts):
        texts = _split_attachment_texts(forward_text or "", forward_attachments)
        discord_texts = list(texts)

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False, embeds=None, prefer_plain=False):
        """Per-target delivery callback handed to relay_message: routes to the
        Discord or Telegram deliverer by the target chat's platform."""
        if chat["platform"] == "discord":
            return await deliver_discord_relay(
                chat, header=header, body_discord=body_discord, reply_line=reply_line,
                reply_link_line=reply_link_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender, embeds=embeds, prefer_plain=prefer_plain,
            )

        if chat["platform"] == "telegram":
            return await deliver_telegram_relay(
                chat, header=header, body_plain=body_plain,
                body_telegram_html=body_telegram_html, reply_line=reply_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
            )

        if chat["platform"] == "inbox":
            from inbox import deliver_inbox_relay
            return await deliver_inbox_relay(
                chat, header=header, body_plain=body_plain,
                body_telegram_html=body_telegram_html, reply_line=reply_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name,
            )

    if system_event_key:
        def render_event(chat, lang):
            """The event's sentence in one target's language."""
            sentence = localized_discord_system_event(sender_name, system_event_key, lang)
            return {"text": sentence, "discord": sentence,
                    "telegram_html": discord_to_telegram_html(sentence)}

        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=origin_chat_id,
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=place_name,
            sender_name=sender_name,
            text=localized_discord_system_event(
                sender_name, system_event_key, get_chat_lang(origin_chat_id)),
            reply_to_msg_db_id=_pinned_message_db_id(message, origin_chat_id)
            if system_event_key == "pinned_message" else None,
            send_to_chat_func=send_to_chat,
            avatar_url=avatar_url,
            body_renderer=render_event,
            system_event=True,
        )
        return

    for index, text in enumerate(texts):
        target_lang = get_chat_lang(origin_chat_id)
        localized_text = text.replace("__DC_STICKER__", localized_sticker(target_lang))
        discord_text = (discord_texts[index] if index < len(discord_texts) else "")
        discord_text = discord_text.replace("__DC_STICKER__", localized_sticker(target_lang))
        telegram_text = replace_channel_mentions_for_telegram(localized_text, message.guild)
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=origin_chat_id,
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=place_name,
            sender_name=sender_name,
            text=telegram_text,
            discord_text=discord_text,
            discord_embeds=rich_embeds if index == 0 else None,
            telegram_html=discord_to_telegram_html(telegram_text),
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            forward_type=forward_type,
            forward_name=forward_name,
            is_bot_sender=is_bot_sender,
            avatar_url=avatar_url,
        )

def _pinned_message_db_id(message, origin_chat_id):
    """The relayed message a Discord pin notice points at — its own original
    in this channel, or one of the bridge's copies here — so the relayed
    notice can reply to it in every chat. None when the bridge never relayed
    the pinned message."""
    reference = getattr(message, "reference", None)
    pinned_id = getattr(reference, "message_id", None)
    if not pinned_id:
        return None
    row = db.cur.execute(
        "SELECT message_id FROM message_copies WHERE platform='discord' AND chat_id=?"
        " AND message_id_platform=?", (origin_chat_id, str(pinned_id))
    ).fetchone()
    if row:
        return row["message_id"]
    row = db.cur.execute(
        "SELECT id FROM messages WHERE origin_platform='discord' AND origin_chat_id=?"
        " AND origin_message_id=? ORDER BY id DESC LIMIT 1", (origin_chat_id, str(pinned_id))
    ).fetchone()
    return row["id"] if row else None

async def _relay_pending_discord_first_message(pend_row):
    """Relay the message a user sent *before* consenting, once they consent.

    The message was left in place (only held back from the bridge), so it can
    be re-fetched by id and pushed through the normal relay path; a message
    that has meanwhile been deleted, or one from a bot, is silently skipped."""
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

def discord_edit_payload(message):
    """Render a changed Discord message for Telegram and Discord copies.

    Rich authored embeds stay structured on Discord. Telegram receives their
    readable text, while unsupported embed types keep a text fallback on both.
    """
    content = replace_mentions(message, message.content or "")
    telegram_blocks = discord_structured_texts(message)
    discord_blocks = discord_structured_texts(message, include_rich=False)
    telegram_text = "\n\n".join([content] + telegram_blocks).strip()
    discord_text = "\n\n".join([content] + discord_blocks).strip()
    embeds = _discord_native_rich_embeds(message)
    return telegram_text, discord_text, embeds

def _edit_author_id_for_consul(message_db_id):
    """Original sender of a relayed message (for re-deriving the consul label
    when an appeal-thread message is edited)."""
    row = db.cur.execute(
        "SELECT origin_sender_id FROM messages WHERE id=?", (message_db_id,)
    ).fetchone()
    return row["origin_sender_id"] if row else "0"

async def process_appeal_dm_edit(author_id, message_id, author_display_name, text):
    """Propagate an edit of the appellant's DM message into the appeal thread."""
    from discord_bot.appeals import _appeal_thread_lang

    appeal = db.get_appeal(author_id)
    if not appeal:
        return
    thread_lang = _appeal_thread_lang(appeal["thread_id"])
    await process_discord_message_edit(
        guild=None,
        channel=None,
        message_id=message_id,
        author_display_name=author_display_name,
        text=text,
        origin_chat_id=f"dm:{author_id}",
        place_name=localized("appeal_dm_place", thread_lang),
    )

async def process_discord_message_edit(*, guild, channel, message_id, author_display_name, text,
                                       origin_chat_id=None, place_name=None,
                                       discord_text=None, discord_embeds=None):
    """Propagate an edit of a Discord origin message into every copy.

    Copies on Discord go through edit_discord_relay_copy (which knows webhook
    copies); Telegram copies are re-rendered with the localized header and
    timestamps, and a copy in a receiver bot's private chat through the inbox
    editor. An edit inside an appeal thread — or an anonymized inbox
    conversation — swaps the author name for the label of the *original*
    sender, so anonymity survives edits."""
    if origin_chat_id is None:
        if not guild or not channel:
            return
        origin_chat_id = f"{guild.id}:{channel.id}"

    row = db.cur.execute(
        """
        SELECT id, bridge_id FROM messages
        WHERE origin_platform='discord' AND origin_chat_id=? AND origin_message_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (origin_chat_id, str(message_id))
    ).fetchone()
    if not row:
        return

    if place_name is None:
        place_name = guild.name or channel.name

    if channel is not None:
        appeal_row = db.get_appeal_by_thread(str(channel.id))
        if appeal_row:
            from discord_bot.appeals import _consul_label
            author_display_name = _consul_label(channel.id, _edit_author_id_for_consul(row["id"]))

    if db.is_inbox_bridge(row["bridge_id"]):
        from inbox import inbox_sender_override
        label, _ = inbox_sender_override(
            row["bridge_id"], "discord", _edit_author_id_for_consul(row["id"])
        )
        if label:
            author_display_name = label

    header = f"[Discord | {clean_display_name(place_name)}] {clean_display_name(author_display_name)}:"
    telegram_text = replace_channel_mentions_for_telegram(text, guild)
    text_html = discord_to_telegram_html(telegram_text)

    from sponsor_delivery import update_pending_bodies
    update_pending_bodies(row["id"], lambda chat: {
        "header": header, "body_plain": telegram_text,
        "body_discord": discord_text if discord_text is not None else text,
        "body_telegram_html": text_html, "embeds": discord_embeds or [],
    })
    copies = db.cur.execute(
        "SELECT * FROM message_copies WHERE message_id=? AND COALESCE(kind,'main')='main'",
        (row["id"],)
    ).fetchall()
    from sponsors import sponsor_relay_allowed
    if not sponsor_relay_allowed(row["bridge_id"], "", "", copies):
        return
    for c in copies:
        try:
            if c["platform"] == "discord":
                ch = await resolve_discord_chat_channel(c["chat_id"])
                if ch is None:
                    continue
                await edit_discord_relay_copy(
                    ch, c["message_id_platform"], header,
                    discord_text if discord_text is not None else text,
                    message_db_id=row["id"], chat=c, embeds=discord_embeds)
            elif c["platform"] == "telegram":
                from telegram_bot.client import edit_relay_texts

                ts_lang = get_chat_lang(c["chat_id"])
                body_html, body_plain = message_relay.prefix_relay_edit_body(
                    row["id"], c, ts_lang,
                    convert_discord_timestamps(text_html, ts_lang),
                    convert_discord_timestamps(telegram_text, ts_lang),
                )
                await edit_relay_texts(
                    c["chat_id"], row["id"], c, c["message_id_platform"],
                    message_relay.build_telegram_texts(header, body_html, body_plain),
                    **message_relay.relay_edit_link_preview_kwargs(
                        row["id"], c, ts_lang, body_html),
                )
            elif c["platform"] == "inbox":
                from inbox import edit_inbox_relay_copy
                target_lang = get_chat_lang(c["chat_id"])
                body_html, body_plain = message_relay.prefix_relay_edit_body(
                    row["id"], c, target_lang, text_html, telegram_text)
                await edit_inbox_relay_copy(
                    c["chat_id"], c["message_id_platform"], author_display_name,
                    body_plain, body_html,
                )
        except Exception as e:
            if "message is not modified" in str(e):
                continue
            logger.warning("Discord edit did not reach a copy (%s %s, message %s): %s",
                           c["platform"], c["chat_id"], c["message_id_platform"], e)

def try_remove_bridge_rule(origin_platform, origin_chat_id, origin_message_id):
    """Deleting the message a /remindrules reminder was set from also disables
    the reminder — unless the deleted message was itself one of the relayed
    copies (those share the origin id and must not kill the rule)."""
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

async def process_discord_message_delete(*, guild_id, channel_id, message_id):
    """React to any Discord deletion: a deleted poll message closes the whole
    poll; a deleted origin (or copy — either way) removes the message
    everywhere; a deleted rules source disables its reminder."""
    from discord_bot.commands.polls import close_and_delete_poll

    poll_id = db.get_poll_by_message("discord", f"{guild_id}:{channel_id}", str(message_id))
    if poll_id is not None:
        await close_and_delete_poll(poll_id)
        return

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

async def handle_delete_of_copy(platform, platform_message_id):
    """When a relayed *copy* is deleted, take the origin and all other copies
    with it — moderation in any chat of the bridge acts on the whole bridge."""
    row = db.cur.execute(
        """
        SELECT message_id FROM message_copies
        WHERE platform=? AND message_id_platform=?
        """,
        (platform, platform_message_id)
    ).fetchone()

    if row:
        await delete_all_copies_and_origin(row["message_id"])

_TELEGRAM_SERVICE_SENDERS = {"777000", "1087968824", "136817688"}
"""Telegram's own accounts a group message can come from: the service
notifications, an administrator posting anonymously, and a channel posting
in its group. All three speak for whoever runs the group."""

async def _telegram_origin_protected(chat_key, sender_id):
    """Whether a Telegram original must survive the deletion of its copy.

    Moderation of a copy elsewhere reaches the original in Telegram too, but
    not the messages of the people who run that group: its owner and its
    Telegram administrators, the bot's own administrators of the chat and
    Bot Admins, and whatever was posted anonymously or as a channel. When the
    bot cannot tell — no sender recorded, the membership not readable — the
    message is kept."""
    from utils import is_admin, is_chat_admin
    from telegram_bot import bot as tg_bot

    sender_id = str(sender_id or "")
    if not sender_id.isdigit() or sender_id in _TELEGRAM_SERVICE_SENDERS:
        return True
    if is_admin("telegram", sender_id) or is_chat_admin("telegram", chat_key, sender_id):
        return True
    try:
        member = await tg_bot.get_chat_member(int(chat_key.split(":")[0]), int(sender_id))
    except Exception:
        return True
    return member.status in ("creator", "administrator")

async def _delete_telegram_origin(msg):
    """Delete a Telegram original — every part of an album — unless its
    author is one `_telegram_origin_protected` keeps."""
    from telegram_bot import bot as tg_bot

    if await _telegram_origin_protected(msg["origin_chat_id"], msg["origin_sender_id"]):
        return
    group = int(msg["origin_chat_id"].split(":")[0])
    ids = [row["message_id_platform"] for row in db.cur.execute(
        "SELECT message_id_platform FROM media_group_members WHERE message_id=?", (msg["id"],))]
    for message_id in ids or [msg["origin_message_id"]]:
        try:
            await tg_bot.delete_message(group, int(message_id))
        except Exception:
            pass

async def delete_all_copies_and_origin(msg_id, *, origin_deleted=False):
    """Delete a relayed message everywhere: every platform copy, the origin
    message itself, the GALLERY re-upload (so handed-out CDN links die too),
    and finally the database rows.

    A Telegram original goes too, unless its author runs that group
    (`_delete_telegram_origin`). `origin_deleted` is for the caller that
    knows the original is already gone — the deletion check — and spares
    asking Telegram about it."""
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
            channel = await resolve_discord_chat_channel(c["chat_id"])
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

        elif c["platform"] == "inbox":
            from inbox import delete_inbox_message
            await delete_inbox_message(c["chat_id"], c["message_id_platform"])

    if msg["origin_platform"] == "discord":
        channel = await resolve_discord_chat_channel(msg["origin_chat_id"])
        if channel:
            try:
                m = await channel.fetch_message(int(msg["origin_message_id"]))
                await m.delete()
            except Exception:
                pass

    elif msg["origin_platform"] == "inbox":
        from inbox import delete_inbox_message
        await delete_inbox_message(msg["origin_chat_id"], msg["origin_message_id"])

    elif msg["origin_platform"] == "telegram" and not origin_deleted:
        await _delete_telegram_origin(msg)

    from discord_bot.feeds import drop_gallery_upload
    await drop_gallery_upload(msg_id)

    db.cur.execute("DELETE FROM sponsor_relay_queue WHERE message_id=?", (msg_id,))
    db.cur.execute("DELETE FROM message_copies WHERE message_id=?", (msg_id,))
    db.cur.execute("DELETE FROM media_group_members WHERE message_id=?", (msg_id,))
    db.cur.execute("DELETE FROM messages WHERE id=?", (msg_id,))
    db.conn.commit()
