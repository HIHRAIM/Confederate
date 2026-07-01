import discord
from discord import app_commands
from discord.utils import get
import db, message_relay
import utils
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, set_chat_lang,
    get_next_status_text, get_chat_lang, rate_limit_ok,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    localized_sticker, localized_discord_system_event, localized_whois,
    localized_bridge_info, localized_deadtopic, localized_help,
    localized, language_name, available_locales, locale_stats, locale_bar,
    compare_reply, LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS, DEFAULT_LANG,
)
from config import SUPPORT_CHATS
import time
import asyncio
import datetime
import io
import os
import secrets
import json
import logging
import re
from message_relay import (
    discord_to_telegram_html, escape_html, convert_discord_timestamps,
    build_telegram_text, clip_text, clean_display_name, DISCORD_MSG_LIMIT,
)

logger = logging.getLogger("bridge.discord")

RELAY_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=False
)

RELAY_WEBHOOK_NAME = "Confederate Bridge"
_relay_webhooks = {}
_relay_webhook_ids = set()

def _remember_relay_webhook(channel_id, webhook):
    _relay_webhooks[channel_id] = webhook
    _relay_webhook_ids.add(webhook.id)
    return webhook

async def _get_relay_webhook(channel):
    """Return (creating if needed and caching) the bot's relay webhook for a channel."""
    cached = _relay_webhooks.get(channel.id)
    if cached is not None:
        return cached
    try:
        hooks = await channel.webhooks()
    except Exception:
        return None
    for w in hooks:
        if w.name == RELAY_WEBHOOK_NAME and getattr(w, "token", None):
            return _remember_relay_webhook(channel.id, w)
    try:
        w = await channel.create_webhook(name=RELAY_WEBHOOK_NAME)
    except Exception:
        return None
    return _remember_relay_webhook(channel.id, w)

def is_own_relay_webhook_message(message):
    """True if ``message`` is one the bot itself posted through a relay webhook.

    These must never be relayed again. We can't rely on the message_copies row
    here: on_message for the webhook message can fire before relay_message has
    recorded the copy, so we match on the webhook id (known synchronously, before
    the send) instead."""
    return getattr(message, "webhook_id", None) in _relay_webhook_ids

_AVATAR_HOST_CHANNEL = 1476645334904995860
_AVATAR_ASSET_MESSAGES = {
    "user-green.png": 1521522655931404431,
    "user-yellow.png": 1521522710826582086,
    "user-red.png": 1521522731022287030,
    "user-grey.png": 1521522764953944224,
    "user-blue.png": 1521522780766736404,
}
_AVATAR_ASSET_URLS = {
    "user-green.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1521522655931404431/user-green.png?ex=6a4523e5&is=6a43d265&hm=08b6f6e47d4195d298a50441ff8620a514b8d7306c9f971f57b14befd655b1bc&",
    "user-yellow.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1521522710826582086/user-yellow.png?ex=6a4523f2&is=6a43d272&hm=04e6b3767e8934487c9c75703e2636c7a4994bb0e2f9109c9b9b9bc4947c14b5&",
    "user-red.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1521522731022287030/user-red.png?ex=6a4523f7&is=6a43d277&hm=11189f7cff40f0e44576b110520b326eed1763bdd482c8da70a6c32fd990b107&",
    "user-grey.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1521522764953944224/user-grey.png?ex=6a4523ff&is=6a43d27f&hm=0f816c050fcc289a66b86a201d2aaf7e381301b4b3d011e20699b9d570be7a05&",
    "user-blue.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1521522780766736404/user-blue.png?ex=6a452403&is=6a43d283&hm=bc9ab7af78b08bc6917f416a9dfccbdaccfd9318838d37d3e1046fb35f68ee03&",
}
_avatar_url_cache = {}
_AVATAR_URL_TTL = 12 * 3600

async def avatar_asset_url(asset):
    """Fresh Discord CDN URL for a bundled avatar asset, fetched from its host
    message (signature refreshed on each fetch) and cached. Falls back to the
    literal signed URL if the live fetch fails."""
    now = time.time()
    cached = _avatar_url_cache.get(asset)
    if cached and now - cached[1] < _AVATAR_URL_TTL:
        return cached[0]

    url = None
    msg_id = _AVATAR_ASSET_MESSAGES.get(asset)
    if msg_id:
        try:
            ch = bot.get_channel(_AVATAR_HOST_CHANNEL) or await bot.fetch_channel(_AVATAR_HOST_CHANNEL)
            msg = await ch.fetch_message(msg_id)
            if msg.attachments:
                url = msg.attachments[0].url
        except Exception as e:
            logger.warning("avatar asset fetch failed (%s): %s", asset, e)
            url = None
    if url is None:
        url = _AVATAR_ASSET_URLS.get(asset)
    if url:
        _avatar_url_cache[asset] = (url, now)
    return url

_MD_ESCAPE = {c: "\\" + c for c in "\\*_~`|"}

def _esc_md(text):
    """Escape Discord markdown specials so names with * or _ don't format the header."""
    return "".join(_MD_ESCAPE.get(ch, ch) for ch in (text or ""))

_BOT_SENDER_EMOJI = "<:bot:1513502696953352363>"

def _discord_relay_header(messenger_name, place_name, sender_name, is_bot_sender):
    base = f"[{_esc_md(messenger_name)} | {_esc_md(place_name)}] {_esc_md(sender_name)}"
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

async def deliver_discord_relay(
    chat, *, header, body_discord, reply_line, reply_to_platform_message_id,
    sender_name=None, place_name=None, messenger_name=None, avatar_url=None,
    is_bot_sender=False, reply_link_line=None,
):
    """Deliver a relayed message into a Discord channel.

    If the channel has /webhooks enabled (and isn't a thread/forum post), the
    message is sent through a per-channel webhook with the sender's name + platform
    + server as the username and the sender's avatar — otherwise it's a normal bot
    message with the usual ``[Messenger | Place] Sender:`` header.
    """
    channel_id = int(chat["chat_id"].split(":")[1])
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None

    body = body_discord
    if reply_line:
        body = f"{reply_line}\n{body}"

    if db.get_webhooks_enabled(chat["chat_id"]) and not isinstance(channel, discord.Thread):
        webhook = await _get_relay_webhook(channel)
        if webhook is not None:
            username = _webhook_username(sender_name, place_name)
            webhook_body = f"{reply_link_line}\n{body}" if reply_link_line else body
            content = clip_text(webhook_body, DISCORD_MSG_LIMIT) or "​"
            for _ in range(2):
                try:
                    sent = await webhook.send(
                        content, username=username, avatar_url=avatar_url,
                        allowed_mentions=RELAY_ALLOWED_MENTIONS, wait=True,
                    )
                    return str(sent.id)
                except discord.NotFound:
                    _relay_webhooks.pop(channel.id, None)
                    webhook = await _get_relay_webhook(channel)
                    if webhook is None:
                        break
                except Exception:
                    break

    if sender_name is not None or place_name is not None or messenger_name is not None:
        disc_header = _discord_relay_header(messenger_name, place_name, sender_name, is_bot_sender)
    else:
        disc_header = header

    send_kwargs = {"allowed_mentions": RELAY_ALLOWED_MENTIONS}
    if reply_to_platform_message_id:
        send_kwargs["reference"] = discord.MessageReference(
            message_id=int(reply_to_platform_message_id),
            channel_id=channel_id,
            fail_if_not_exists=False,
        )
        send_kwargs["mention_author"] = False
    try:
        sent = await channel.send(clip_text(f"{disc_header}\n{body}".strip(), DISCORD_MSG_LIMIT), **send_kwargs)
        return str(sent.id)
    except Exception:
        return None

