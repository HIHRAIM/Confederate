import discord
from discord import app_commands
from discord.utils import get
import db, message_relay, bluesky, youtube
import utils
from utils import (
    is_admin, extract_username_from_bot_message, is_chat_admin, set_chat_lang,
    get_next_status_text, get_chat_lang, rate_limit_ok, feed_scope_name,
    FEED_REQUEST_HEADERS, FEED_REQUEST_TIMEOUT, FeedError, feed_media_name,
    localized_bridge_join, localized_bridge_leave, localized_bot_joined,
    localized_consent_title, localized_consent_body, localized_consent_button,
    localized_sticker, localized_discord_system_event, localized_whois,
    localized_bridge_info, localized_deadtopic, localized_help,
    localized, language_name, available_locales, locale_stats, locale_bar,
    compare_reply, LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS, DEFAULT_LANG,
)
from config import (
    SUPPORT_CHATS, GALLERY,
    PURGATORIUM_GUILD_ID, PURGATORIUM_INVITE_URL, APPEAL_CHANNEL_ID, APPEAL_PARDON_CHANNELS,
    APPEAL_BANINFO_CHANNELS, CONSULS, GUARD_BOT_ID,
)
import time
import asyncio
import datetime
import io
import aiohttp
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

def _locale_to_lang(locale):
    """Map a Discord interaction locale (e.g. 'ru', 'en-US') to a supported
    bot language. The appeal system can't know which server banned the user
    (that is guard_bot's database), so the user's own client language is the
    best signal for how to talk to them."""
    code = str(locale or "").lower()[:2]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG

def _appeal_thread_chat_id(thread_id):
    return f"{PURGATORIUM_GUILD_ID}:{thread_id}"

def _appeal_thread_lang(thread_id):
    return get_chat_lang(_appeal_thread_chat_id(thread_id)) or DEFAULT_LANG

def _consul_label(thread_id, consul_user_id):
    """How a server member writing in an appeal thread is signed in the copy the
    appellant receives: their `/setname` alias when they have one, otherwise the
    stable anonymized 'Consul A', 'Consul B', …

    Neither form is localized. An alias is a fixed string by design, and the
    anonymous signature is deliberately built from the English wording and the
    Latin alphabet whatever the appellant reads the rest of the bridge in: it is
    a label identifying one person across a thread, not a sentence, and a
    consul who appears as 'Consul B' to one appellant and 'Консул Б' to another
    is harder to talk about than one who is 'Consul B' to everybody.

    The per-thread index is reserved in either case: a consul who later drops
    their alias must fall back to the same letter they would have had, instead
    of reading to the appellant as one more person joining the thread."""
    idx = db.get_consul_ord(str(thread_id), consul_user_id)
    alias = db.get_consul_name(consul_user_id)
    if alias:
        return alias
    letters = localized("appeal_consul_letters", DEFAULT_LANG)
    letter = letters[idx] if isinstance(letters, str) and idx < len(letters) else str(idx + 1)
    return localized("appeal_consul_name", DEFAULT_LANG, letter=letter)

CONSUL_NAME_MAX_LEN = 32
_CONSUL_PING_RE = re.compile(r"@everyone|@here|<@[!&]?\d+>")
_CONSUL_MARKUP_RE = re.compile(r"[*_~`|\\<>]")

def _normalize_consul_name(name):
    """Comparison form of an alias: case-folded, inner whitespace collapsed, so
    that 'Ivan' and 'ivan ' are one and the same name."""
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()

def _reserved_consul_labels():
    """Every anonymized signature the bot itself can produce, in all supported
    languages — an alias must not be able to impersonate one of them (nor a
    consul in another language: appellants read different locales)."""
    reserved = set()
    for lang in SUPPORTED_LANGS:
        letters = localized("appeal_consul_letters", lang)
        marks = list(letters) if isinstance(letters, str) else []
        marks += [str(n) for n in range(1, 100)]
        for mark in marks:
            reserved.add(_normalize_consul_name(localized("appeal_consul_name", lang, letter=mark)))
    return reserved

def _clean_consul_name(raw):
    """Validate a `/setname` alias.

    Returns ``(display, error_key)``. An empty ``display`` with no error means a
    reset. Pings and the characters that could forge a relay header are removed
    rather than escaped: the alias is re-escaped downstream by the relay's own
    header builder, so storing an escaped form would show the backslashes."""
    text = _CONSUL_PING_RE.sub("", str(raw or ""))
    text = _CONSUL_MARKUP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "", None
    if len(text) > CONSUL_NAME_MAX_LEN:
        return None, "setname_too_long"
    if _normalize_consul_name(text) in _reserved_consul_labels():
        return None, "setname_reserved"
    return text, None

async def _is_consul_user(user_id):
    """Whether a user may hold a consul alias: a CONSULS role holder on
    Purgatorium, or a bot admin. Unlike `_can_judge_appeals`, which inspects the
    member who ran a command, this resolves an arbitrary user id against
    Purgatorium's member list."""
    if is_admin("discord", user_id):
        return True
    guild = bot.get_guild(PURGATORIUM_GUILD_ID)
    if guild is None:
        return False
    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            return False
    return any(role.id in CONSULS for role in getattr(member, "roles", None) or [])

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
    "dc-user-green.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1530997027687759912/dc-user-green.png?ex=6a679b97&is=6a664a17&hm=fb8ab5f0cb4c06ae28e587cd705411c83caac3de52950be7a1a870065769f65b&",
    "dc-user-yellow.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1530997027113013268/dc-user-yellow.png?ex=6a679b97&is=6a664a17&hm=9cbf2a1d717b86b830213261df4730b64177a6f3986861af3067dad79348d52a&",
    "dc-user-red.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1530997028212183050/dc-user-red.png?ex=6a679b98&is=6a664a18&hm=a89e920798df74f9e2245d97decc32552cf7b9a54cd0e0483b784aed5e5b0962&",
    "dc-user-grey.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1530997027939291298/dc-user-grey.png?ex=6a679b98&is=6a664a18&hm=0c5c29fdd253ef70a372ad0f27b9d3431a30b132daff98e2c3035e8e72e7428d&",
    "dc-user-blue.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1530997027390099496/dc-user-blue.png?ex=6a679b97&is=6a664a17&hm=aef87205bef54db718c5e47ce0ab86e1b8a9e0828a1e2a39eed775be1e50a764&",
    "user-bsky.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1532701711175647242/user-bsky.png?ex=6a6dcf34&is=6a6c7db4&hm=6b66f092fd80685e7f9fda2c4037b91b338edf14ad41e0060ef825bd5d2d21d7&",
    "user-yt.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1532701711481962647/user-yt.png?ex=6a6dcf34&is=6a6c7db4&hm=8484ab463794c673a832946a227ad284fa3742a1f1169f30080f16d9bc220cbe&",
    "channel.png": "https://cdn.discordapp.com/attachments/1476645334904995860/1531203664310698024/channel.png?ex=6a685c09&is=6a670a89&hm=7773ca12eb69c1ece71a53683ef04b2e38accfa1b825dc0989aadd8456919a4b&",
}
_avatar_url_cache = {}
_AVATAR_URL_TTL = 12 * 3600

async def _find_avatar_asset_message(asset):
    """Locate an asset's host message by attachment filename and remember its id.

    Assets uploaded in one go share a single message, so their ids can't all be
    listed by hand; the host channel is scanned once and the answer cached in
    ``_AVATAR_ASSET_MESSAGES`` alongside the hand-written ones."""
    try:
        ch = bot.get_channel(_AVATAR_HOST_CHANNEL) or await bot.fetch_channel(_AVATAR_HOST_CHANNEL)
        async for msg in ch.history(limit=300):
            if any(a.filename == asset for a in msg.attachments):
                _AVATAR_ASSET_MESSAGES[asset] = msg.id
                return msg
    except Exception as e:
        logger.warning("avatar asset lookup failed (%s): %s", asset, e)
    return None

