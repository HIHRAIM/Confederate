"""Followed Telegram channels on the push side.

A channel the bot administrates never gets polled: Telegram hands the bot
every post as a `channel_post` update, and the feed row is marked live. This
module relays those pushed posts and demotes a feed back to polling when the
bot loses its admin rights there.

Not this module's zone: reading a channel the bot is *not* in (that is
sources/telegram.py, the web-preview scraper) or the /settgfeed command
(commands/feeds.py).
"""
import asyncio
import logging

from aiogram.types import Message

import db

from telegram_bot.client import bot, router
from telegram_bot.files import _collect_gallery_candidates, _upload_telegram_files_to_gallery

logger = logging.getLogger("bridge.telegram")

_channel_post_buffer = {}

async def _demote_live_channel_feeds(channel_id):
    """A followed channel the bot has just lost its admin rights in stops
    delivering posts on its own, so its feeds fall back to polling the public
    web preview. Everything published so far counts as seen — the fallback is
    meant to keep the feed alive, not to replay it."""
    rows = db.get_feeds_by_source_id("telegram", channel_id)
    live = [r for r in rows if r["live"]]
    if not live:
        return
    last_id = None
    try:
        from sources.telegram import fetch_posts
        _, posts = await fetch_posts(live[0]["source"])
        last_id = posts[-1]["id"] if posts else None
    except Exception as e:
        logger.warning("channel feed fallback lookup failed (%s): %s", channel_id, e)
    for feed in live:
        db.add_feed(feed["kind"], feed["source"], feed["platform"], feed["chat_id"],
                    source_id=feed["source_id"], title=feed["title"],
                    last_post_id=last_id or feed["last_post_id"], live=False,
                    added_by=feed["added_by"])

def _channel_post_forward(message: Message):
    """``(forward_type, forward_name)`` for a channel post that is itself a
    repost — the bridge's own "(forwarded from …)" wording, resolved through the
    Bot API rather than guessed."""
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return "chat", chat.title or getattr(chat, "username", None) or "unknown"
    user = getattr(message, "forward_from", None)
    if user is not None:
        return "user", user.full_name or getattr(user, "username", None) or "unknown"
    name = getattr(message, "forward_sender_name", None)
    if name:
        return "user", name
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if origin_chat is not None:
            return "chat", origin_chat.title or getattr(origin_chat, "username", None) or "unknown"
        sender = getattr(origin, "sender_user", None)
        if sender is not None:
            return "user", sender.full_name or "unknown"
        sender_name = getattr(origin, "sender_user_name", None)
        if sender_name:
            return "user", sender_name
    return None, None

async def _relay_channel_post(message: Message, extra_messages=None):
    """Relay a post of a followed Telegram channel the bot is a member of.

    The files come through the Bot API, so they are re-uploaded to GALLERY the
    same way a user's Telegram files are; whatever does not fit is represented
    by the usual "[N files from Telegram]" footer."""
    from discord_bot import relay_feed_post

    rows = db.get_feeds_by_source_id("telegram", message.chat.id)
    if not rows:
        return

    group = [message] + list(extra_messages or [])
    candidates = []
    for m in group:
        candidates.extend(_collect_gallery_candidates(m))
    upload, uploaded = await _upload_telegram_files_to_gallery(candidates)
    gallery_urls = list(upload["urls"]) if upload else []

    text = ""
    for m in group:
        text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
        if text:
            break

    forward_type, forward_name = _channel_post_forward(message)
    username = getattr(message.chat, "username", None)
    post = {
        "id": str(message.message_id),
        "text": text,
        "media": [],
        "link": f"https://t.me/{username}/{message.message_id}" if username else None,
        "author_name": message.chat.title,
        "forward_type": forward_type,
        "forward_name": forward_name,
    }
    skipped = max(0, len(candidates) - uploaded)
    for feed in rows:
        try:
            await relay_feed_post(feed, post, gallery=(gallery_urls, skipped))
        except Exception as e:
            logger.warning("channel post relay failed (%s -> %s): %s",
                           message.chat.id, feed["chat_id"], e)

async def _flush_channel_post_group(buffer_key):
    """Relay a buffered channel album once a second has passed with no new
    part (each part re-arms this task)."""
    await asyncio.sleep(1.0)
    payload = _channel_post_buffer.pop(buffer_key, None)
    if not payload:
        return
    await _relay_channel_post(payload["message"], extra_messages=payload["rest"])

@router.channel_post()
async def channel_post_handler(message: Message):
    """Posts of channels the bot was added to. Only those attached with
    `/settgfeed` are relayed; an album arrives as several updates and is held
    for a second so it goes out as one message."""
    if not db.get_feeds_by_source_id("telegram", message.chat.id):
        return

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (message.chat.id, str(media_group_id))
        payload = _channel_post_buffer.get(key)
        if not payload:
            payload = {"message": message, "rest": [], "task": None}
            _channel_post_buffer[key] = payload
        elif message.message_id < payload["message"].message_id:
            payload["rest"].append(payload["message"])
            payload["message"] = message
        else:
            payload["rest"].append(message)
        if payload.get("task"):
            payload["task"].cancel()
        payload["task"] = asyncio.create_task(_flush_channel_post_group(key))
        return

    await _relay_channel_post(message)