async def edit_discord_relay_copy(ch, message_id_platform, header, body, message_db_id=None, chat=None):
    """Edit a relayed Discord copy, handling both normal bot messages and the
    per-sender webhook messages produced when /webhooks is enabled.

    A webhook message can't carry native reply/forward references, so those are
    stored inline as prefix lines in its content; ``message_db_id``/``chat`` let
    the edit rebuild them instead of dropping them."""
    try:
        m = await ch.fetch_message(int(message_id_platform))
    except Exception:
        return
    if getattr(m, "webhook_id", None):
        webhook = await _get_relay_webhook(ch)
        if webhook is not None and webhook.id == m.webhook_id:
            content_body = body
            if message_db_id is not None and chat is not None:
                content_body = message_relay.build_discord_webhook_relay_body(
                    message_db_id, chat, get_chat_lang(chat["chat_id"]), body
                )
            try:
                await webhook.edit_message(
                    m.id,
                    content=clip_text(content_body, DISCORD_MSG_LIMIT) or "​",
                    allowed_mentions=RELAY_ALLOWED_MENTIONS,
                )
            except Exception:
                pass
        return
    try:
        await m.edit(
            content=clip_text(f"{header}\n{body}".strip(), DISCORD_MSG_LIMIT),
            allowed_mentions=RELAY_ALLOWED_MENTIONS,
        )
    except Exception:
        pass

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

def replace_channel_mentions_for_telegram(text, guild) -> str:
    """Discord-упоминания каналов (<#id>) рендерятся в Telegram как #название.
    В Discord они оставляются как есть, чтобы сохранить кликабельное упоминание,
    поэтому замена применяется только к тексту, уходящему в Telegram."""
    if not guild or not text:
        return text

    def _repl(m):
        channel = guild.get_channel_or_thread(int(m.group(1)))
        name = getattr(channel, "name", None)
        return f"#{name}" if name else m.group(0)

    return re.sub(r"<#(\d+)>", _repl, text)

def _discord_embed_texts(message: discord.Message):
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

async def _relay_verified_discord_message(message: discord.Message, bridge_id, system_event_key=None, is_bot_sender=False):
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

        origin_chat_id = f"{message.guild.id}:{message.channel.id}"

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

    embed_texts = _discord_embed_texts(message)
    if embed_texts:
        embed_block = "\n\n".join(embed_texts)
        if any((t or "").strip() for t in texts):
            texts[0] = (texts[0] or "").rstrip() + "\n\n" + embed_block
        else:
            texts = [embed_block]

    if forward_type and not any((t or "").strip() for t in texts):
        texts = [forward_text or ""]

    async def send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html, reply_line, reply_link_line=None, reply_to_platform_message_id=None, sender_name=None, place_name=None, messenger_name=None, avatar_url=None, is_bot_sender=False):
        if chat["platform"] == "discord":
            return await deliver_discord_relay(
                chat, header=header, body_discord=body_discord, reply_line=reply_line,
                reply_link_line=reply_link_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
                sender_name=sender_name, place_name=place_name,
                messenger_name=messenger_name, avatar_url=avatar_url,
                is_bot_sender=is_bot_sender,
            )

        if chat["platform"] == "telegram":
            from telegram_bot import bot as tg_bot
            chat_id_str, thread = chat["chat_id"].split(":")
            ts_lang = get_chat_lang(chat["chat_id"])
            body_html = convert_discord_timestamps(body_telegram_html or escape_html(body_plain), ts_lang)
            body_plain_local = convert_discord_timestamps(body_plain, ts_lang)
            if reply_line:
                body_html = f"{escape_html(reply_line)}\n{body_html}"
            text_html = build_telegram_text(header, body_html, body_plain_local)
            send_kwargs = dict(
                chat_id=int(chat_id_str),
                message_thread_id=int(thread) or None,
                text=text_html,
                parse_mode="HTML",
            )
            if reply_to_platform_message_id:
                send_kwargs["reply_to_message_id"] = int(reply_to_platform_message_id)
            try:
                sent = await tg_bot.send_message(**send_kwargs)
            except Exception:
                if reply_to_platform_message_id:
                    send_kwargs.pop("reply_to_message_id", None)
                    sent = await tg_bot.send_message(**send_kwargs)
                else:
                    raise
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
            reply_to_msg_db_id=None,
            send_to_chat_func=send_to_chat,
            avatar_url=str(message.author.display_avatar.url),
        )
        return

    for text in texts:
        target_lang = get_chat_lang(f"{message.guild.id}:{message.channel.id}")
        localized_text = text.replace("__DC_STICKER__", localized_sticker(target_lang))
        telegram_text = replace_channel_mentions_for_telegram(localized_text, message.guild)
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=f"{message.guild.id}:{message.channel.id}",
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=message.guild.name or message.channel.name,
            sender_name=message.author.display_name or str(message.author),
            text=telegram_text,
            discord_text=localized_text,
            telegram_html=discord_to_telegram_html(telegram_text),
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            forward_type=forward_type,
            forward_name=forward_name,
            is_bot_sender=is_bot_sender,
            avatar_url=str(message.author.display_avatar.url),
        )

async def _send_db_backup_discord(client):
    import io
    from config import BACKUP_CHATS
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception:
        return
    fname = encrypted_filename("bridge.db")
    for channel_id in BACKUP_CHATS.get("discord", set()):
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(channel_id)
                except Exception:
                    continue
            if ch:
                await ch.send(file=discord.File(io.BytesIO(data), filename=fname))
        except Exception:
            pass

async def _send_db_backup_telegram():
    from config import BACKUP_CHATS
    from telegram_bot import bot as tg_bot
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception:
        return
    fname = encrypted_filename("bridge.db")
    for chat_entry in BACKUP_CHATS.get("telegram", set()):
        try:
            chat_id_str, thread_str = chat_entry.split(":")
            from aiogram.types import BufferedInputFile
            doc = BufferedInputFile(data, filename=fname)
            await tg_bot.send_document(
                chat_id=int(chat_id_str),
                document=doc,
                message_thread_id=int(thread_str) or None,
            )
        except Exception:
            pass