async def warm_feed_avatars():
    """Resolve the avatars of the followed sources once, before the first post
    needs them.

    Their literal fallback URLs carry a signature that expires within days, so
    the host-message lookup is what keeps them working — this makes a failure of
    that lookup visible in the log instead of showing up as posts silently
    losing their avatar."""
    for asset in dict.fromkeys(spec["avatar_asset"] for spec in FEED_KINDS.values()):
        url = await avatar_asset_url(asset)
        if asset in _AVATAR_ASSET_MESSAGES:
            logger.info("Feed avatar %s resolved from its host message", asset)
        else:
            logger.warning(
                "Feed avatar %s could not be found in channel %s — falling back to a "
                "stored link, which stops working once its signature expires",
                asset, _AVATAR_HOST_CHANNEL,
            )
        if not url:
            logger.warning("Feed avatar %s has no usable URL at all", asset)

async def avatar_asset_url(asset):
    """Fresh Discord CDN URL for a bundled avatar asset, fetched from its host
    message (signature refreshed on each fetch) and cached. Falls back to the
    literal signed URL if the live fetch fails."""
    now = time.time()
    cached = _avatar_url_cache.get(asset)
    if cached and now - cached[1] < _AVATAR_URL_TTL:
        return cached[0]

    url = None
    msg = None
    msg_id = _AVATAR_ASSET_MESSAGES.get(asset)
    if msg_id:
        try:
            ch = bot.get_channel(_AVATAR_HOST_CHANNEL) or await bot.fetch_channel(_AVATAR_HOST_CHANNEL)
            msg = await ch.fetch_message(msg_id)
        except Exception as e:
            logger.warning("avatar asset fetch failed (%s): %s", asset, e)
            msg = None
    if msg is None:
        msg = await _find_avatar_asset_message(asset)
    if msg is not None and msg.attachments:
        match = discord.utils.find(lambda a: a.filename == asset, msg.attachments)
        url = (match or msg.attachments[0]).url
    if url is None:
        url = _AVATAR_ASSET_URLS.get(asset)
    if url:
        _avatar_url_cache[asset] = (url, now)
    return url

_DC_AVATAR_ASSETS = {
    1: "dc-user-green.png", 2: "dc-user-green.png",
    3: "dc-user-yellow.png", 4: "dc-user-yellow.png",
    5: "dc-user-red.png", 6: "dc-user-red.png",
    7: "dc-user-grey.png", 8: "dc-user-grey.png",
    9: "dc-user-blue.png", 0: "dc-user-blue.png",
}

async def relay_avatar_url(user_id, avatar_url):
    """The avatar a Discord sender's webhook copies should carry. Senders who
    hid it in `/privacy` get a neutral placeholder chosen by the last digit of
    their ID — the same rule that gives Telegram senders theirs."""
    if not user_id or not avatar_url:
        return avatar_url
    if not db.get_privacy_flag("discord", user_id, "hide_avatar"):
        return avatar_url
    try:
        asset = _DC_AVATAR_ASSETS.get(int(user_id) % 10)
    except Exception:
        return None
    return await avatar_asset_url(asset) if asset else None

GALLERY_MAX_FILES = 10
GALLERY_DEFAULT_SIZE_LIMIT = 10 * 1024 * 1024

async def resolve_gallery_channel():
    """First GALLERY channel the bot can actually post files into, or None."""
    for cid in GALLERY:
        try:
            channel = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
        except Exception:
            continue
        if channel is None:
            continue
        guild = getattr(channel, "guild", None)
        me = guild.me if guild is not None else None
        if me is not None:
            try:
                perms = channel.permissions_for(me)
            except Exception:
                continue
            if not (perms.send_messages and perms.attach_files):
                continue
        return channel
    return None

def _gallery_size_limit(channel):
    """Upload limit of the gallery's server — 10 MB unless it is boosted."""
    guild = getattr(channel, "guild", None)
    limit = getattr(guild, "filesize_limit", None) if guild is not None else None
    return int(limit) if limit else GALLERY_DEFAULT_SIZE_LIMIT

async def gallery_upload_budget():
    """``(max_files, max_total_bytes)`` for one GALLERY message, or None when no
    gallery channel is reachable. Callers use it to decide which files fit
    before spending time downloading them."""
    channel = await resolve_gallery_channel()
    if channel is None:
        return None
    return GALLERY_MAX_FILES, _gallery_size_limit(channel)

async def gallery_upload(files):
    """Post ``files`` (``{"name", "data"}`` dicts) to GALLERY as a single message
    and return ``{"channel_id", "message_id", "urls"}``.

    Returns None on any failure — an unreachable channel, a missing permission,
    a rejected upload — so that the caller falls back to the marker/link footer
    instead of losing the message."""
    if not files:
        return None
    channel = await resolve_gallery_channel()
    if channel is None:
        logger.warning("GALLERY re-upload skipped: no reachable gallery channel")
        return None
    try:
        sent = await channel.send(files=[
            discord.File(io.BytesIO(f["data"]), filename=f["name"]) for f in files
        ])
    except Exception as e:
        logger.warning("GALLERY upload failed (channel=%s): %s", channel.id, e)
        return None
    return {
        "channel_id": str(channel.id),
        "message_id": str(sent.id),
        "urls": [a.url for a in sent.attachments],
    }

async def delete_gallery_message(channel_id, gallery_message_id):
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(gallery_message_id))
        await msg.delete()
        return True
    except Exception:
        return False

async def drop_gallery_upload(message_id):
    """Remove a relayed message's GALLERY upload, message and row alike."""
    row = db.get_gallery_upload(message_id)
    if not row:
        return
    await delete_gallery_message(row["channel_id"], row["gallery_message_id"])
    db.delete_gallery_upload(message_id)

FEED_KINDS = {
    "bluesky": {
        "messenger": "Bluesky",
        "avatar_asset": "user-bsky.png",
        "file_source": "bluesky",
        "stale_after": 14 * 86400,
    },
    "youtube": {
        "messenger": "YouTube",
        "avatar_asset": "user-yt.png",
        "file_source": "youtube",
        "stale_after": 120 * 86400,
    },
    "telegram": {
        "messenger": "Telegram",
        "avatar_asset": "channel.png",
        "file_source": "telegram",
        "stale_after": 14 * 86400,
    },
}

FEED_STALE_AFTER = 14 * 86400

def feed_stale_since(posts, kind=None):
    """The date of a fetched feed's newest post, when it is old enough that the
    source has plainly stopped moving — otherwise ``None``.

    What counts as "stopped" depends on what is being followed, so each kind
    names its own patience in `FEED_KINDS`: a news account quiet for a fortnight
    has probably gone somewhere else, while a video channel quiet for a fortnight
    is merely between uploads. Reported once a day at most, this is what keeps a
    feed that has nothing left to relay from going silent with nothing in the log
    to explain it."""
    created = (posts[-1] if posts else {}).get("created_at")
    after = (FEED_KINDS.get(kind) or {}).get("stale_after") or FEED_STALE_AFTER
    if not created or time.time() - int(created) < after:
        return None
    return datetime.datetime.fromtimestamp(
        int(created), tz=datetime.timezone.utc).strftime("%Y-%m-%d")

def feed_module(kind):
    """The module that reads one kind of followed source. Telegram channels are
    read by the Telegram half of the bot, imported on use like every other call
    that crosses between the two halves."""
    if kind == "bluesky":
        return bluesky
    if kind == "youtube":
        return youtube
    if kind == "telegram":
        import telegram_bot
        return telegram_bot
    raise KeyError(kind)

