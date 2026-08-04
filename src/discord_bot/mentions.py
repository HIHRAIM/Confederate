"""Extracting the human-readable pieces of a Discord message: user and
channel mentions, embeds, forwarded snapshots, attachments, system events —
plus resolving a free-form user reference to an id.

Everything here is pure extraction/formatting for the relay to consume;
nothing sends. Not this module's zone: the /mention command (commands/user)
or the relay headers themselves (relay.py).
"""
import re

import discord
from discord.utils import get

from discord_bot.client import bot

async def resolve_discord_user(guild: discord.Guild, identifier: str):
    """Resolve '<@id>', a bare id, 'name#discriminator', a username or a
    display name to a user id within `guild`, trying the cheap forms first and
    a 1000-member fetch as the last resort. Returns None when nothing matches.
    Used by every command that takes a user argument."""
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
    """Rewrite raw <@id>/<@&id> mentions as readable @names. Relay copies in
    other chats can't ping these users anyway, so the text form is all that
    survives the crossing."""
    if not message.guild or not text:
        return text

    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")

    for user in message.mentions:
        text = text.replace(f"<@{user.id}>", f"@{user.display_name}")
        text = text.replace(f"<@!{user.id}>", f"@{user.display_name}")

    return text

def replace_channel_mentions_for_telegram(text, guild) -> str:
    """Discord-упоминания каналов (<#id>) рендерятся в Telegram как #название.
    В Discord они оставляются как есть, чтобы сохранить кликабельное упоминание,
    поэтому замена применяется только к тексту, уходящему в Telegram."""
    if not guild or not text:
        return text

    def _repl(m):
        """One <#id> match as #name, or unchanged when the channel is gone."""
        channel = guild.get_channel_or_thread(int(m.group(1)))
        name = getattr(channel, "name", None)
        return f"#{name}" if name else m.group(0)

    return re.sub(r"<#(\d+)>", _repl, text)

def _discord_embed_texts(message: discord.Message):
    """Flatten a message's embeds into text blocks the relay can carry.

    Pure media embeds (image/gifv/video link previews) are skipped — the link
    itself is already in the message text and the target chat renders its own
    preview. For the rest, author/title/description/fields/images/footer are
    laid out as markdown lines so bot-generated announcements survive the
    crossing to Telegram as readable text."""
    texts = []
    for e in getattr(message, "embeds", []) or []:
        if getattr(e, "type", None) in ("image", "gifv", "video"):
            continue
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
    """The localization key for a system message worth relaying (boosts,
    thread creation, pins, joins), or None for an ordinary message. System
    events are relayed even from unverified users — they carry no user
    content, only the fact."""
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

def _split_attachment_texts(content, attachment_urls):
    """One relayed message per attachment: the first joined to the text, the
    rest on their own.

    A chat renders a preview for only one link per message, so a message
    carrying several files has to be relayed as several messages for all of them
    to be visible. This applies to a message's own attachments and to the
    attachments of a message forwarded into the chat alike."""
    if not attachment_urls:
        return [content]
    first = f"{content}\n{attachment_urls[0]}" if content else attachment_urls[0]
    return [first] + list(attachment_urls[1:])

async def extract_discord_forward_payload(message: discord.Message):
    """What a forwarded message carries: ``(forward_type, forward_name, text,
    attachment_urls)``.

    The attachments are kept apart from the text so the caller can split them
    across one relayed message per file, the way it already splits a message's
    own attachments."""
    forward_type = None
    forward_name = None

    snapshots = getattr(message, "message_snapshots", None) or []
    if snapshots:
        snap = snapshots[0]
        body = (getattr(snap, "content", "") or "").strip()
        snap_attachments = []
        for a in getattr(snap, "attachments", []) or []:
            url = getattr(a, "url", None)
            if url:
                snap_attachments.append(url)

        ref = getattr(message, "reference", None)
        original = getattr(snap, "cached_message", None)
        if original is None and ref is not None and getattr(ref, "message_id", None) and getattr(ref, "channel_id", None):
            src_channel = bot.get_channel(ref.channel_id)
            if src_channel is None:
                try:
                    src_channel = await bot.fetch_channel(ref.channel_id)
                except Exception:
                    src_channel = None
            if src_channel is not None:
                try:
                    original = await src_channel.fetch_message(ref.message_id)
                except Exception:
                    original = None

        author = getattr(original, "author", None)
        if author is not None:
            return "user", (getattr(author, "display_name", None) or str(author)), body, snap_attachments

        src_guild = bot.get_guild(ref.guild_id) if ref is not None and getattr(ref, "guild_id", None) else None
        if src_guild is not None and src_guild.name:
            return "chat", src_guild.name, body, snap_attachments

        return "unknown", None, body, snap_attachments

    if getattr(message, "type", None) == discord.MessageType.reply:
        return None, None, "", []

    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None)
    if resolved and isinstance(resolved, discord.Message):
        body = replace_mentions(resolved, resolved.content or "").strip()
        ref_attachments = [a.url for a in getattr(resolved, "attachments", []) if getattr(a, "url", None)]
        if resolved.channel and getattr(resolved.channel, "name", None):
            forward_type = "chat"
            forward_name = resolved.channel.name
        elif resolved.author:
            forward_type = "user"
            forward_name = resolved.author.display_name or str(resolved.author)
        else:
            forward_type = "unknown"
        return forward_type, forward_name, body, ref_attachments

    if ref and not resolved:
        return "unknown", None, "", []

    return None, None, "", []