async def _relay_pending_discord_first_message(pend_row):
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
class DiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        self.loop.create_task(self.deadchat_loop())
        self.loop.create_task(self.status_loop())
        self.loop.create_task(self.bridge_rules_loop())
        self.loop.create_task(self.deadtopic_loop())
        self.loop.create_task(self.backup_loop())
        try:
            for poll in db.get_open_polls():
                self.add_view(PollView(poll["id"], json.loads(poll["options"])))
        except Exception:
            pass

    async def backup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(12 * 3600)
            await _send_db_backup_discord(self)
            await _send_db_backup_telegram()

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

    async def bridge_rules_loop(self):
        """Periodically posts bridge rules to ALL chats in the bridge (Discord + Telegram)."""
        await self.wait_until_ready()
        from telegram_bot import bot as tg_bot

        while not self.is_closed():
            try:
                now = int(time.time())
                rows = db.cur.execute("SELECT * FROM bridge_rules").fetchall()

                for r in rows:
                    interval_minutes = r["hours"]
                    if not interval_minutes or interval_minutes <= 0:
                        continue

                    interval_seconds = interval_minutes * 60
                    elapsed_since_post = now - (r["last_post_ts"] or 0)

                    time_due = elapsed_since_post >= interval_seconds

                    msg_count = r["messages"]
                    count_due = (msg_count is None) or (r["message_counter"] or 0) >= msg_count

                    if not (time_due and count_due):
                        continue

                    content = r["content"] or ""
                    if not content:
                        continue

                    bridge_id = r["bridge_id"]
                    chats = db.get_bridge_chats(bridge_id)

                    for chat in chats:
                        try:
                            if chat["platform"] == "discord":
                                channel_id = int(chat["chat_id"].split(":")[1])
                                channel = self.get_channel(channel_id)
                                if not channel:
                                    try:
                                        channel = await self.fetch_channel(channel_id)
                                    except Exception:
                                        continue
                                await channel.send(content)

                            elif chat["platform"] == "telegram":
                                chat_id_str, thread = chat["chat_id"].split(":")
                                await tg_bot.send_message(
                                    chat_id=int(chat_id_str),
                                    message_thread_id=int(thread) or None,
                                    text=content,
                                )
                        except Exception as e:
                            print(f"bridge_rules_loop: failed to send to {chat['chat_id']}: {e}")

                    db.cur.execute(
                        "UPDATE bridge_rules SET last_post_ts=?, message_counter=0 WHERE bridge_id=?",
                        (now, bridge_id)
                    )
                    db.conn.commit()

            except Exception as e:
                print(f"bridge_rules_loop error: {e}")

            await asyncio.sleep(60)

    async def deadtopic_loop(self):
        """
        Ровно в 00:00 UTC проверяет deadtopic_chats.
        Если с последнего сообщения (или последней отправки бота) прошло >= 6 дней —
        отправляет фантомное сообщение и сразу удаляет его.
        Засыпает до следующей полуночи, чтобы перезапуск бота не влиял на расписание.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            next_midnight = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_seconds = (next_midnight - now_utc).total_seconds()
            await asyncio.sleep(sleep_seconds)

            try:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                now_ts = int(time.time())
                today_str = now_utc.strftime("%Y-%m-%d")
                rows = db.cur.execute("SELECT * FROM deadtopic_chats").fetchall()

                for r in rows:
                    try:
                        chat_id = r["chat_id"]

                        bot_last_ts = r["bot_last_sent_ts"] or 0
                        if bot_last_ts > 0:
                            last_sent_day = datetime.datetime.fromtimestamp(
                                bot_last_ts, tz=datetime.timezone.utc
                            ).strftime("%Y-%m-%d")
                            if last_sent_day == today_str:
                                continue

                        last_msg_ts = r["last_message_ts"] or 0
                        ref_ts = max(last_msg_ts, bot_last_ts)

                        if now_ts - ref_ts < 6 * 86400:
                            continue

                        _, channel_id_str = chat_id.split(":")
                        channel = self.get_channel(int(channel_id_str))
                        if not channel:
                            try:
                                channel = await self.fetch_channel(int(channel_id_str))
                            except Exception:
                                continue

                        lang = get_chat_lang(chat_id) or "en"
                        phantom_text = localized_deadtopic("phantom_message", lang)

                        sent = await channel.send(phantom_text)
                        await asyncio.sleep(1)
                        try:
                            await sent.delete()
                        except Exception:
                            pass

                        db.cur.execute(
                            "UPDATE deadtopic_chats SET bot_last_sent_ts=? WHERE chat_id=?",
                            (now_ts, chat_id)
                        )
                        db.conn.commit()

                    except Exception as e:
                        print(f"deadtopic_loop: error for {r['chat_id']}: {e}")

            except Exception as e:
                print(f"deadtopic_loop error: {e}")

bot = DiscordBot()

async def _post_user_id_to_channels(channel_ids, user_id):
    """Post a bare user ID to each of the given Discord channels.

    Used to publish verification state changes to the VERIFIED / UNVERIFIED
    channels so guard_bot can mirror them into its cross-server database.
    """
    for cid in channel_ids:
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        try:
            await channel.send(str(user_id))
        except Exception as e:
            logger.warning("Failed to publish user id %s to channel %s: %s", user_id, cid, e)

async def announce_verified_user(user_id):
    """Publish a newly verified user's ID to the VERIFIED channel(s)."""
    from config import VERIFIED
    await _post_user_id_to_channels(VERIFIED, user_id)

async def announce_unverified_user(user_id):
    """Publish an unverified user's ID to the UNVERIFIED channel(s)."""
    from config import UNVERIFIED
    await _post_user_id_to_channels(UNVERIFIED, user_id)

@bot.tree.command(name="atb", description="attach this chat to a bridge (bot admins)")
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