async def download_feed_media(items, max_files=None, max_total_bytes=None, headers=None):
    """Download a followed post's attachments for the GALLERY re-upload.

    Returns ``(files, skipped)`` — the ``{"name", "data"}`` dicts that fit, in
    post order, and how many attachments did not make it. Each attachment is
    tried from its largest rendition down, so an oversized video arrives in a
    smaller size instead of not at all; one that fails to download or has no
    rendition small enough is skipped rather than holding up the post."""
    if not items:
        return [], 0
    headers = headers or FEED_REQUEST_HEADERS
    files, total, skipped = [], 0, 0
    async with aiohttp.ClientSession() as session:
        for index, item in enumerate(items):
            if max_files is not None and len(files) >= max_files:
                skipped += 1
                continue
            data, chosen = None, None
            for url in item.get("urls") or [item.get("url")]:
                if not url:
                    continue
                try:
                    async with session.get(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        declared = int(resp.headers.get("Content-Length") or 0)
                        if max_total_bytes is not None and declared and total + declared > max_total_bytes:
                            continue
                        body = await resp.read()
                except Exception:
                    continue
                if max_total_bytes is not None and total + len(body) > max_total_bytes:
                    continue
                data, chosen = body, url
                break
            if data is None:
                skipped += 1
                continue
            files.append({
                "name": feed_media_name(chosen, item.get("index", index),
                                        item.get("kind", "photo"),
                                        prefix=item.get("prefix", "media")),
                "data": data,
            })
            total += len(data)
    return files, skipped

async def upload_feed_media(post):
    """Re-upload a followed post's attachments to GALLERY.

    Returns ``(urls, skipped)`` — the links to hand out in the relayed copies,
    and how many attachments could not be brought over, for which the copies get
    the "[N files from …]" footer instead."""
    items = post.get("media") or []
    if not items:
        return [], 0
    budget = await gallery_upload_budget()
    if budget is None:
        return [], len(items)
    max_files, max_bytes = budget
    files, skipped = await download_feed_media(
        items, max_files=max_files, max_total_bytes=int(max_bytes * 0.95)
    )
    if not files:
        return [], len(items)
    upload = await gallery_upload(files)
    if not upload or not upload.get("urls"):
        return [], len(items)
    return upload["urls"], skipped + max(0, len(files) - len(upload["urls"]))

async def feed_send_to_chat(chat, *, header, body_plain, body_discord, body_telegram_html,
                            reply_line, reply_link_line=None, reply_to_platform_message_id=None,
                            sender_name=None, place_name=None, messenger_name=None,
                            avatar_url=None, is_bot_sender=False):
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
        return await deliver_telegram_relay(
            chat, header=header, body_plain=body_plain,
            body_telegram_html=body_telegram_html, reply_line=reply_line,
            reply_to_platform_message_id=reply_to_platform_message_id,
        )

async def relay_feed_post(feed, post, gallery=None):
    """Relay one post of a followed source into the chats of its feed.

    The post is handled like a forwarded message from a sender with no community
    behind them: the header is ``[Bluesky] Account:`` or ``[Telegram]
    Channel:`` where webhooks are off, and where they are on the copy carries the
    source's name and its own avatar instead. A repost keeps the bridge's usual
    "(forwarded from …)" line above the text. Attachments are re-uploaded to
    GALLERY, so the chats get links the bot controls; ``gallery`` lets a caller
    that already has the files (a live Telegram channel post) pass the result in
    rather than downloading them again."""
    spec = FEED_KINDS[feed["kind"]]
    author = post.get("author_name") or feed["title"] or feed["source"]
    body = post.get("text") or ""
    if post.get("link"):
        body = f"{body}\n{post['link']}".strip()

    gallery_urls, skipped = gallery if gallery is not None else await upload_feed_media(post)
    skipped += int(post.get("unavailable_media") or 0)

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (feed["chat_id"],)
    ).fetchone()

    await message_relay.relay_message(
        bridge_id=row["bridge_id"] if row else None,
        origin_platform=f"feed:{feed['kind']}",
        origin_chat_id=f"{feed['kind']}:{feed['source']}",
        origin_message_id=str(post["id"]),
        origin_sender_id=feed["source"],
        messenger_name=spec["messenger"],
        place_name=None,
        sender_name=author,
        text=body,
        discord_text=body,
        telegram_html=escape_html(body),
        send_to_chat_func=feed_send_to_chat,
        forward_type=post.get("forward_type"),
        forward_name=post.get("forward_name"),
        avatar_url=await avatar_asset_url(spec["avatar_asset"]),
        gallery_urls=gallery_urls,
        targets=db.feed_targets(feed["platform"], feed["chat_id"]),
        file_count=skipped or None,
        file_count_source=spec["file_source"],
    )

_MD_ESCAPE = {c: "\\" + c for c in "\\*_~`|"}

def _esc_md(text):
    """Escape Discord markdown specials so names with * or _ don't format the header."""
    return "".join(_MD_ESCAPE.get(ch, ch) for ch in (text or ""))

_BOT_SENDER_EMOJI = "<:bot:1513502696953352363>"

def _discord_relay_header(messenger_name, place_name, sender_name, is_bot_sender):
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

async def deliver_discord_relay(
    chat, *, header, body_discord, reply_line, reply_to_platform_message_id,
    sender_name=None, place_name=None, messenger_name=None, avatar_url=None,
    is_bot_sender=False, reply_link_line=None,
):
    """Deliver a relayed message into a Discord channel.

    If the channel has /webhooks enabled (and isn't a thread/forum post), the
    message is sent through a per-channel webhook with the sender's name + platform
    + server as the username and the sender's avatar — otherwise it's a normal bot
    message with the usual ``[Messenger | Place] Sender:`` header. Also handles
    'dm:<user_id>' chats (appeal bridges), which never use webhooks.
    """
    channel = await resolve_discord_chat_channel(chat["chat_id"])
    if channel is None:
        return None
    channel_id = channel.id

    body = body_discord
    if reply_line:
        body = f"{reply_line}\n{body}"

    if db.get_webhooks_enabled(chat["chat_id"]) and not isinstance(channel, discord.Thread):
        webhook = await _get_relay_webhook(channel)
        if webhook is not None:
            username = _webhook_username(sender_name, place_name)
            webhook_body = f"{reply_link_line}\n{body}" if reply_link_line else body
            content = clip_text(webhook_body, DISCORD_MSG_LIMIT) or "​"
            for attempt in range(2):
                try:
                    sent = await webhook.send(
                        content, username=username, avatar_url=avatar_url,
                        allowed_mentions=RELAY_ALLOWED_MENTIONS, wait=True,
                    )
                    return str(sent.id)
                except Exception as e:
                    _relay_webhooks.pop(channel.id, None)
                    logger.warning("Relay webhook send failed (channel=%s, try %d): %s",
                                   channel.id, attempt + 1, e)
                    if attempt:
                        break
                    webhook = await _get_relay_webhook(channel)
                    if webhook is None:
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

async def deliver_telegram_relay(
    chat, *, header, body_plain, body_telegram_html, reply_line,
    reply_to_platform_message_id,
):
    """Deliver a relayed message into a Telegram chat/topic.

    Discord-only markup that Telegram can't render (timestamps) is localized
    first; a reply is sent as a native reply where the replied-to copy still
    exists, and retried without the reference when it doesn't."""
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

async def _relay_verified_discord_message(message: discord.Message, bridge_id, system_event_key=None, is_bot_sender=False,
                                          origin_chat_id=None, place_name=None, sender_name=None, avatar_url=None):
    """Relay a Discord message into its bridge.

    The origin/place/sender/avatar default to the guild message's own values;
    the appeal system overrides them to relay from a DM (origin 'dm:<uid>',
    localized place) and to anonymize appeal-thread members as consuls (no
    avatar so nothing about them leaks into the webhook path).
    """
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

    embed_texts = _discord_embed_texts(message)
    if embed_texts:
        embed_block = "\n\n".join(embed_texts)
        if any((t or "").strip() for t in texts):
            texts[0] = (texts[0] or "").rstrip() + "\n\n" + embed_block
        else:
            texts = [embed_block]

    if forward_type and not any((t or "").strip() for t in texts):
        texts = _split_attachment_texts(forward_text or "", forward_attachments)

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
            return await deliver_telegram_relay(
                chat, header=header, body_plain=body_plain,
                body_telegram_html=body_telegram_html, reply_line=reply_line,
                reply_to_platform_message_id=reply_to_platform_message_id,
            )

    if system_event_key:
        origin_lang = get_chat_lang(origin_chat_id)
        event_text = localized_discord_system_event(
            sender_name,
            system_event_key,
            origin_lang,
        )
        await message_relay.relay_message(
            bridge_id=bridge_id,
            origin_platform="discord",
            origin_chat_id=origin_chat_id,
            origin_message_id=str(message.id),
            origin_sender_id=str(message.author.id),
            messenger_name="Discord",
            place_name=place_name,
            sender_name=sender_name,
            text=event_text,
            discord_text=event_text,
            telegram_html=discord_to_telegram_html(event_text),
            reply_to_msg_db_id=None,
            send_to_chat_func=send_to_chat,
            avatar_url=avatar_url,
        )
        return

    for text in texts:
        target_lang = get_chat_lang(origin_chat_id)
        localized_text = text.replace("__DC_STICKER__", localized_sticker(target_lang))
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
            discord_text=localized_text,
            telegram_html=discord_to_telegram_html(telegram_text),
            reply_to_msg_db_id=reply_to_msg_db_id,
            send_to_chat_func=send_to_chat,
            forward_type=forward_type,
            forward_name=forward_name,
            is_bot_sender=is_bot_sender,
            avatar_url=avatar_url,
        )

