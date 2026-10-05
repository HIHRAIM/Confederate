"""Confirmed Telegram-origin deletion propagation for supergroups.

The Bot API offers no deletion update for ordinary messages. MTProto does
emit channel/supergroup deletions with a chat ID; Telethon exposes these as
MessageDeleted events. This optional listener ignores events without a chat
ID, large batches, and IDs absent from our exact origin records. In
particular it never infers deletion from a failed fetch or a missing update.

The listener uses a memory-only bot session: no unencrypted auth file is
written. An offline deletion may be missed; that is preferable to deleting
valid copies after an uncertain lookup.
"""
import asyncio
import importlib.util
import logging
import os

import db
from config import TELEGRAM_TOKEN

logger = logging.getLogger("bridge.telegram_deletions")

def configured():
    """Enable only when both Telegram API credentials are explicitly set."""
    if not (os.environ.get("TELEGRAM_MTPROTO_API_ID") and
            os.environ.get("TELEGRAM_MTPROTO_API_HASH")):
        return False
    if importlib.util.find_spec("telethon") is None:
        logger.warning("Telegram deletion listener disabled: Telethon is missing")
        return False
    try:
        return int(os.environ["TELEGRAM_MTPROTO_API_ID"]) > 0
    except ValueError:
        logger.warning("Telegram deletion listener disabled: API ID is invalid")
        return False

def confirmed_origin_rows(chat_id, message_id):
    """Find only messages whose Telegram origin has this exact group and ID.

    Topic suffixes differ, but Telegram message IDs are unique within a
    supergroup. A missing chat ID from MTProto is never substituted with a
    guessed one; basic-group delete events have no safe routing information.
    """
    try:
        group = int(chat_id)
        mid = int(message_id)
    except (TypeError, ValueError):
        return []
    if group >= 0 or mid <= 0:
        return []
    prefix = f"{group}:%"
    return db.cur.execute(
        "SELECT DISTINCT m.id FROM messages m WHERE"
        " m.origin_platform='telegram' AND m.origin_chat_id LIKE ?"
        " AND m.origin_message_id=? UNION"
        " SELECT DISTINCT g.message_id FROM media_group_members g"
        " JOIN messages m ON m.id=g.message_id"
        " WHERE m.origin_platform='telegram' AND g.chat_id LIKE ?"
        " AND g.message_id_platform=?",
        (prefix, str(mid), prefix, str(mid)),
    ).fetchall()

async def propagate_confirmed_deletion(chat_id, message_ids):
    """Delete copies only for a small, explicit MTProto deletion batch."""
    if chat_id is None or not message_ids or len(message_ids) > 50:
        return 0
    from discord_bot.relay import delete_all_copies_and_origin

    matched = set()
    for mid in message_ids:
        for row in confirmed_origin_rows(chat_id, mid):
            matched.add(int(row[0]))
    if len(matched) > 50:
        logger.warning("Telegram delete batch exceeded safety cap in %s", chat_id)
        return 0
    for row_id in sorted(matched):
        await delete_all_copies_and_origin(row_id)
    return len(matched)

async def listen():
    """Run the optional MTProto bot connection alongside aiogram polling.

    A disconnect retries after a pause. Missing credentials or Telethon leave
    the primary bridge running; no deletion is inferred in that state.
    """
    if not configured():
        return
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import MemorySession
    except ImportError:
        logger.warning("Telegram deletion listener disabled: Telethon is missing")
        return
    try:
        api_id = int(os.environ["TELEGRAM_MTPROTO_API_ID"])
    except (ValueError, KeyError):
        logger.warning("Telegram deletion listener disabled: API ID is invalid")
        return

    while True:
        client = TelegramClient(
            MemorySession(), api_id, os.environ["TELEGRAM_MTPROTO_API_HASH"]
        )

        @client.on(events.MessageDeleted)
        async def on_deleted(event):
            if event.chat_id is None:
                return
            try:
                await propagate_confirmed_deletion(event.chat_id, event.deleted_ids)
            except Exception as error:
                logger.warning("Confirmed Telegram delete could not be applied: %s", error)

        try:
            await client.start(bot_token=TELEGRAM_TOKEN)
            logger.info("Telegram deletion listener connected")
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Telegram deletion listener disconnected: %s", error)
        finally:
            await client.disconnect()
        await asyncio.sleep(60)