@bot.tree.command(name="rfb", description="remove this chat from the bridge")
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
        if message.author == bot.user:
            return
        if is_own_relay_webhook_message(message):
            return
        if db.get_allow_bots(chat_id) and not db.is_relay_copy("discord", chat_id, str(message.id)):
            row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if row:
                if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
                    logger.warning("Rate limit: dropping relay from discord bot %s in %s", message.author.id, chat_id)
                    return
                await _relay_verified_discord_message(message, row["bridge_id"], is_bot_sender=True)
        return

    db.cur.execute(
        "UPDATE deadtopic_chats SET last_message_ts=? WHERE chat_id=?",
        (int(time.time()), chat_id)
    )
    db.conn.commit()

    system_event_key = _discord_system_event_key(message)

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    db.cur.execute(
        "UPDATE bridge_rules SET message_counter = COALESCE(message_counter, 0) + 1 WHERE bridge_id=?",
        (bridge_id,)
    )
    db.conn.commit()

    prefix = str(message.guild.id)
    user_id_str = str(message.author.id)

    if db.is_shadow_banned("discord", user_id_str):
        try:
            await message.delete()
        except Exception as e:
            logger.warning(
                "Failed to delete shadow-banned message (user=%s, chat=%s): %s",
                user_id_str, chat_id, e
            )
        return

    if system_event_key and not db.is_user_verified("discord", user_id_str, prefix):
        await _relay_verified_discord_message(message, bridge_id, system_event_key)
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
                    db.add_verified_user("discord", self.user_id, self.prefix, days_valid=365)
                    all_pendings = db.get_all_pending_consents_for_user("discord", self.user_id)
                    for p in all_pendings:
                        db.remove_pending_consent("discord", p["prefix"], self.user_id)
                        p_bot_msg_id = p["bot_message_id"]
                        if p_bot_msg_id:
                            try:
                                p_guild_id, p_channel_id = p["chat_key"].split(":")
                                p_ch = bot.get_channel(int(p_channel_id))
                                if p_ch:
                                    try:
                                        p_msg = await p_ch.fetch_message(int(p_bot_msg_id))
                                        await p_msg.delete()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass
                    await interaction.response.send_message("Thanks — verified", ephemeral=True)
                    await announce_verified_user(self.user_id)
                    for p in all_pendings:
                        await _relay_pending_discord_first_message(p)

            try:
                mention = f"<@{message.author.id}>"
                consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
                sent = await message.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
                bot_msg_id = str(sent.id)
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    bot_msg_id,
                    chat_key,
                    first_message_id=str(message.id)
                )
            except Exception:
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    "",
                    chat_key,
                    first_message_id=str(message.id)
                )
            return

    if not rate_limit_ok(("relay", "discord", user_id_str), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping relay from discord user %s in %s", user_id_str, chat_id)
        return

    await _relay_verified_discord_message(message, bridge_id, system_event_key)

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

    header = f"[Discord | {clean_display_name(guild.name or channel.name)}] {clean_display_name(author_display_name)}:"
    telegram_text = replace_channel_mentions_for_telegram(text, guild)
    text_html = discord_to_telegram_html(telegram_text)

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
                await edit_discord_relay_copy(ch, c["message_id_platform"], header, text, message_db_id=row["id"], chat=c)
            elif c["platform"] == "telegram":
                from telegram_bot import bot as tg_bot
                chat_id, _ = c["chat_id"].split(":")
                ts_lang = get_chat_lang(c["chat_id"])
                await tg_bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(c["message_id_platform"]),
                    text=build_telegram_text(
                        header,
                        convert_discord_timestamps(text_html, ts_lang),
                        convert_discord_timestamps(telegram_text, ts_lang),
                    ),
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

@bot.event
async def on_message_delete(message: discord.Message):
    await process_discord_message_delete(
        guild_id=message.guild.id if message.guild else None,
        channel_id=message.channel.id if message.channel else None,
        message_id=message.id,
    )

async def process_discord_message_delete(*, guild_id, channel_id, message_id):
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

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await process_discord_message_delete(
        guild_id=payload.guild_id,
        channel_id=payload.channel_id,
        message_id=payload.message_id,
    )

@bot.tree.command(name="setadmin", description="add a Bridge Admin")
async def setadmin(interaction: discord.Interaction, user: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = None
    if user.startswith("@") or not user.isdigit() or "#" in user or user.startswith("<@"):
        uid = await resolve_discord_user(interaction.guild, user)
        if uid is None:
            await interaction.response.send_message("Could not resolve user", ephemeral=True)
            return
    else:
        uid = int(user)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        await interaction.response.send_message(localized_bridge_info("not_in_bridge", lang), ephemeral=True)
        return
    bridge_id = row["bridge_id"]

    db.add_bridge_admin(bridge_id, uid)
    await interaction.response.send_message(localized("setadmin_bridge_done", lang, user_id=uid), ephemeral=True)

    try:
        member = interaction.guild.get_member(uid) or await bot.fetch_user(uid)
        if member:
            await member.send(localized("setadmin_bridge_dm", lang, bridge_id=bridge_id))
    except Exception:
        pass

@bot.tree.command(name="remadmin", description="remove a Bridge Admin")
async def remadmin(interaction: discord.Interaction, user: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    if not is_admin("discord", interaction.user.id):
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
        "DELETE FROM chat_admins WHERE platform=? AND chat_id=? AND user_id=?",
        ("discord", chat_id, str(uid))
    )
    db.conn.commit()

    await interaction.response.send_message(f"User `{uid}` removed from chat admins", ephemeral=True)

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
    db.cur.execute("DELETE FROM media_group_members WHERE message_id=?", (msg_id,))
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

    db.cur.execute(
        "DELETE FROM deadtopic_chats WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.conn.commit()

@bot.tree.command(name="deadchat", description="ping a role when chat is inactive (Discord only)")
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

    if not role_id.isdigit():
        await interaction.response.send_message(
            "role_id must be a numeric role ID or 'disable'",
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

@bot.tree.command(name="newschat", description="auto-react to messages in a news channel (Discord only)")
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

@bot.tree.command(name="deadtopic", description="send a phantom message every 6 days of inactivity to keep the topic alive")
async def deadtopic(
    interaction: discord.Interaction,
    action: str,
):
    """
    /deadtopic enable  — включить авто-сохранение темы (phantom message каждые 6 дней).
    /deadtopic disable — выключить.
    Доступно только Bridge Admins и Bot Admins.
    """
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    user_id = interaction.user.id

    allowed = is_admin("discord", user_id)
    if not allowed:
        row = db.cur.execute(
            "SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if row:
            bridge_admins = db.get_bridge_admins(row["bridge_id"])
            if str(user_id) in bridge_admins:
                allowed = True

    if not allowed:
        await interaction.response.send_message("No permission", ephemeral=True)
        return

    lang = get_chat_lang(chat_id) or "en"
    action = action.strip().lower()

    if action == "disable":
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id=?", (chat_id,))
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("disabled", lang), ephemeral=True
        )
        return

    if action == "enable":
        now_ts = int(time.time())
        db.cur.execute(
            """
            INSERT INTO deadtopic_chats (chat_id, last_message_ts, bot_last_sent_ts)
            VALUES (?, ?, NULL)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_message_ts=excluded.last_message_ts,
                bot_last_sent_ts=NULL
            """,
            (chat_id, now_ts)
        )
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("enabled", lang), ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Usage: /deadtopic enable | /deadtopic disable",
        ephemeral=True
    )

@bot.tree.command(name="remindrules", description="periodically post rules to all bridge chats (e.g.: 2h, 30m)")
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

    raw = hours_or_disable.strip().lower()
    try:
        if raw.endswith("h"):
            interval_minutes = int(raw[:-1]) * 60
        elif raw.endswith("m"):
            interval_minutes = int(raw[:-1])
        else:
            interval_minutes = int(raw) * 60
        if interval_minutes <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            "Usage: /remindrules <5h|30m|disable> [messages] [message_id] [text]\n"
            "Examples: `2h` — every 2 hours, `30m` — every 30 minutes",
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
            interval_minutes,
            messages,
            int(time.time()) - (interval_minutes * 60),
            0
        )
    )
    db.conn.commit()

    human = f"{interval_minutes // 60}h {interval_minutes % 60}m".replace("0h ", "").replace(" 0m", "").strip()
    await interaction.response.send_message(
        f"Rules saved — will be posted to **all bridge chats** every {human}",
        ephemeral=True
    )

@bot.tree.command(name="lang", description="set bot language (ru, uk, pl, en, es, pt)")
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

@bot.tree.command(name="list_chats", description="list all chats the bot is in (bot admins)")
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
    lines.append("**Discord-серверы:**")
    for g in bot.guilds:
        lines.append(f"- {g.name} — id: {g.id}")

    rows = db.cur.execute("SELECT chat_id FROM chats WHERE platform='telegram'").fetchall()
    prefixes = {}
    for r in rows:
        prefix = r["chat_id"].split(":", 1)[0]
        prefixes[prefix] = True

    if prefixes:
        lines.append("\n**Telegram-группы:**")
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

@bot.tree.command(name="force_leave", description="make the bot leave a chat (bot admins)")
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
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id LIKE ?", (f"{gid}:%",))
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

@bot.tree.command(name="verify", description="confirm consent to message forwarding")
async def verify_slash(interaction: discord.Interaction):
    prefix = str(interaction.guild_id)
    user_id_str = str(interaction.user.id)

    if not rate_limit_ok(("verify-cmd", "discord", user_id_str), limit=2, window_seconds=60):
        await interaction.response.send_message("Too many requests — try again later", ephemeral=True)
        return

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
            db.add_verified_user("discord", self.user_id, self.prefix, days_valid=365)
            all_pendings = db.get_all_pending_consents_for_user("discord", self.user_id)
            for p in all_pendings:
                db.remove_pending_consent("discord", p["prefix"], self.user_id)
                p_bot_msg_id = p["bot_message_id"]
                if p_bot_msg_id:
                    try:
                        p_guild_id, p_channel_id = p["chat_key"].split(":")
                        p_ch = bot.get_channel(int(p_channel_id))
                        if p_ch:
                            try:
                                p_msg = await p_ch.fetch_message(int(p_bot_msg_id))
                                await p_msg.delete()
                            except Exception:
                                pass
                    except Exception:
                        pass
            try:
                await interaction2.message.delete()
            except Exception:
                pass
            await interaction2.response.send_message("Thanks — verified", ephemeral=True)
            await announce_verified_user(self.user_id)
            for p in all_pendings:
                await _relay_pending_discord_first_message(p)

    mention = f"<@{interaction.user.id}>"
    consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
    sent = await interaction.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
    db.add_pending_consent(
        "discord",
        prefix,
        user_id_str,
        str(sent.id),
        f"{interaction.guild_id}:{interaction.channel_id}"
    )
    await interaction.response.send_message("Verification message sent (check the channel)", ephemeral=True)

@bot.tree.command(name="unverify", description="revoke a user's verification")
async def unverify(interaction: discord.Interaction, target: str = None):
    if target is None or not target.strip():
        uid = interaction.user.id
    else:
        if not is_admin("discord", interaction.user.id):
            await interaction.response.send_message("No permission", ephemeral=True)
            return
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
    await announce_unverified_user(uid)

@bot.tree.command(name="shadow-ban", description="hide user's messages from relay")
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

async def _whois_lookup(interaction: discord.Interaction, target_message: discord.Message | None, replied_id: str | None = None):
    """Общая логика для whois: ищет автора target_message или сообщения с replied_id."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    requester_id = str(interaction.user.id)
    if not rate_limit_ok(("whois", "discord", requester_id), limit=5, window_seconds=60):
        await interaction.response.send_message("Too many requests — try again later", ephemeral=True)
        return

    if not (
        is_admin("discord", interaction.user.id)
        or db.is_user_verified("discord", requester_id, str(interaction.guild_id))
    ):
        await interaction.response.send_message(
            localized_whois("not_verified", lang), ephemeral=True
        )
        return

    def _find_origin_row(message_id_platform: str | None):
        if not message_id_platform:
            return None
        return db.cur.execute(
            "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
            ("discord", chat_key, str(message_id_platform))
        ).fetchone()

    row = None
    if target_message:
        row = _find_origin_row(str(target_message.id))
    elif replied_id:
        row = _find_origin_row(replied_id)

    if not row:
        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await interaction.response.send_message(localized_whois("origin_missing", lang), ephemeral=True)
        return

    origin_platform = msg_row["origin_platform"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    try:
        if origin_platform == "discord":
            guild_id, _ = msg_row["origin_chat_id"].split(":")
            guild = bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None

            user_obj = None
            try:
                user_obj = await bot.fetch_user(int(origin_sender_id))
            except Exception:
                user_obj = getattr(member, "user", None)

            nick = member.display_name if member else "—"
            user_name = "—"
            if user_obj:
                user_name = f"{user_obj.name}#{user_obj.discriminator}"
            elif member:
                user_name = f"{member.name}#{member.discriminator}"

            mode_key = str(getattr(member, "status", "offline"))
            if mode_key not in ("online", "idle", "dnd", "offline", "invisible"):
                mode_key = "offline"
            if mode_key == "invisible":
                mode_key = "offline"
            mode = localized_whois(f"mode_{mode_key}", lang)

            custom_status = "—"
            if member:
                try:
                    custom = discord.utils.find(lambda a: isinstance(a, discord.CustomActivity), member.activities or [])
                    if custom and getattr(custom, "name", None):
                        custom_status = custom.name
                except Exception:
                    custom_status = "—"

            avatar_url = None
            banner_url = None
            created_at = "—"
            if user_obj:
                avatar_url = str(user_obj.display_avatar.url) if getattr(user_obj, "display_avatar", None) else None
                banner_url = str(user_obj.banner.url) if getattr(user_obj, "banner", None) else None
                if getattr(user_obj, "created_at", None):
                    created_at = discord.utils.format_dt(user_obj.created_at, style="F")

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick or "—", inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=user_name or "—", inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_status", lang), value=custom_status, inline=False)
            embed.add_field(name=localized_whois("field_mode", lang), value=mode, inline=False)
            embed.add_field(name=localized_whois("field_registered", lang), value=created_at, inline=False)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            if banner_url:
                embed.set_image(url=banner_url)

            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif origin_platform == "telegram":
            from telegram_bot import bot as tg_bot
            prefix = msg_row["origin_chat_id"].split(":", 1)[0]
            try:
                member = await tg_bot.get_chat_member(int(prefix), int(origin_sender_id))
                u = member.user
                nick = u.full_name or (u.first_name or "—")
                username = f"@{u.username}" if u.username else "—"
                full_user = await tg_bot.get_chat(int(origin_sender_id))
                bio = getattr(full_user, "bio", None) or "—"
            except Exception:
                nick, username, bio = "—", "—", "—"

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick, inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=username, inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_bio", lang), value=bio if len(bio) < 1000 else (bio[:997] + "..."), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
    except Exception as e:
        logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
        await interaction.response.send_message(
            localized_whois("fetch_error", lang, error=type(e).__name__), ephemeral=True
        )

@bot.tree.context_menu(name="whois")
async def whois_context_menu(interaction: discord.Interaction, message: discord.Message):
    """Context menu (правая кнопка → Apps → whois): показывает автора пересланного сообщения."""
    await _whois_lookup(interaction, target_message=message)


@bot.tree.command(name="whois", description="info about the message author (reply to a bot relay message)")
async def whois_command(interaction: discord.Interaction):
    """
    Slash-команда /whois. Поскольку Discord не передаёт контекст reply для slash-команд,
    используй лучше context menu: ПКМ на сообщении → Apps → whois.
    """
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    await interaction.response.send_message(
        localized_whois("use_context_menu", lang), ephemeral=True
    )

async def resolve_bridge_admins(bridge_id):
    """Return (discord_admins, telegram_admins) for a bridge's admins, each sorted
    alphabetically. discord_admins items are (uid:int, username:str|None);
    telegram_admins items are display strings (@username or name)."""
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

@bot.tree.command(name="allow-bots", description="allow or block relay of bot messages")
async def allow_bots_command(interaction: discord.Interaction, action: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message("No permission", ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_allow_bots(chat_id, True)
        await interaction.response.send_message("Bot and webhook messages will now be relayed from this channel", ephemeral=True)
    elif action == "disable":
        db.set_allow_bots(chat_id, False)
        await interaction.response.send_message("Bot and webhook messages will no longer be relayed from this channel", ephemeral=True)
    else:
        await interaction.response.send_message("Usage: /allow-bots enable | /allow-bots disable", ephemeral=True)

@bot.tree.command(name="webhooks", description="show relayed messages as webhooks (sender avatar and name)")
@app_commands.describe(action="enable or disable")
async def webhooks_command(interaction: discord.Interaction, action: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    if isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message(localized("webhooks_thread_error", lang), ephemeral=True)
        return

    action = action.strip().lower()
    if action == "enable":
        db.set_webhooks_enabled(chat_id, True)
        await interaction.response.send_message(localized("webhooks_enabled", lang), ephemeral=True)
    elif action == "disable":
        db.set_webhooks_enabled(chat_id, False)
        await interaction.response.send_message(localized("webhooks_disabled", lang), ephemeral=True)
    else:
        await interaction.response.send_message(localized("webhooks_usage", lang), ephemeral=True)

async def post_loc_suggestion(*, lang, key, suggestion, code, ui_lang, username, user_id, avatar_url=None):
    """Post a localization suggestion to the Discord and Telegram support chat(s)."""
    body = localized("loc_suggest_support_body", ui_lang,
                     suggestion=suggestion, name=language_name(lang), lang=lang, key=key)
    footer = f"{username} │ ID: {user_id} │ {code}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        embed = discord.Embed(description=body)
        embed.set_footer(text=footer, icon_url=avatar_url)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), f"{body}\n\n{footer}",
                    message_thread_id=int(thread) or None
                )
            except Exception:
                pass

async def post_loc_reply(*, admin, code, ui_lang, title, body):
    """Publish an admin's /loc-reply to the support chat(s)."""
    prefix = localized("loc_reply_support_prefix", ui_lang, admin=admin, code=code)
    text = f"{prefix}\n\n{body}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        try:
            await channel.send(embed=discord.Embed(title=title, description=text))
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), text, message_thread_id=int(thread) or None
                )
            except Exception:
                pass