async def _send_db_backup_discord(client):
    import io
    from config import BACKUP_CHATS
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
        return
    fname = encrypted_filename("bridge.db")
    for channel_id in BACKUP_CHATS.get("discord", set()):
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(channel_id)
                except Exception:
                    logger.warning("Periodic backup: cannot fetch Discord channel %s", channel_id)
                    continue
            if ch:
                await ch.send(file=discord.File(io.BytesIO(data), filename=fname))
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Discord channel %s: %s", channel_id, e)

async def _send_db_backup_telegram():
    from config import BACKUP_CHATS
    from telegram_bot import bot as tg_bot
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
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
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Telegram chat %s: %s", chat_entry, e)

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
        self.loop.create_task(self.appeal_maintenance_loop())
        try:
            for poll in db.get_open_polls():
                self.add_view(PollView(poll["id"], json.loads(poll["options"])))
        except Exception:
            pass
        try:
            for row in db.get_open_appeals():
                lang = get_chat_lang(f"{PURGATORIUM_GUILD_ID}:{row['thread_id']}") or DEFAULT_LANG
                self.add_view(AppealVerdictView(int(row["user_id"]), lang))
        except Exception:
            pass

    async def appeal_maintenance_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await _appeal_maintenance_pass(self)
            except Exception as e:
                logger.warning("appeal maintenance failed: %s", e)
            await asyncio.sleep(24 * 3600)

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

