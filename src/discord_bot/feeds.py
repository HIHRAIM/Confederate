"""Followed sources on the Discord side: the kind registry (FEED_KINDS and
feed_module), attaching a source to a chat, relaying its posts, the bundled
avatar assets, and the GALLERY re-upload helpers.

The GALLERY and avatar helpers live here rather than in a module of their
own because feeds are their main consumer; the Telegram file re-upload and
the relay's delete path borrow them (via call-site imports on their side or
deferred imports here, keeping the import graph acyclic).

The avatars themselves are files in `src/assets/`, and that directory is the
registry: nothing lists them by hand, so a picture put there is hosted with
no code change and one the code names but nobody shipped is named in the log
at start-up. Where each of them currently lives on Discord is a row in
`avatar_assets` (db/assets.py), never a constant here — the id of a message
somebody can delete is not something to write down in source, which is how
every avatar of this bot went missing at once.

Not this module's zone: fetching and parsing posts (sources/*), the polling
schedule (main.py: feed_loop), or the /set*feed commands (commands/feeds.py).
"""
import asyncio
import datetime
import hashlib
import io
import logging
import os
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

AVATAR_ASSET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

TG_AVATAR_ASSETS = {
    1: "user-green.png", 2: "user-green.png",
    3: "user-yellow.png", 4: "user-yellow.png",
    5: "user-red.png", 6: "user-red.png",
    7: "user-grey.png", 8: "user-grey.png",
    9: "user-blue.png", 0: "user-blue.png",
}

def bundled_avatar_assets():
    """Every avatar picture the repository ships: the contents of `assets/`.

    The directory is the registry, which is why no list of names appears
    anywhere in this module. A picture put there is warmed and hosted without
    a code change, and one the code names but nobody shipped is reported by
    `warm_avatar_assets` instead of turning up as a relayed copy with no
    face."""
    try:
        return sorted(f for f in os.listdir(AVATAR_ASSET_DIR)
                      if f.lower().endswith(".png"))
    except OSError:
        return []

def required_avatar_assets():
    """The assets the code itself asks for by name: the two placeholder sets,
    the followed-source kinds and the wiki hosts. Anything here that `assets/`
    does not hold is a picture some relayed copy is going to go without."""
    names = set(TG_AVATAR_ASSETS.values()) | set(_DC_AVATAR_ASSETS.values())
    names |= {spec["avatar_asset"] for spec in FEED_KINDS.values()}
    names |= set(WIKI_AVATAR_ASSETS.values()) | {WIKI_DEFAULT_AVATAR}
    return names

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
_avatar_failed = {}
_AVATAR_RETRY_AFTER = 3600
_avatar_lock = asyncio.Lock()