@bot.tree.command(name="locale", description="localization status, or a language's file")
@app_commands.describe(lang="Language code (optional). With a code, sends that language's localization file.")
async def locale_cmd(interaction: discord.Interaction, lang: str = None):
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")

    if not lang or not lang.strip():
        lines = [localized("loc_list_header", ui_lang)]
        for code in available_locales():
            st = locale_stats(code)
            lines.append(f"{language_name(code)} (`{code}`): {locale_bar(code)} {st['percent']}%")
        lines.append("")
        lines.append(localized("loc_list_footer", ui_lang))
        await interaction.response.send_message("\n".join(lines))
        return

    code = lang.strip().lower()
    if code not in available_locales():
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=code, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return

    if not rate_limit_ok(("locale-file", "discord", interaction.guild_id or interaction.user.id),
                         limit=1, window_seconds=600):
        await interaction.response.send_message(localized("loc_cooldown", ui_lang), ephemeral=True)
        return

    path = os.path.join(os.path.dirname(utils.__file__), "i18n", f"{code}.json")
    st = locale_stats(code)
    caption = localized("loc_file_caption", ui_lang, name=language_name(code), code=code, percent=st["percent"])
    try:
        await interaction.response.send_message(caption, file=discord.File(path, filename=f"{code}.json"))
    except Exception:
        await interaction.response.send_message(caption, ephemeral=True)