async def _post_user_id_to_channels(channel_ids, payload):
    """Post a verification-sync payload to each of the given Discord channels.

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
            await channel.send(str(payload))
        except Exception as e:
            logger.warning("Failed to publish user id %s to channel %s: %s", payload, cid, e)

async def announce_verified_user(user_id):
    """Publish a newly verified user's ID to the VERIFIED channel(s).

    The message carries exactly one ID — the user's — so consumers can never
    mistake it for anything else.

    Only useful when running alongside Confederate Guard; can be turned off
    with /verify-list disable.
    """
    if not db.is_verify_list_enabled():
        return
    from config import VERIFIED
    await _post_user_id_to_channels(VERIFIED, user_id)

async def announce_unverified_user(user_id):
    """Publish an unverified user's ID to the UNVERIFIED channel(s).

    Only useful when running alongside Confederate Guard; can be turned off
    with /verify-list disable.
    """
    if not db.is_verify_list_enabled():
        return
    from config import UNVERIFIED
    await _post_user_id_to_channels(UNVERIFIED, user_id)

@bot.tree.command(name="atb", description="attach this chat to a bridge (bot admins)")
async def atb(interaction: discord.Interaction, bridge_id: int):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id) or "en"

    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    if db.chat_exists(chat_id):
        await interaction.response.send_message(localized("atb_already_attached", lang), ephemeral=True)
        return

    db.attach_chat("discord", chat_id, bridge_id)

    try:
        await interaction.channel.send(localized_bot_joined(lang))
    except Exception:
        pass

    await interaction.response.send_message(
        localized("atb_attached", lang, bridge_id=bridge_id),
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

async def resolve_telegram_channel(channel):
    """What the Bot API can tell us about a public channel: its numeric id, its
    title, and whether the bot is an administrator of it.

    Membership decides how the channel is read: an admin bot is handed every post
    as it appears, everyone else has to poll the channel's public web preview.
    Returns None when the API knows nothing about the name at all."""
    from telegram_bot import bot as tg_bot
    try:
        chat = await tg_bot.get_chat(f"@{channel}")
    except Exception as e:
        logger.info("Telegram channel lookup failed (%s): %s", channel, e)
        return None
    member = False
    try:
        me = await tg_bot.get_me()
        status = getattr(await tg_bot.get_chat_member(chat.id, me.id), "status", None)
        member = str(status) in ("administrator", "creator", "ChatMemberStatus.ADMINISTRATOR",
                                 "ChatMemberStatus.CREATOR")
    except Exception:
        member = False
    return {"id": chat.id, "title": chat.title or f"@{channel}", "member": member}

async def attach_feed(kind, raw_source, platform, chat_id, added_by):
    """Attach a public source to a chat — and thereby to its whole bridge.

    Returns ``(status, source, title, stale_since)`` where status is 'ok',
    'live' (a Telegram channel the bot is in, whose posts arrive without
    polling), 'exists' (this chat or its bridge already follows the source),
    'invalid' (not a handle of that platform), 'throttled' (the source is
    rate-limiting us — worth retrying shortly) or 'unreachable' (nothing
    readable there). `stale_since` is set when what the source serves has not
    moved for days, which the admin should hear about right away rather than
    discover through silence. The newest post is recorded as already seen, so
    the feed starts with what comes next instead of replaying the backlog into
    every chat."""
    module = feed_module(kind)
    source = module.normalize_source(raw_source)
    if not source:
        return "invalid", None, None, None
    if db.find_feed(kind, source, chat_id):
        return "exists", source, None, None

    source_id = None
    if kind == "telegram":
        resolved = await resolve_telegram_channel(source)
        if resolved:
            source_id = resolved["id"]
            if resolved["member"]:
                db.add_feed(kind, source, platform, chat_id, source_id=source_id,
                            title=resolved["title"], live=True, added_by=added_by)
                return "live", source, resolved["title"], None

    try:
        title, posts = await module.fetch_posts(source)
    except FeedError as e:
        logger.warning("feed attach failed (%s %s): %s", kind, source, e)
        status = "throttled" if getattr(e, "throttled", False) else "unreachable"
        return status, source, None, None

    db.add_feed(kind, source, platform, chat_id, source_id=source_id, title=title,
                last_post_id=posts[-1]["id"] if posts else None, added_by=added_by)
    return "ok", source, title or source, feed_stale_since(posts, kind)

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
        text = localized(keys[status], lang, account=source)
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
    await _feed_command(interaction, "youtube", channel, YTFEED_KEYS)

@bot.tree.command(name="remytfeed", description="stop relaying a YouTube channel into this bridge (bot admins)")
@app_commands.describe(channel="YouTube channel: @handle, a link to the channel or a UC… channel id")
async def remytfeed_cmd(interaction: discord.Interaction, channel: str):
    await _rem_feed_command(interaction, "youtube", channel, YTFEED_KEYS)

@bot.tree.command(name="setbskyfeed", description="relay a public Bluesky account into this bridge (bot admins)")
@app_commands.describe(account="Bluesky account: handle, @handle, a link to the profile or a DID")
async def setbskyfeed_cmd(interaction: discord.Interaction, account: str):
    await _feed_command(interaction, "bluesky", account, BSKYFEED_KEYS)

@bot.tree.command(name="rembskyfeed", description="stop relaying a Bluesky account into this bridge (bot admins)")
@app_commands.describe(account="Bluesky account: handle, @handle, a link to the profile or a DID")
async def rembskyfeed_cmd(interaction: discord.Interaction, account: str):
    await _rem_feed_command(interaction, "bluesky", account, BSKYFEED_KEYS)

@bot.tree.command(name="settgfeed", description="relay a public Telegram channel into this bridge (bot admins)")
@app_commands.describe(channel="Telegram channel: the name after t.me/, @name or a link")
async def settgfeed_cmd(interaction: discord.Interaction, channel: str):
    await _feed_command(interaction, "telegram", channel, TGFEED_KEYS)

@bot.tree.command(name="remtgfeed", description="stop relaying a Telegram channel into this bridge (bot admins)")
@app_commands.describe(channel="Telegram channel: the name after t.me/, @name or a link")
async def remtgfeed_cmd(interaction: discord.Interaction, channel: str):
    await _rem_feed_command(interaction, "telegram", channel, TGFEED_KEYS)

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

APPEAL_AUTO_KICK_SECONDS = 7 * 86400
APPEAL_CLEANUP_SECONDS = 30 * 86400

def _cleanup_appeal_records(appeal_row):
    """Drop an appeal's rows and detach its bridge chats (the bridge row itself
    disappears with its last chat via remove_chat_from_bridge)."""
    if not appeal_row:
        return
    db.remove_chat_from_bridge(f"{PURGATORIUM_GUILD_ID}:{appeal_row['thread_id']}")
    db.remove_chat_from_bridge(f"dm:{appeal_row['user_id']}")
    db.delete_appeal(appeal_row["user_id"])

def _can_judge_appeals(member):
    """Consuls hold one of the CONSULS roles on Purgatorium; global bot admins also count."""
    if is_admin("discord", member.id):
        return True
    return any(role.id in CONSULS for role in getattr(member, "roles", None) or [])

class AppealVerdictButton(discord.ui.Button):
    def __init__(self, action, user_id, lang):
        super().__init__(
            label=localized(f"appeal_btn_{action}", lang),
            style=discord.ButtonStyle.success if action == "pardon" else discord.ButtonStyle.danger,
            custom_id=f"appeal:{action}:{user_id}",
        )
        self.action = action
        self.target_uid = int(user_id)

    async def callback(self, interaction: discord.Interaction):
        await handle_appeal_verdict_click(interaction, self.action, self.target_uid)

class AppealVerdictView(discord.ui.View):
    """Verdict buttons pinned in an appeal thread. Persistent: stable custom_ids,
    re-registered in setup_hook for every open appeal after a restart."""
    def __init__(self, user_id, lang):
        super().__init__(timeout=None)
        self.add_item(AppealVerdictButton("pardon", user_id, lang))
        self.add_item(AppealVerdictButton("condemn", user_id, lang))

class _AppealConfirmView(discord.ui.View):
    """One-button ephemeral confirmation shown before a verdict is executed."""
    def __init__(self, action, target_uid, pinned_message_id, lang):
        super().__init__(timeout=60)
        button = discord.ui.Button(
            label=localized("appeal_confirm_btn", lang),
            style=discord.ButtonStyle.danger,
        )

        async def _confirm(interaction: discord.Interaction):
            await execute_appeal_verdict(interaction, action, target_uid, pinned_message_id)

        button.callback = _confirm
        self.add_item(button)

async def handle_appeal_verdict_click(interaction: discord.Interaction, action, target_uid):
    thread_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG
    if not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", thread_lang), ephemeral=True)
        return
    if not db.get_open_appeal(target_uid):
        await interaction.response.send_message(localized("appeal_already_resolved", thread_lang), ephemeral=True)
        return
    key = "appeal_confirm_pardon" if action == "pardon" else "appeal_confirm_condemn"
    await interaction.response.send_message(
        localized(key, thread_lang),
        view=_AppealConfirmView(action, target_uid, interaction.message.id if interaction.message else None, thread_lang),
        ephemeral=True,
    )

async def execute_appeal_verdict(interaction: discord.Interaction, action, target_uid, pinned_message_id):
    thread_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG
    if not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", thread_lang), ephemeral=True)
        return

    status = "pardoned" if action == "pardon" else "condemned"
    appeal = db.get_open_appeal(target_uid)
    if not appeal or not db.resolve_appeal(target_uid, status, interaction.user.id):
        await interaction.response.send_message(localized("appeal_already_resolved", thread_lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    user_lang = appeal["lang"] if appeal["lang"] in SUPPORTED_LANGS else DEFAULT_LANG
    purg = bot.get_guild(PURGATORIUM_GUILD_ID)

    try:
        user = bot.get_user(target_uid) or await bot.fetch_user(target_uid)
        dm_key = "appeal_user_pardoned" if action == "pardon" else "appeal_user_condemned"
        await user.send(localized(dm_key, user_lang))
    except Exception:
        pass

    if action == "pardon":
        await _post_user_id_to_channels(APPEAL_PARDON_CHANNELS.get("discord", set()), target_uid)
        if purg:
            try:
                await purg.kick(discord.Object(id=target_uid), reason="Appeal granted")
            except Exception:
                pass
    else:
        if purg:
            try:
                await purg.ban(
                    discord.Object(id=target_uid),
                    reason="Appeal denied by consuls",
                    delete_message_days=0,
                )
            except Exception:
                pass

    thread = interaction.channel
    note_key = "appeal_note_pardoned" if action == "pardon" else "appeal_note_condemned"
    try:
        await thread.send(localized(note_key, thread_lang))
    except Exception:
        pass

    if pinned_message_id:
        try:
            pinned = await thread.fetch_message(int(pinned_message_id))
            await pinned.edit(view=None)
        except Exception:
            pass

    try:
        await interaction.followup.send(localized("appeal_verdict_done", thread_lang), ephemeral=True)
    except Exception:
        pass

    try:
        await thread.edit(archived=True, locked=True)
    except Exception:
        pass

@bot.tree.command(name="appeal", description="file a ban appeal (on the appeal server)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def appeal_cmd(interaction: discord.Interaction):
    lang = _locale_to_lang(interaction.locale)
    uid = interaction.user.id

    purg = bot.get_guild(PURGATORIUM_GUILD_ID)
    member = purg.get_member(uid) if purg else None
    if member is None:
        await interaction.response.send_message(
            localized("appeal_not_member", lang, invite=PURGATORIUM_INVITE_URL), ephemeral=True
        )
        return

    if db.get_open_appeal(uid):
        await interaction.response.send_message(localized("appeal_already_open", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(APPEAL_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(APPEAL_CHANNEL_ID)
        except Exception:
            channel = None
    if channel is None:
        await interaction.followup.send(localized("appeal_failed", lang), ephemeral=True)
        return

    try:
        thread = await channel.create_thread(
            name=clean_display_name(interaction.user.display_name or interaction.user.name, max_len=90),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
    except Exception as e:
        logger.warning("appeal thread creation failed (user=%s): %s", uid, e)
        await interaction.followup.send(localized("appeal_failed", lang), ephemeral=True)
        return

    _cleanup_appeal_records(db.get_appeal(uid))

    bridge_id = db.next_appeal_bridge_id()
    db.attach_chat("discord", f"{PURGATORIUM_GUILD_ID}:{thread.id}", bridge_id)
    db.attach_chat("discord", f"dm:{uid}", bridge_id)
    db.create_appeal(uid, thread.id, bridge_id, lang)

    db.add_verified_user("discord", uid, str(PURGATORIUM_GUILD_ID), days_valid=365)

    try:
        await interaction.user.send(localized("appeal_created_dm", lang))
    except Exception:
        _cleanup_appeal_records(db.get_appeal(uid))
        try:
            await thread.delete()
        except Exception:
            pass
        await interaction.followup.send(localized("appeal_dm_closed", lang), ephemeral=True)
        return

    thread_lang = _appeal_thread_lang(thread.id)
    try:
        pinned = await thread.send(
            localized(
                "appeal_thread_info", thread_lang,
                mention=f"<@{uid}>", username=str(interaction.user), id=uid,
            ),
            view=AppealVerdictView(uid, thread_lang),
        )
        try:
            await pinned.pin()
        except Exception:
            pass
    except Exception as e:
        logger.warning("appeal pinned message failed (user=%s): %s", uid, e)

    await _post_user_id_to_channels(
        APPEAL_BANINFO_CHANNELS.get("discord", set()), f"{uid} {thread.id}"
    )

    await interaction.followup.send(localized("appeal_created", lang), ephemeral=True)

@bot.tree.command(name="setname", description="set the alias appellants see instead of 'Consul A' (consuls)")
@app_commands.describe(
    name=f"Alias shown to appellants, up to {CONSUL_NAME_MAX_LEN} characters. Leave empty to go back to 'Consul A'.",
    user="Bot Admins only: whose alias to change (ID or mention)",
)
async def setname_command(interaction: discord.Interaction, name: str = None, user: str = None):
    """Consul aliases. Always answers ephemerally — the command may well be run
    inside an appeal thread, and the appellant must see neither the call nor the
    answer."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG

    if not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    target_id = interaction.user.id
    if user is not None:
        if not is_admin("discord", interaction.user.id):
            await interaction.response.send_message(localized("setname_user_forbidden", lang), ephemeral=True)
            return
        try:
            resolved = await resolve_discord_user(interaction.guild, user)
        except Exception:
            resolved = None
        if resolved is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
        target_id = resolved

    is_other = str(target_id) != str(interaction.user.id)
    display, error_key = _clean_consul_name(name)
    if error_key:
        await interaction.response.send_message(
            localized(error_key, lang, limit=CONSUL_NAME_MAX_LEN), ephemeral=True
        )
        return

    if not display:
        db.remove_consul_name(target_id)
        key = "setname_reset_other" if is_other else "setname_reset"
        await interaction.response.send_message(
            localized(key, lang, user=f"<@{target_id}>"), ephemeral=True
        )
        return

    if not await _is_consul_user(target_id):
        await interaction.response.send_message(localized("setname_target_not_consul", lang), ephemeral=True)
        return

    normalized = _normalize_consul_name(display)
    owner = db.find_consul_name_owner(normalized)
    if owner is not None and str(owner) != str(target_id):
        await interaction.response.send_message(localized("setname_taken", lang), ephemeral=True)
        return

    db.set_consul_name(target_id, display, normalized, set_by=interaction.user.id)
    key = "setname_done_other" if is_other else "setname_done"
    await interaction.response.send_message(
        localized(key, lang, name=display, user=f"<@{target_id}>"), ephemeral=True
    )

