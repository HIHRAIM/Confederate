"""Telegram attachments: counting them, and re-uploading them to the GALLERY
channel so the other chats of a bridge get links instead of nothing.

Telegram files are not publicly addressable — a file_id means nothing outside
this bot — so relaying an attachment means downloading it through the Bot API
and posting it to a Discord channel the bot controls (the GALLERY), whose CDN
links every chat can then be given. That is why the mechanic needs explicit
consent from every chat of the bridge (db.bridge_file_relay_enabled), or from
every host chat of an inbox conversation (db.inbox_file_relay_enabled).

The two entry points take an optional `source_bot`, because a file_id only
means something to the bot it was handed to: a file sent into a receiver
bot's private chat must be downloaded through that bot, not this one.

Not this module's zone: deciding whether the consent is there (relay.py,
inbox.py), or the GALLERY channel plumbing itself (discord_bot/feeds.py).
"""
import logging

from aiogram.types import Message

from message_relay import clean_display_name

from telegram_bot.client import bot

logger = logging.getLogger("bridge.telegram")

TELEGRAM_GETFILE_LIMIT = 20 * 1024 * 1024

def _count_telegram_files(message: Message) -> int:
    """How many attachments a message carries, counted the way the relay
    reports them. An animation arrives with a `document` of its own, so the
    two are counted as one file, not two."""
    count = 0
    if getattr(message, "document", None) and not getattr(message, "animation", None):
        count += 1
    if getattr(message, "photo", None):
        count += 1
    if getattr(message, "video", None):
        count += 1
    if getattr(message, "audio", None):
        count += 1
    if getattr(message, "voice", None):
        count += 1
    if getattr(message, "video_note", None):
        count += 1
    if getattr(message, "animation", None):
        count += 1
    return count

def _collect_gallery_candidates(message: Message):
    """The message's files that may be re-uploaded to GALLERY.

    Stickers, voice messages and video notes are left out on purpose — they keep
    their own localized markers. Mirrors `_count_telegram_files`, including its
    treatment of animations (which arrive with a `document` of their own)."""
    candidates = []
    mid = message.message_id

    def add(obj, name):
        """Record one file with the name its GALLERY upload should carry."""
        candidates.append({
            "file_id": obj.file_id,
            "size": getattr(obj, "file_size", None),
            "name": clean_display_name(name, max_len=96),
        })

    animation = getattr(message, "animation", None)
    document = getattr(message, "document", None)
    if animation:
        add(animation, getattr(animation, "file_name", None) or f"animation_{mid}.mp4")
    elif document:
        add(document, getattr(document, "file_name", None) or f"document_{mid}")

    photo = getattr(message, "photo", None)
    if photo:
        add(photo[-1], f"photo_{mid}.jpg")

    video = getattr(message, "video", None)
    if video:
        add(video, getattr(video, "file_name", None) or f"video_{mid}.mp4")

    audio = getattr(message, "audio", None)
    if audio:
        add(audio, getattr(audio, "file_name", None) or f"audio_{mid}.mp3")

    return candidates

async def _download_telegram_file(file_id, max_size, source_bot=None):
    """Bytes of a Telegram file, or None when it can't be fetched or is too
    large. Telegram's getFile refuses anything above 20 MB, and Discord's own
    upload limit cuts in below that on a non-boosted server.

    `source_bot` names the bot to ask. A file_id is meaningful only to the bot
    that was handed it, so a file sent into a receiver bot's private chat has
    to be fetched through *that* bot (inbox.py) — the main one gets nothing
    but an error for it."""
    api = source_bot or bot
    try:
        f = await api.get_file(file_id)
    except Exception as e:
        logger.warning("getFile failed (%s): %s", file_id, e)
        return None
    size = getattr(f, "file_size", None)
    if size and int(size) > max_size:
        return None
    try:
        buf = await api.download_file(f.file_path)
    except Exception as e:
        logger.warning("Telegram file download failed (%s): %s", file_id, e)
        return None
    data = buf.read() if hasattr(buf, "read") else buf
    if not data or len(data) > max_size:
        return None
    return data

async def _upload_telegram_files_to_gallery(candidates, source_bot=None):
    """Re-upload a Telegram message's files to a GALLERY channel.

    Returns ``(upload, uploaded_count)``. Files that exceed Telegram's getFile
    ceiling, Discord's upload limit or what is left of the single-message budget
    are skipped, and the caller keeps representing them with the usual footer;
    a bigger file never blocks a smaller one that still fits. Any failure at all
    — no reachable gallery, a failed download, a rejected upload — degrades to
    ``(None, 0)``, i.e. to the pre-GALLERY behaviour.

    `source_bot` is the bot the files are downloaded through; it defaults to
    the main one and is set only by the inbox system, whose files belong to a
    receiver bot's token."""
    if not candidates:
        return None, 0

    from discord_bot import gallery_upload, gallery_upload_budget
    budget = await gallery_upload_budget()
    if budget is None:
        return None, 0
    max_files, max_total = budget
    max_single = min(max_total, TELEGRAM_GETFILE_LIMIT)

    files = []
    total = 0
    for cand in candidates:
        if len(files) >= max_files:
            break
        known_size = cand.get("size") or 0
        if known_size and (known_size > max_single or total + known_size > max_total):
            continue
        data = await _download_telegram_file(cand["file_id"], max_single, source_bot)
        if data is None or total + len(data) > max_total:
            continue
        files.append({"name": cand["name"], "data": data})
        total += len(data)

    if not files:
        return None, 0

    upload = await gallery_upload(files)
    if not upload or not upload.get("urls"):
        return None, 0
    return upload, len(upload["urls"])