@bot.tree.command(name="loc-compare", description="compare a reply across languages")
@app_commands.describe(key="Reply code (as shown in the localization file)")
async def loc_compare_cmd(interaction: discord.Interaction, key: str):
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    key = key.strip()
    data = compare_reply(key)
    if data is None:
        await interaction.response.send_message(localized("loc_compare_not_found", ui_lang, key=key), ephemeral=True)
        return

    lines = [localized("loc_compare_header", ui_lang, key=key)]
    for code in LANG_ORDER:
        if code not in data:
            continue
        status, text = data[code]
        emoji = LOCALE_STATUS_EMOJI.get(status, "")
        if text is None:
            shown = localized("loc_compare_untranslated", ui_lang)
        else:
            shown = str(text)
            if len(shown) > 300:
                shown = shown[:297] + "..."
        lines.append(f"{emoji} {language_name(code)}: {shown}")
    msg = "\n".join(lines)
    if len(msg) > 1990:
        msg = msg[:1990]
    await interaction.response.send_message(msg)

@bot.tree.command(name="loc-suggest", description="suggest a localization")
@app_commands.describe(language="Language code", code="Reply code", text="Suggested text")
async def loc_suggest_cmd(interaction: discord.Interaction, language: str, code: str, text: str):
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    language = language.strip().lower()
    if language not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=language, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return
    if not SUPPORT_CHATS.get("discord") and not SUPPORT_CHATS.get("telegram"):
        await interaction.response.send_message(localized("loc_suggest_no_support", ui_lang), ephemeral=True)
        return

    msg_code = secrets.token_hex(4)
    db.add_loc_suggestion(msg_code, "discord", interaction.user.id, str(interaction.user),
                          language, code.strip(), text, ui_lang)
    avatar_url = None
    try:
        avatar_url = interaction.user.display_avatar.url
    except Exception:
        avatar_url = None
    await post_loc_suggestion(lang=language, key=code.strip(), suggestion=text, code=msg_code,
                              ui_lang=ui_lang, username=str(interaction.user),
                              user_id=interaction.user.id, avatar_url=avatar_url)
    await interaction.response.send_message(localized("loc_suggest_confirm", ui_lang, code=msg_code), ephemeral=True)