async def _appeal_maintenance_pass(client):
    """Daily housekeeping of the appeal system.

    Silently kicks Purgatorium members who have been on the server for over
    7 days without ever filing an appeal (bots, bot admins and anyone holding
    a role — consuls/staff — are exempt; Discord's own joined_at makes this
    restart-proof), and garbage-collects appeals resolved more than 30 days
    ago together with their bridge attachments.
    """
    purg = client.get_guild(PURGATORIUM_GUILD_ID)
    if purg:
        now = discord.utils.utcnow()
        for member in list(purg.members):
            if member.bot:
                continue
            if is_admin("discord", member.id):
                continue
            if len(member.roles) > 1:
                continue
            if member.joined_at is None:
                continue
            if (now - member.joined_at).total_seconds() < APPEAL_AUTO_KICK_SECONDS:
                continue
            if db.has_any_appeal(member.id):
                continue
            try:
                await purg.kick(member, reason="No appeal filed within 7 days")
            except Exception:
                pass

    for row in db.get_resolved_appeals_older_than(APPEAL_CLEANUP_SECONDS):
        _cleanup_appeal_records(row)

@bot.event
async def on_thread_delete(thread):
    row = db.get_appeal_by_thread(str(thread.id))
    if row:
        _cleanup_appeal_records(row)

async def handle_appeal_dm_message(message: discord.Message):
    """DM side of an appeal bridge: relay the appellant's direct messages into
    their appeal thread. DMs from users without an open appeal are ignored."""
    if message.author.bot:
        return
    appeal = db.get_open_appeal(message.author.id)
    if not appeal:
        return
    if db.is_shadow_banned("discord", str(message.author.id)):
        return
    if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping appeal DM from %s", message.author.id)
        return
    thread_lang = _appeal_thread_lang(appeal["thread_id"])
    await _relay_verified_discord_message(
        message, appeal["bridge_id"],
        origin_chat_id=f"dm:{message.author.id}",
        place_name=localized("appeal_dm_place", thread_lang),
    )

async def handle_appeal_thread_message(message: discord.Message, appeal_row, bridge_id):
    """Thread side of an appeal bridge: relay server members' messages to the
    appellant's DM, anonymized as 'Consul A/B/…'. No consent prompt — writing
    in an appeal thread is a moderation duty, not a bridged community chat.
    The appellant's own thread messages (they can see their thread if channel
    permissions allow) are not relayed back to their DM."""
    if appeal_row["status"] != "open":
        return
    if str(message.author.id) == str(appeal_row["user_id"]):
        return
    if db.is_shadow_banned("discord", str(message.author.id)):
        try:
            await message.delete()
        except Exception:
            pass
        return
    if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping appeal thread message from %s", message.author.id)
        return
    await _relay_verified_discord_message(
        message, bridge_id,
        sender_name=_consul_label(message.channel.id, message.author.id),
        avatar_url="",
    )

@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        try:
            await handle_appeal_dm_message(message)
        except Exception as e:
            logger.warning("appeal DM relay failed (user=%s): %s", message.author.id, e)
        return

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
        if message.author.id == GUARD_BOT_ID and db.get_appeal_by_thread(str(message.channel.id)):
            try:
                await message.pin()
            except Exception:
                pass
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

    appeal_row = db.get_appeal_by_thread(str(message.channel.id))
    if appeal_row:
        if not _discord_system_event_key(message):
            try:
                await handle_appeal_thread_message(message, appeal_row, bridge_id)
            except Exception as e:
                logger.warning("appeal thread relay failed (thread=%s): %s", message.channel.id, e)
        return

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
                        await interaction.response.send_message(localized("verify_button_not_yours", lang), ephemeral=True)
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
                    await interaction.response.send_message(localized("verify_thanks", lang), ephemeral=True)
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
    if after.guild is None:
        await process_appeal_dm_edit(after.author.id, after.id,
                                     after.author.display_name or str(after.author),
                                     after.content or "")
        return
    await process_discord_message_edit(
        guild=after.guild,
        channel=after.channel,
        message_id=after.id,
        author_display_name=after.author.display_name or str(after.author),
        text=replace_mentions(after, after.content or ""),
    )

def _edit_author_id_for_consul(message_db_id):
    """Original sender of a relayed message (for re-deriving the consul label
    when an appeal-thread message is edited)."""
    row = db.cur.execute(
        "SELECT origin_sender_id FROM messages WHERE id=?", (message_db_id,)
    ).fetchone()
    return row["origin_sender_id"] if row else "0"