def _avatar_asset_file(asset):
    """The bundled file's bytes and their sha256, or ``(None, None)`` when the
    repository holds no such asset."""
    try:
        with open(os.path.join(AVATAR_ASSET_DIR, asset), "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    return data, hashlib.sha256(data).hexdigest()

async def _read_avatar_asset_url(row):
    """The signed link of the attachment in an asset's host message, read
    afresh, or None when that message is no longer reachable."""
    try:
        channel_id = int(row["channel_id"])
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await ch.fetch_message(int(row["message_id"]))
    except Exception as e:
        logger.info("avatar asset %s: its host message is unreachable (%s)",
                    row["name"], e)
        return None
    match = discord.utils.find(lambda a: a.filename == row["name"], msg.attachments)
    attachment = match or (msg.attachments[0] if msg.attachments else None)
    return attachment.url if attachment is not None else None

async def _upload_avatar_asset(asset, data, digest):
    """Put a bundled avatar into the GALLERY channel and remember the message.

    GALLERY is where the bot already keeps the files it hands out as links:
    it is picked for reachability and for exactly the permissions this needs,
    and the bot never deletes anything there. A deletion by somebody else
    costs one re-upload instead of a broken picture in every relayed copy."""
    channel = await resolve_gallery_channel()
    if channel is None:
        logger.warning("avatar asset %s: no reachable gallery channel to upload it to",
                       asset)
        return None
    try:
        sent = await channel.send(file=discord.File(io.BytesIO(data), filename=asset))
    except Exception as e:
        logger.warning("avatar asset %s upload failed (channel=%s): %s",
                       asset, channel.id, e)
        return None
    url = sent.attachments[0].url if sent.attachments else None
    if not url:
        return None
    db.save_avatar_asset(asset, digest, channel.id, sent.id, url)
    logger.info("avatar asset %s uploaded to channel %s", asset, channel.id)
    return url

async def avatar_asset_url(asset):
    """A Discord-usable link to one bundled avatar.

    Discord signs attachment links and they stop working within a day, so
    what is kept is not the link but the *message* the file was uploaded in:
    the link is read off it again whenever the cached one ages out. When
    there is no such message any more — deleted, never made, or the file in
    `assets/` replaced since — the file is uploaded again and the new message
    recorded. That is the whole point of the indirection: it needs nobody to
    leave a message alone, which the list of hand-written ids it replaces did
    need, and did not get.

    Returns None when the asset is neither bundled nor stored; the caller
    then posts without an avatar rather than with a broken one. A failure is
    remembered for an hour, so a gallery channel that is briefly unreachable
    costs one attempt rather than one per relayed message — and is tried
    again afterwards, since the bot is not restarted for such things."""
    now = time.time()
    cached = _avatar_url_cache.get(asset)
    if cached and now - cached[1] < _AVATAR_URL_TTL:
        return cached[0]
    failed_at = _avatar_failed.get(asset)
    if failed_at and now - failed_at < _AVATAR_RETRY_AFTER:
        return None

    async with _avatar_lock:
        now = time.time()
        cached = _avatar_url_cache.get(asset)
        if cached and now - cached[1] < _AVATAR_URL_TTL:
            return cached[0]

        data, digest = _avatar_asset_file(asset)
        row = db.get_avatar_asset(asset)
        url = None

        if row and row["message_id"] and (digest is None or row["sha256"] == digest):
            if row["url"] and now - (row["url_ts"] or 0) < _AVATAR_URL_TTL:
                url = row["url"]
            else:
                url = await _read_avatar_asset_url(row)
                if url:
                    db.set_avatar_asset_url(asset, url)

        if url is None and data is not None:
            url = await _upload_avatar_asset(asset, data, digest)

        if url is None and row and row["url"]:
            url = row["url"]
            logger.warning("avatar asset %s: falling back to a stored link that may "
                           "have expired", asset)

        if url:
            _avatar_failed.pop(asset, None)
            _avatar_url_cache[asset] = (url, time.time())
            return url

        _avatar_failed[asset] = time.time()
        if data is None:
            logger.warning("avatar asset %s is not in %s", asset, AVATAR_ASSET_DIR)
        else:
            logger.warning("avatar asset %s could not be published", asset)
        return None

async def warm_avatar_assets():
    """Resolve every bundled avatar once, before the first message needs one.

    One pass over `assets/`, one lookup each and no scanning of any channel:
    on an ordinary restart the stored links are still young and this costs no
    Discord call at all, and on the first start after a file changed it costs
    one upload for that file. Doing it here rather than purely on demand is
    what turns a missing asset into two lines in the log at start-up instead
    of a silently faceless copy hours later."""
    bundled = bundled_avatar_assets()
    missing = sorted(required_avatar_assets() - set(bundled))
    if missing:
        logger.warning("avatar assets the code asks for but %s does not hold: %s",
                       AVATAR_ASSET_DIR, ", ".join(missing))
    unavailable = [asset for asset in bundled if not await avatar_asset_url(asset)]
    if unavailable:
        logger.warning("avatar assets that could not be published: %s",
                       ", ".join(unavailable))
    else:
        logger.info("%d avatar assets ready", len(bundled))

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