@bot.tree.command(name="loc-reply", description="reply to a localization suggestion (bot admins)")
@app_commands.describe(code="Message code from the suggestion", text="Reply text")
async def loc_reply_cmd(interaction: discord.Interaction, code: str, text: str):
    ui_lang_cmd = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", ui_lang_cmd), ephemeral=True)
        return

    row = db.get_loc_suggestion(code.strip())
    if not row:
        await interaction.response.send_message(localized("loc_reply_not_found", ui_lang_cmd, code=code), ephemeral=True)
        return

    ui_lang = row["ui_lang"] or DEFAULT_LANG
    title = localized("loc_reply_dm_title", ui_lang)
    body = localized("loc_reply_dm_body", ui_lang,
                     suggestion=row["suggestion"], reply=text,
                     name=language_name(row["lang"]), lang=row["lang"], key=row["rkey"])

    ok = False
    if row["platform"] == "discord":
        try:
            user = await bot.fetch_user(int(row["user_id"]))
            await user.send(embed=discord.Embed(title=title, description=body))
            ok = True
        except Exception:
            ok = False
    elif row["platform"] == "telegram":
        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.send_message(int(row["user_id"]), f"{title}\n\n{body}")
            ok = True
        except Exception:
            ok = False

    await post_loc_reply(admin=str(interaction.user), code=code.strip(),
                         ui_lang=ui_lang, title=title, body=body)

    if ok:
        db.delete_loc_suggestion(code.strip())
        await interaction.response.send_message(localized("loc_reply_sent", ui_lang_cmd), ephemeral=True)
    else:
        await interaction.response.send_message(localized("loc_reply_failed", ui_lang_cmd), ephemeral=True)

POLL_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def _poll_emoji(idx):
    return POLL_NUMBER_EMOJI[idx] if idx < len(POLL_NUMBER_EMOJI) else f"{idx + 1}."

def _poll_start_text_discord(question, options, ends_at, lang):
    lines = [f"📊 **{question}**", f"-# {localized('poll_anonymous', lang)}", ""]
    for i, opt in enumerate(options):
        lines.append(f"{_poll_emoji(i)} {opt}")
    lines.append("")
    lines.append(localized("poll_ends", lang, ends=f"<t:{ends_at}:R>"))
    return "\n".join(lines)

def _poll_relay_header(origin_platform, place, nick, target_platform):
    """First line shown when a poll is relayed to other chats — like a forwarded
    message header (normal text). Markdown in the names is escaped on Discord."""
    messenger = "Discord" if origin_platform == "discord" else "Telegram"
    if target_platform == "discord":
        return f"[{_esc_md(messenger)} | {_esc_md(place or '')}] {_esc_md(nick or '')}"
    return f"[{messenger} | {place or ''}] {nick or ''}"

def _format_poll_results(question, options, counts, total, lang):
    lines = [localized("poll_results_header", lang, question=question), ""]
    for i, opt in enumerate(options):
        c = counts[i]
        pct = round(c / total * 100) if total else 0
        lines.append(f"{_poll_emoji(i)} {opt} — {c} ({pct}%)")
    lines.append("")
    lines.append(localized("poll_total_votes", lang, total=total))
    return "\n".join(lines)

class PollButton(discord.ui.Button):
    def __init__(self, poll_id, idx, option):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=clip_text(option, 80) or str(idx + 1),
            emoji=(POLL_NUMBER_EMOJI[idx] if idx < len(POLL_NUMBER_EMOJI) else None),
            custom_id=f"poll:{poll_id}:{idx}",
        )
        self.poll_id = poll_id
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await handle_discord_poll_vote(interaction, self.poll_id, self.idx)

class PollView(discord.ui.View):
    def __init__(self, poll_id, options):
        super().__init__(timeout=None)
        for idx, opt in enumerate(options):
            self.add_item(PollButton(poll_id, idx, opt))

async def handle_discord_poll_vote(interaction: discord.Interaction, poll_id, idx):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"
    poll = db.get_poll(poll_id)
    if not poll or poll["closed"] or (poll["ends_at"] and poll["ends_at"] <= int(time.time())):
        await interaction.response.send_message(localized("poll_closed", lang), ephemeral=True)
        return
    user_id = str(interaction.user.id)
    if not db.is_user_verified("discord", user_id, str(interaction.guild_id)):
        await interaction.response.send_message(localized("poll_not_verified", lang), ephemeral=True)
        return
    db.record_poll_vote(poll_id, "discord", user_id, idx)
    await interaction.response.send_message(localized("poll_vote_recorded", lang), ephemeral=True)

async def publish_poll(poll_id, bridge_id, question, options, ends_at, *,
                       origin_chat_id, origin_platform, origin_place, origin_nick,
                       skip_chat_id=None):
    """Post the interactive poll message to every chat in the bridge. The origin
    chat gets no header; every other chat is prefixed with a forwarded-message
    header showing the origin platform, community and creator. `skip_chat_id` is
    skipped entirely (used when the origin Discord message is the command response)."""
    try:
        from telegram_bot import bot as tg_bot, build_poll_keyboard, poll_start_text_telegram
    except Exception:
        tg_bot = None

    for chat in db.get_bridge_chats(bridge_id):
        if skip_chat_id and chat["chat_id"] == skip_chat_id:
            continue
        lang = get_chat_lang(chat["chat_id"]) or "en"
        is_origin = chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id
        header = None if is_origin else _poll_relay_header(origin_platform, origin_place, origin_nick, chat["platform"])

        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if channel is None:
                continue
            body = _poll_start_text_discord(question, options, ends_at, lang)
            content = body if header is None else f"{header}\n{body}"
            try:
                msg = await channel.send(
                    content, view=PollView(poll_id, options),
                    allowed_mentions=RELAY_ALLOWED_MENTIONS,
                )
                db.add_poll_message(poll_id, "discord", chat["chat_id"], msg.id)
            except Exception as e:
                logger.warning("poll post to discord %s failed: %s", chat["chat_id"], e)
        elif chat["platform"] == "telegram" and tg_bot is not None:
            try:
                tg_chat_id, thread = chat["chat_id"].split(":")
                body = poll_start_text_telegram(question, options, ends_at, lang)
                text = body if header is None else f"{header}\n{body}"
                sent = await tg_bot.send_message(
                    int(tg_chat_id), text,
                    message_thread_id=int(thread) or None,
                    reply_markup=build_poll_keyboard(poll_id, options),
                )
                db.add_poll_message(poll_id, "telegram", chat["chat_id"], sent.message_id)
            except Exception as e:
                logger.warning("poll post to telegram %s failed: %s", chat["chat_id"], e)

