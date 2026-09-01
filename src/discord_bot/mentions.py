"""Extracting the human-readable pieces of a Discord message: user and
channel mentions, embeds, components, forwarded snapshots, attachments,
system events — plus resolving a free-form user reference to an id.

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

def _media_url(holder):
    """The URL behind an embed image/thumbnail/video or a component's media."""
    if not holder:
        return None
    media = getattr(holder, "media", None)
    if media is not None:
        holder = media
    url = getattr(holder, "url", None)
    return url or None

_GENERATED_EMBED_TYPES = ("image", "gifv", "video", "article", "link")

def _is_authored_embed(embed):
    """Whether this embed was *sent* with the message, rather than unfurled by
    Discord from a link inside it.

    Discord stamps its own link previews with a type of their own — `video` for
    a YouTube link, `article` for a news page, plus `image`, `gifv` and `link`
    — and leaves `rich` for the embeds a bot or a webhook actually posted.
    Only the second kind is content. A preview is Discord's rendering of a URL
    that is already in the message text, so flattening it would append to the
    copy a paragraph the sender never wrote — channel name, video title,
    description, thumbnail and the link a second time — and the target chat
    unfurls the very same link on its own. A message carrying a link must
    cross as the message it is."""
    kind = getattr(embed, "type", None)
    return kind is None or kind not in _GENERATED_EMBED_TYPES

def _one_embed_text(embed, keep_media_only):
    """One embed as a block of markdown lines, or ``None`` when it carries
    nothing worth relaying.

    `keep_media_only` decides the fate of an embed that is nothing but a
    picture: with text beside it in the message the picture is a link preview
    the target chat will build for itself, but on a message that has nothing
    else it is the whole message, and dropping it leaves the reader a bare
    relay header."""
    parts = []

    author = getattr(embed, "author", None)
    if author:
        author_name = getattr(author, "name", None)
        author_url = getattr(author, "url", None)
        if author_name:
            parts.append(f"[{author_name}]({author_url})" if author_url else f"**{author_name}**")

    title = getattr(embed, "title", None)
    url = getattr(embed, "url", None)
    if title:
        parts.append(f"[**{title}**]({url})" if url else f"**{title}**")
    elif url:
        parts.append(url)

    description = getattr(embed, "description", None)
    if description:
        parts.append(str(description))

    for field in getattr(embed, "fields", []) or []:
        fname = getattr(field, "name", None)
        fvalue = getattr(field, "value", None)
        if fname and fvalue:
            parts.append(f"**{fname}**\n{fvalue}")
        elif fname:
            parts.append(f"**{fname}**")
        elif fvalue:
            parts.append(str(fvalue))

    text_parts = list(parts)

    for holder in (getattr(embed, "image", None), getattr(embed, "thumbnail", None),
                   getattr(embed, "video", None)):
        media = _media_url(holder)
        if media and media not in parts:
            parts.append(media)

    footer = getattr(embed, "footer", None)
    footer_text = getattr(footer, "text", None) if footer else None
    if footer_text:
        parts.append(f"_{footer_text}_")

    if not parts:
        return None
    if not text_parts and not footer_text and not keep_media_only:
        return None
    return "\n".join(parts)

def _discord_embed_texts(message: discord.Message):
    """Flatten a message's embeds into text blocks the relay can carry.

    Author, title, description, fields, images and footer are laid out as
    markdown lines, so a bot-generated announcement survives the crossing to
    Telegram — where embeds do not exist — as readable text, and reaches a
    Discord target as text too rather than as a rebuilt embed.

    Only embeds somebody *sent* are flattened (`_is_authored_embed`); the
    previews Discord builds for the links in a message are left alone, since
    the link they came from is already in the text being relayed. Among the
    sent ones, an embed that is only a picture is dropped while the message
    has text of its own and kept when it does not, where it *is* the
    message."""
    embeds = getattr(message, "embeds", []) or []
    has_own_text = bool((getattr(message, "content", "") or "").strip()
                        or getattr(message, "attachments", None))
    texts = []
    for embed in embeds:
        if not _is_authored_embed(embed):
            continue
        block = _one_embed_text(embed, keep_media_only=not has_own_text)
        if block:
            texts.append(block)
    return texts

def _component_text(component, depth=0):
    """One Components V2 element as markdown lines.

    Recursive because the v2 layout nests: a container holds sections, a
    section holds text displays plus one accessory. Only what a reader in
    another chat can use is kept — the text, the media links and the link
    buttons; a button that fires an interaction back at the sending bot is
    useless outside its own channel and is left out."""
    if depth > 6:
        return []

    kind = getattr(getattr(component, "type", None), "name", None)
    lines = []

    if kind == "text_display":
        content = getattr(component, "content", None)
        if content:
            lines.append(str(content))
        return lines

    if kind in ("section", "container", "action_row", "label"):
        for child in getattr(component, "children", []) or []:
            lines.extend(_component_text(child, depth + 1))
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            lines.extend(_component_text(accessory, depth + 1))
        return lines

    if kind == "button":
        label = getattr(component, "label", None)
        url = getattr(component, "url", None)
        if label and url:
            lines.append(f"[{label}]({url})")
        elif url:
            lines.append(str(url))
        return lines

    if kind == "media_gallery":
        for item in getattr(component, "items", []) or []:
            media = _media_url(item)
            if media:
                lines.append(media)
        return lines

    if kind in ("file", "thumbnail"):
        media = _media_url(component)
        if media:
            lines.append(media)
        return lines

    return lines

def _discord_component_texts(message: discord.Message):
    """The readable text of a Components V2 message.

    A message sent with the components-v2 flag carries no `content` and no
    embeds at all — everything it says lives in its components — so without
    this the relay would deliver a header with nothing under it. Returns an
    empty list for ordinary messages, whose components are only the buttons
    beneath a body that was relayed already."""
    flags = getattr(message, "flags", None)
    if not getattr(flags, "components_v2", False):
        return []

    lines = []
    for component in getattr(message, "components", []) or []:
        lines.extend(_component_text(component))
    block = "\n".join(line for line in lines if line)
    return [block] if block.strip() else []

def discord_structured_texts(message: discord.Message):
    """Everything a message says outside its plain `content`: its embeds and,
    for a components-v2 message, its components. One text block per source,
    for the relay to join onto the message body."""
    return _discord_embed_texts(message) + _discord_component_texts(message)

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