async def process_appeal_dm_edit(author_id, message_id, author_display_name, text):
    """Propagate an edit of the appellant's DM message into the appeal thread."""
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
                                       origin_chat_id=None, place_name=None):
    if origin_chat_id is None:
        if not guild or not channel:
            return
        origin_chat_id = f"{guild.id}:{channel.id}"

    row = db.cur.execute(
        """
        SELECT id FROM messages
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
            author_display_name = _consul_label(channel.id, _edit_author_id_for_consul(row["id"]))

    header = f"[Discord | {clean_display_name(place_name)}] {clean_display_name(author_display_name)}:"
    telegram_text = replace_channel_mentions_for_telegram(text, guild)
    text_html = discord_to_telegram_html(telegram_text)

    copies = db.cur.execute("SELECT * FROM message_copies WHERE message_id=?", (row["id"],)).fetchall()
    for c in copies:
        try:
            if c["platform"] == "discord":
                ch = await resolve_discord_chat_channel(c["chat_id"])
                if ch is None:
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
        author_data = data.get("author") or {}
        author_id = author_data.get("id")
        if isinstance(channel, discord.DMChannel) and author_id:
            name = author_data.get("global_name") or author_data.get("username") or "Unknown"
            try:
                await process_appeal_dm_edit(int(author_id), payload.message_id, name, data.get("content") or "")
            except Exception as e:
                logger.warning("appeal DM edit relay failed (user=%s): %s", author_id, e)
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

async def _dm_discord_user(interaction: discord.Interaction, uid, text):
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

    if msg["origin_platform"] == "discord":
        channel = await resolve_discord_chat_channel(msg["origin_chat_id"])
        if channel:
            try:
                m = await channel.fetch_message(int(msg["origin_message_id"]))
                await m.delete()
            except Exception:
                pass

    await drop_gallery_upload(msg_id)

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
    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            localized("no_permission", lang),
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
            localized("deadchat_disabled", lang),
            ephemeral=True
        )
        return

    if not role_id.isdigit():
        await interaction.response.send_message(
            localized("deadchat_invalid_role", lang),
            ephemeral=True
        )
        return

    if hours is None or hours <= 0:
        await interaction.response.send_message(
            localized("deadchat_invalid_hours", lang),
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
        localized("deadchat_set", lang, role_id=role_id, hours=hours),
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

    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            localized("no_permission", lang),
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
            localized("newschat_disabled", lang),
            ephemeral=True
        )
        return

    if action.lower() == "add":
        if emoji is None or emoji.strip() == "":
            await interaction.response.send_message(
                localized("newschat_specify_emoji", lang),
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
                localized("newschat_bad_emoji", lang),
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
            localized("newschat_added", lang, emoji=emoji_str),
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        localized("newschat_usage", lang),
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

    lang = get_chat_lang(chat_id) or "en"
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

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
        localized_deadtopic("usage", lang),
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
    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
        return

    bridge_id = row["bridge_id"]

    if hours_or_disable.strip().lower() == "disable":
        db.cur.execute("DELETE FROM bridge_rules WHERE bridge_id=?", (bridge_id,))
        db.conn.commit()
        await interaction.response.send_message(localized("remindrules_disabled", lang), ephemeral=True)
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
            localized("remindrules_usage_discord", lang),
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
            await interaction.response.send_message(localized("remindrules_fetch_failed", lang), ephemeral=True)
            return

    if not content:
        await interaction.response.send_message(
            localized("remindrules_no_content", lang),
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
        localized("remindrules_saved", lang, interval=human),
        ephemeral=True
    )

@bot.tree.command(name="locallang", description="set bot language for this channel/thread (ru, uk, pl, en, es, pt)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
async def locallang_command(interaction: discord.Interaction, code: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_key, interaction.user.id)
    ):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    code = code.strip().lower()
    try:
        set_chat_lang(chat_key, code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set", code, code=code), ephemeral=True)

@bot.tree.command(name="lang", description="set the default bot language for the whole server (bridge admins)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
async def lang_command(interaction: discord.Interaction, code: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    allowed = is_admin("discord", interaction.user.id)
    if not allowed:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row and str(interaction.user.id) in db.get_bridge_admins(row["bridge_id"]):
            allowed = True
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    code = code.strip().lower()
    try:
        set_chat_lang(str(interaction.guild_id), code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set_server", code, code=code), ephemeral=True)

@bot.tree.command(name="mention", description="mention a user from another bridge community (1-hour cooldown per user)")
@app_commands.describe(target="User ID or username")
async def mention_cmd(interaction: discord.Interaction, target: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
        return
    bridge_id = row["bridge_id"]

    target = target.strip()
    if target.isdigit():
        uid = int(target)
    else:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            for c in db.get_bridge_chats(bridge_id):
                if c["platform"] != "discord" or c["chat_id"] == chat_key:
                    continue
                guild = bot.get_guild(int(c["chat_id"].split(":")[0]))
                if guild is None:
                    continue
                uid = await resolve_discord_user(guild, target)
                if uid is not None:
                    break
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if db.get_privacy_flag("discord", uid, "block_mention"):
        await interaction.response.send_message(localized("mention_opted_out", lang), ephemeral=True)
        return

    if not rate_limit_ok(("mention-target", str(uid)), limit=1, window_seconds=3600):
        await interaction.response.send_message(localized("mention_cooldown", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    ok = await send_bridge_mention(
        bridge_id, "discord", chat_key, uid,
        sender_name=interaction.user.display_name or str(interaction.user),
        place_name=interaction.guild.name if interaction.guild else "Discord",
        messenger_name="Discord",
        avatar_url=await relay_avatar_url(
            interaction.user.id, str(interaction.user.display_avatar.url)
        ),
    )
    if ok:
        await interaction.followup.send(localized("mention_sent", lang), ephemeral=True)
    else:
        await interaction.followup.send(localized("mention_no_discord", lang), ephemeral=True)

@bot.tree.command(name="list_chats", description="list all chats the bot is in (bot admins)")
async def list_chats(interaction: discord.Interaction):
    """
    Показывает администратору (ADMINS discord) список:
     - Discord: все сервера (guild.name и guild.id),
     - Telegram: все найденные префиксы chat_id (group_id) и попытка получить их названия через Telegram API.
    Доступна только администраторам из config.ADMINS["discord"].
    """
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
        import io
        bio = io.BytesIO(msg.encode("utf-8"))
        bio.seek(0)
        await interaction.response.send_message(localized("list_chats_too_long", lang), ephemeral=True)
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

@bot.tree.command(name="verify", description="confirm consent to message forwarding")
async def verify_slash(interaction: discord.Interaction):
    prefix = str(interaction.guild_id)
    user_id_str = str(interaction.user.id)
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"

    if not rate_limit_ok(("verify-cmd", "discord", user_id_str), limit=2, window_seconds=60):
        await interaction.response.send_message(localized("too_many_requests", lang), ephemeral=True)
        return

    if db.is_user_verified("discord", user_id_str, "*"):
        await interaction.response.send_message(localized("verify_already", lang), ephemeral=True)
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

    from discord import ui, ButtonStyle

    class _VerifyView(ui.View):
        def __init__(self, prefix, user_id):
            super().__init__(timeout=None)
            self.prefix = str(prefix)
            self.user_id = str(user_id)

        @ui.button(label=localized_consent_button(lang), style=ButtonStyle.primary)
        async def accept(self, interaction2: discord.Interaction, button: ui.Button):
            if str(interaction2.user.id) != str(self.user_id):
                await interaction2.response.send_message(localized("verify_button_not_yours", lang), ephemeral=True)
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
            await interaction2.response.send_message(localized("verify_thanks", lang), ephemeral=True)
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
    await interaction.response.send_message(localized("verify_message_sent", lang), ephemeral=True)

@bot.tree.command(name="unverify", description="revoke a user's verification")
async def unverify(interaction: discord.Interaction, target: str = None):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if target is None or not target.strip():
        uid = interaction.user.id
    else:
        if not is_admin("discord", interaction.user.id):
            await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
            return
        if target.startswith("<@") or not target.isdigit() or "#" in target:
            uid = await resolve_discord_user(interaction.guild, target)
            if uid is None:
                await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
                return
        else:
            uid = int(target)

    db.cur.execute("DELETE FROM verified_users WHERE platform='discord' AND user_id=?", (str(uid),))
    db.conn.commit()
    await interaction.response.send_message(localized("unverify_done", lang, user_id=uid), ephemeral=True)
    await announce_unverified_user(uid)

@bot.tree.command(name="shadow-ban", description="hide user's messages from relay")
async def shadow_ban(interaction: discord.Interaction, target: str):
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)
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
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = None
    if target.startswith("<@") or not target.isdigit() or "#" in target:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(target)

    db.add_shadow_ban("discord", uid)
    await interaction.response.send_message(localized("shadowban_done", lang, user_id=uid), ephemeral=True)

def _private_whois_embed(lang, nickname, avatar_url):
    """Everything `whois` may show about a sender who turned it off in
    `/privacy`: the nickname their messages already carry, and their avatar."""
    embed = discord.Embed(title=localized_whois("title", lang), color=discord.Color.blurple())
    embed.add_field(name=localized_whois("field_nickname", lang), value=nickname or "—", inline=False)
    embed.set_footer(text=localized_whois("private", lang))
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed

async def _whois_lookup(interaction: discord.Interaction, target_message: discord.Message | None, replied_id: str | None = None):
    """Общая логика для whois: ищет автора target_message или сообщения с replied_id."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    requester_id = str(interaction.user.id)
    if not rate_limit_ok(("whois", "discord", requester_id), limit=5, window_seconds=60):
        await interaction.response.send_message(localized("too_many_requests", lang), ephemeral=True)
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

            avatar_url = None
            if user_obj and getattr(user_obj, "display_avatar", None):
                avatar_url = str(user_obj.display_avatar.url)

            if db.get_privacy_flag("discord", origin_sender_id, "hide_whois"):
                await interaction.response.send_message(
                    embed=_private_whois_embed(lang, nick, avatar_url), ephemeral=True
                )
                return

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

            banner_url = None
            created_at = "—"
            if user_obj:
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

            if db.get_privacy_flag("telegram", origin_sender_id, "hide_whois"):
                await interaction.response.send_message(
                    embed=_private_whois_embed(lang, nick, None), ephemeral=True
                )
                return

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

def _privacy_embed(user_id, lang):
    """The `/privacy` menu: one line per switch, with what it does and whether
    the user has it on."""
    flags = db.get_user_privacy("discord", user_id)
    lines = [localized("privacy_header", lang), ""]
    for flag in db.PRIVACY_FLAGS:
        state = localized("privacy_state_on" if flags[flag] else "privacy_state_off", lang)
        mark = "🔒" if flags[flag] else "🔓"
        lines.append(f"{mark} {localized(f'privacy_opt_{flag}', lang)} — **{state}**")
    return discord.Embed(
        title=localized("privacy_title", lang),
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )

class PrivacyToggleButton(discord.ui.Button):
    def __init__(self, flag, lang, enabled):
        super().__init__(
            label=clip_text(localized(f"privacy_btn_{flag}", lang), 80),
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
        )
        self.flag = flag
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message(
                localized("privacy_not_yours", self.lang), ephemeral=True
            )
            return
        db.toggle_privacy_flag("discord", interaction.user.id, self.flag)
        await interaction.response.edit_message(
            embed=_privacy_embed(interaction.user.id, self.lang),
            view=PrivacyView(interaction.user.id, self.lang),
        )

class PrivacyView(discord.ui.View):
    def __init__(self, user_id, lang):
        super().__init__(timeout=600)
        self.user_id = user_id
        flags = db.get_user_privacy("discord", user_id)
        for flag in db.PRIVACY_FLAGS:
            self.add_item(PrivacyToggleButton(flag, lang, flags[flag]))

@bot.tree.command(name="privacy", description="configure what the bot may share about you")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def privacy_cmd(interaction: discord.Interaction):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    await interaction.response.send_message(
        embed=_privacy_embed(interaction.user.id, lang),
        view=PrivacyView(interaction.user.id, lang),
        ephemeral=True,
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

@bot.tree.command(name="allow-bots", description="allow or block relay of bot messages")
async def allow_bots_command(interaction: discord.Interaction, action: str):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_allow_bots(chat_id, True)
        await interaction.response.send_message(localized("allow_bots_enabled", lang), ephemeral=True)
    elif action == "disable":
        db.set_allow_bots(chat_id, False)
        await interaction.response.send_message(localized("allow_bots_disabled", lang), ephemeral=True)
    else:
        await interaction.response.send_message(localized("allow_bots_usage", lang), ephemeral=True)

@bot.tree.command(name="allow-files", description="allow re-uploading Telegram files to Discord and sharing their links")
@app_commands.describe(action="enable or disable", scope="local — this bridge only (default: this whole server)")
async def allow_files_command(interaction: discord.Interaction, action: str, scope: str = None):
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("allow_files_no_permission", lang), ephemeral=True)
        return

    action = action.strip().lower()
    scope = (scope or "").strip().lower()
    if action not in ("enable", "disable") or scope not in ("", "local"):
        await interaction.response.send_message(localized("allow_files_usage", lang), ephemeral=True)
        return

    enabled = action == "enable"
    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
            return
        db.set_bridge_file_consent(row["bridge_id"], enabled, enabled_by=interaction.user.id)
        key = "allow_files_bridge_enabled" if enabled else "allow_files_bridge_disabled"
    else:
        db.set_server_file_consent("discord", str(interaction.guild_id), enabled, enabled_by=interaction.user.id)
        key = "allow_files_enabled" if enabled else "allow_files_disabled"

    await interaction.response.send_message(localized(key, lang), ephemeral=True)

@bot.tree.command(name="verify-list", description="publish IDs of (un)verified users for Confederate Guard (bot admins)")
@app_commands.describe(action="enable or disable")
async def verify_list_cmd(interaction: discord.Interaction, action: str):
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_verify_list_enabled(True)
        await interaction.response.send_message(localized("verify_list_enabled", lang), ephemeral=True)
    elif action == "disable":
        db.set_verify_list_enabled(False)
        await interaction.response.send_message(localized("verify_list_disabled", lang), ephemeral=True)
    else:
        await interaction.response.send_message(localized("verify_list_usage", lang), ephemeral=True)

@bot.tree.command(name="webhooks", description="show relayed messages as webhooks (sender avatar and name)")
@app_commands.describe(action="enable or disable",
                       scope="`local` for this bridge only; omit for the whole server")
async def webhooks_command(interaction: discord.Interaction, action: str, scope: str | None = None):
    """Webhook-style relay copies, in the same two scopes as `/allow-files`:
    the whole server by default, or the server's chats in one bridge with
    `scope: local`. Both cover chats that join later."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    action = action.strip().lower()
    if action not in ("enable", "disable"):
        await interaction.response.send_message(localized("webhooks_usage", lang), ephemeral=True)
        return
    scope = (scope or "").strip().lower()
    if scope not in ("", "local"):
        await interaction.response.send_message(localized("webhooks_usage", lang), ephemeral=True)
        return

    enabled = action == "enable"
    server_id = str(interaction.guild_id)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
            return
        db.set_bridge_webhooks(server_id, row["bridge_id"], enabled,
                               enabled_by=interaction.user.id)
        key = "webhooks_bridge_enabled" if enabled else "webhooks_bridge_disabled"
    else:
        db.set_server_webhooks(server_id, enabled, enabled_by=interaction.user.id)
        key = "webhooks_enabled" if enabled else "webhooks_disabled"

    if not enabled:
        db.set_webhooks_enabled(chat_id, False)

    await interaction.response.send_message(localized(key, lang), ephemeral=True)

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
        localized_help("cmd_mention", lang),
        localized_help("cmd_poll", lang),
        localized_help("cmd_appeal", lang),
        localized_help("cmd_privacy", lang),
        localized_help("cmd_locale", lang),
        localized_help("cmd_loc_compare", lang),
        localized_help("cmd_loc_suggest", lang),
        localized_help("cmd_help", lang),
    ])

    admins_lines = "\n".join([
        localized_help("cmd_rfb", lang),
        localized_help("cmd_setadmin", lang),
        localized_help("cmd_lang", lang),
        localized_help("cmd_locallang", lang),
        localized_help("cmd_remindrules", lang),
        localized_help("cmd_shadowban", lang),
        localized_help("cmd_allow_bots", lang),
        localized_help("cmd_allow_files", lang),
        localized_help("cmd_webhooks", lang),
        localized_help("cmd_deadtopic", lang),
        localized_help("cmd_deadchat", lang),
        localized_help("cmd_newschat", lang),
    ])

    bot_admins_lines = "\n".join([
        localized_help("cmd_atb", lang),
        localized_help("cmd_setytfeed", lang),
        localized_help("cmd_remytfeed", lang),
        localized_help("cmd_setbskyfeed", lang),
        localized_help("cmd_rembskyfeed", lang),
        localized_help("cmd_settgfeed", lang),
        localized_help("cmd_remtgfeed", lang),
        localized_help("cmd_remadmin", lang),
        localized_help("cmd_setlocaladmin", lang),
        localized_help("cmd_remlocaladmin", lang),
        localized_help("cmd_localizer_add", lang),
        localized_help("cmd_localizer_rem", lang),
        localized_help("cmd_unverify", lang),
        localized_help("cmd_setname", lang),
        localized_help("cmd_verify_list", lang),
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