async def post_poll_results(poll_id):
    poll = db.get_poll(poll_id)
    if not poll:
        return
    options = json.loads(poll["options"])
    counts = db.get_poll_results(poll_id, len(options))
    total = sum(counts)
    starts = {(m["platform"], m["chat_id"]): m["message_id"] for m in db.get_poll_messages(poll_id)}
    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None

    for chat in db.get_bridge_chats(poll["bridge_id"]):
        lang = get_chat_lang(chat["chat_id"]) or "en"
        text = _format_poll_results(poll["question"], options, counts, total, lang)
        start_mid = starts.get((chat["platform"], chat["chat_id"]))
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if channel is None:
                continue
            send_kwargs = {"allowed_mentions": RELAY_ALLOWED_MENTIONS}
            if start_mid:
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=int(start_mid), channel_id=channel_id, fail_if_not_exists=False
                )
            try:
                await channel.send(clip_text(text, DISCORD_MSG_LIMIT), **send_kwargs)
            except Exception:
                pass
        elif chat["platform"] == "telegram" and tg_bot is not None:
            tg_chat_id, thread = chat["chat_id"].split(":")
            kw = dict(chat_id=int(tg_chat_id), message_thread_id=int(thread) or None, text=text)
            if start_mid:
                kw["reply_to_message_id"] = int(start_mid)
            try:
                await tg_bot.send_message(**kw)
            except Exception:
                kw.pop("reply_to_message_id", None)
                try:
                    await tg_bot.send_message(**kw)
                except Exception:
                    pass

async def close_and_delete_poll(poll_id):
    """Close a poll and delete its message in every chat (triggered when a copy is deleted)."""
    poll = db.get_poll(poll_id)
    if not poll:
        return
    db.close_poll(poll_id)
    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    for m in db.get_poll_messages(poll_id):
        try:
            if m["platform"] == "discord":
                channel_id = int(m["chat_id"].split(":")[1])
                ch = bot.get_channel(channel_id)
                if ch is None:
                    ch = await bot.fetch_channel(channel_id)
                msg = await ch.fetch_message(int(m["message_id"]))
                await msg.delete()
            elif m["platform"] == "telegram" and tg_bot is not None:
                tg_chat_id, _ = m["chat_id"].split(":")
                await tg_bot.delete_message(int(tg_chat_id), int(m["message_id"]))
        except Exception:
            pass
    db.delete_poll(poll_id)

@bot.tree.command(name="poll", description="anonymous poll across all bridge chats")
@app_commands.describe(
    text="Poll question",
    duration="Duration: 1h, 2d, … (max 30 days)",
    option1="Option 1", option2="Option 2",
    option3="Option 3 (optional)", option4="Option 4 (optional)", option5="Option 5 (optional)",
)
async def poll_cmd(interaction: discord.Interaction, text: str, duration: str,
                   option1: str, option2: str, option3: str = None,
                   option4: str = None, option5: str = None):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key) or "en"

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await interaction.response.send_message(localized("poll_not_in_bridge", lang), ephemeral=True)
        return
    bridge_id = row["bridge_id"]

    from utils import parse_poll_duration
    try:
        seconds = parse_poll_duration(duration)
    except ValueError:
        await interaction.response.send_message(localized("poll_duration_invalid", lang), ephemeral=True)
        return

    options = [o.strip() for o in (option1, option2, option3, option4, option5) if o and o.strip()]
    if len(options) < 2:
        await interaction.response.send_message(localized("poll_too_few", lang), ephemeral=True)
        return

    ends_at = int(time.time()) + seconds
    poll_id = db.create_poll(bridge_id, text.strip(), json.dumps(options, ensure_ascii=False), ends_at)

    await interaction.response.send_message(
        _poll_start_text_discord(text.strip(), options, ends_at, lang),
        view=PollView(poll_id, options),
        allowed_mentions=RELAY_ALLOWED_MENTIONS,
    )
    try:
        origin_msg = await interaction.original_response()
        db.add_poll_message(poll_id, "discord", chat_key, origin_msg.id)
    except Exception:
        pass

    place = interaction.guild.name if interaction.guild else "Discord"
    nick = interaction.user.display_name
    await publish_poll(
        poll_id, bridge_id, text.strip(), options, ends_at,
        origin_chat_id=chat_key, origin_platform="discord",
        origin_place=place, origin_nick=nick, skip_chat_id=chat_key,
    )

@bot.tree.command(name="help", description="show this command list")
async def help_command(interaction: discord.Interaction):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")

    everyone_lines = "\n".join([
        localized_help("cmd_bridge", lang),
        localized_help("cmd_whois", lang),
        localized_help("cmd_verify", lang),
        localized_help("cmd_poll", lang),
        localized_help("cmd_locale", lang),
        localized_help("cmd_loc_compare", lang),
        localized_help("cmd_loc_suggest", lang),
        localized_help("cmd_help", lang),
    ])

    admins_lines = "\n".join([
        localized_help("cmd_rfb", lang),
        localized_help("cmd_setadmin", lang),
        localized_help("cmd_lang", lang),
        localized_help("cmd_remindrules", lang),
        localized_help("cmd_shadowban", lang),
        localized_help("cmd_allow_bots", lang),
        localized_help("cmd_webhooks", lang),
        localized_help("cmd_deadtopic", lang),
        localized_help("cmd_deadchat", lang),
        localized_help("cmd_newschat", lang),
    ])

    bot_admins_lines = "\n".join([
        localized_help("cmd_atb", lang),
        localized_help("cmd_remadmin", lang),
        localized_help("cmd_unverify", lang),
        localized_help("cmd_list_chats", lang),
        localized_help("cmd_force_leave", lang),
        localized_help("cmd_backup", lang),
        localized_help("cmd_loc_reply", lang),
    ])

    embed = discord.Embed(
        title=localized_help("title", lang),
        color=discord.Color.blurple()
    )
    embed.add_field(name=localized_help("section_everyone", lang), value=everyone_lines, inline=False)
    embed.add_field(name=localized_help("section_admins", lang), value=admins_lines, inline=False)
    embed.add_field(name=localized_help("section_bot_admins", lang), value=bot_admins_lines, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="backup", description="get a database backup (bot admins)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def backup_discord_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        import io
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
