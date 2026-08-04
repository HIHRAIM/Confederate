"""Followed sources on the Discord side: the kind registry (FEED_KINDS and
feed_module), attaching a source to a chat, relaying its posts, the bundled
avatar assets, and the GALLERY re-upload helpers.

The GALLERY and avatar helpers live here rather than in a module of their
own because feeds are their main consumer; the Telegram file re-upload and
the relay's delete path borrow them (via call-site imports on their side or
deferred imports here, keeping the import graph acyclic).

Not this module's zone: fetching and parsing posts (sources/*), the polling
schedule (main.py: feed_loop), or the /set*feed commands (commands/feeds.py).
"""
import datetime
import io
import logging
import time

import aiohttp
import discord

import db
import message_relay
from config import GALLERY
from message_relay import escape_html
from sources import bluesky, youtube
from utils import (
    FEED_REQUEST_HEADERS, FEED_REQUEST_TIMEOUT, FeedError, feed_media_name,
)

from discord_bot.client import bot
from discord_bot.relay import deliver_discord_relay, deliver_telegram_relay

logger = logging.getLogger("bridge.discord")

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
    "wiki-fandom.png": "https://media.discordapp.net/attachments/1476645334904995860/1533896061016608882/wiki-fandom.png?ex=6a722787&is=6a70d607&hm=5b25d503878006244fa4a31b15c5b39676c1beb019891ad3c480dd08745305f3&=&format=webp&quality=lossless",
    "wiki-miraheze.png": "https://media.discordapp.net/attachments/1476645334904995860/1533896061503013005/wiki-miraheze.png?ex=6a722787&is=6a70d607&hm=76fcd9fff4a00f36d0ac0a6776cee5ff832baff01bd9df6ed6f12920d92a7817&=&format=webp&quality=lossless",
    "Confederate.png": "https://media.discordapp.net/attachments/1476645334904995860/1533896606670131311/Confederate.png?ex=6a722809&is=6a70d689&hm=6c9722bec1c60cc3d5841a159368e23f5e58ac210c46c9fb47c1af7ff63d77c1&=&format=webp&quality=lossless&width=1024&height=1024",
}

WIKI_AVATAR_ASSETS = {
    "fandom": "wiki-fandom.png",
    "miraheze": "wiki-miraheze.png",
}

WIKI_DEFAULT_AVATAR = "Confederate.png"

def wiki_avatar_asset(source):
    """The avatar a wiki's relayed activity wears on Discord.

    The farm's own logo where the wiki lives on one the bot knows, and the
    bot's own mark everywhere else — a self-hosted wiki has no logo to
    borrow, and showing a farm's logo for a wiki that is not on it would be
    a lie."""
    import wiki_events
    return WIKI_AVATAR_ASSETS.get(wiki_events.wiki_hosting(source), WIKI_DEFAULT_AVATAR)

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
    assets = [spec["avatar_asset"] for spec in FEED_KINDS.values()]
    assets += list(WIKI_AVATAR_ASSETS.values()) + [WIKI_DEFAULT_AVATAR]
    for asset in dict.fromkeys(assets):
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
    """Delete one GALLERY message (its CDN links die with it). True on
    success; False covers everything from a missing channel to a message
    already gone."""
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
    "wiki": {
        "messenger": "Wiki",
        "avatar_asset": "Confederate.png",
        "file_source": "wiki",
        "stale_after": 120 * 86400,
    },
    "wikidisc": {
        "messenger": "Wiki",
        "avatar_asset": "Confederate.png",
        "file_source": "wiki",
        "stale_after": 120 * 86400,
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
    """The reader module (see sources/__init__.py for the interface) behind
    one kind of followed source."""
    if kind == "bluesky":
        return bluesky
    if kind == "youtube":
        return youtube
    if kind == "telegram":
        from sources import telegram
        return telegram
    if kind == "wiki":
        from sources import wiki
        return wiki
    if kind == "wikidisc":
        from sources import fandom
        return fandom
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
    """Delivery callback for feed posts — same platform routing as the relay's
    own send_to_chat closures, defined once here because every feed post uses
    it."""
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
        if getattr(e, "throttled", False):
            status = "throttled"
        else:
            status = getattr(e, "reason", None) or "unreachable"
        return status, source, None, None

    db.add_feed(kind, source, platform, chat_id, source_id=source_id, title=title,
                last_post_id=posts[-1]["id"] if posts else None, added_by=added_by)
    return "ok", source, title or source, feed_stale_since(posts, kind)
