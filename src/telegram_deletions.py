"""Deleting the copies of a Telegram original that was deleted in Telegram.

The Bot API has no update for a deleted message, and waiting for MTProto's
deletion updates does not work either: Telegram hands a bot's updates to one
of its sessions and documents that a second session (this one, beside the
Bot API server aiogram polls) is not guaranteed to receive any — while one
that does subscribe can take updates away from the polling that runs the
bridge. So this module never subscribes: its MTProto session (Telethon,
logged in with the bot token) is opened with updates switched off
(`invokeWithoutUpdates`), and it *asks* instead. `channels.getMessages`
answers every id of a supergroup with the message itself or with an explicit
`messageEmpty`, and a supergroup never reuses an id.

What counts as deleted, and nothing less:
  * the original's id came back as `messageEmpty` — an error, a timeout or an
    id left out of the answer proves nothing and is simply asked again later;
  * in the same answer at least one message came back present. Every request
    carries the bot's own newest copies in that group, which it can always
    read, so an answer from a group the bot cannot read is never taken for a
    group whose messages are all gone;
  * for an album, every one of its parts is gone — deleting one picture of
    an album is not deleting the message;
  * at most `_GROUP_DELETION_CAP` relayed messages per group per slice; a
    larger clean-up (a spam raid) is carried out over the following slices.

Only supergroups are checked: in a basic group the ids belong to the bot's
own message box, not to the group, and cannot be asked about. The checks
cover the relay metadata's 30 days, the young messages most often: see
`_SWEEPS`. The session lives in memory only, so no authorization file is
written.

Telethon does not have to be installed: `vendor/telethon-bundle.zip` holds
Telethon 1.45.0 and its three pure-Python dependencies (pyaes, rsa, pyasn1,
each with its licence in its dist-info), and Python imports them straight
from the archive. An installed Telethon wins over the bundle.
"""
import asyncio
import importlib
import importlib.util
import logging
import os
import sys
import time

import db
from config import TELEGRAM_TOKEN

logger = logging.getLogger("bridge.telegram_deletions")

_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "telethon-bundle.zip")

def _telethon_importable():
    """Whether Telethon can be imported, putting the bundled copy on the
    import path when no installed one is found. Appended, not inserted, so
    that installed packages keep precedence over the archive."""
    if importlib.util.find_spec("telethon") is not None:
        return True
    if os.path.isfile(_BUNDLE) and _BUNDLE not in sys.path:
        sys.path.append(_BUNDLE)
        importlib.invalidate_caches()
    return importlib.util.find_spec("telethon") is not None

_SWEEPS = (
    (2 * 3600, 60),
    (2 * 86400, 15 * 60),
    (31 * 86400, 6 * 3600),
)
"""``(max age, pause)``: each sweep walks the originals younger than its max
age and older than the previous sweep's, slice by slice, then rests for its
pause before walking again. Messages are deleted soonest after they are
posted, so the youngest are checked every minute and the rest of the 30 days
of relay metadata a few times a day."""

_TICK_SECONDS = 20
_SLICE_ROWS = 300
_IDS_PER_REQUEST = 100
_CANARIES = 3
_REQUEST_PAUSE = 0.3
_GROUP_DELETION_CAP = 50
_RELOGIN_PAUSE = 300

def configured():
    """Whether the check can run: both MTProto credentials set and Telethon
    importable, installed or bundled. Says in the log either way, since
    otherwise a deployment without them silently never deletes a copy."""
    api_id = os.environ.get("TELEGRAM_MTPROTO_API_ID")
    api_hash = os.environ.get("TELEGRAM_MTPROTO_API_HASH")
    if not (api_id and api_hash):
        logger.warning("Telegram deletion check is off: TELEGRAM_MTPROTO_API_ID and "
                       "TELEGRAM_MTPROTO_API_HASH are not set in src/.env")
        return False
    try:
        if int(api_id) <= 0:
            raise ValueError
    except ValueError:
        logger.warning("Telegram deletion check is off: TELEGRAM_MTPROTO_API_ID is not a number")
        return False
    if not _telethon_importable():
        logger.warning("Telegram deletion check is off: Telethon is neither installed "
                       "nor bundled in src/vendor")
        return False
    logger.info("Telegram deletion check is on")
    return True

async def _ask(client, group_id, ids):
    """Ask Telegram about some ids of one supergroup. Returns
    ``(present, gone)`` as sets, or None when the answer cannot be used —
    any error, or an answer in which nothing at all was present."""
    from telethon import errors, functions, types

    try:
        channel = await client.get_input_entity(types.PeerChannel(-int(group_id) - 10 ** 12))
        answer = await client(functions.channels.GetMessagesRequest(
            channel=channel, id=[types.InputMessageID(i) for i in ids]))
    except errors.FloodWaitError as error:
        logger.info("Telegram deletion check waits %s s (flood control)", error.seconds)
        await asyncio.sleep(error.seconds)
        return None
    except Exception as error:
        logger.info("Telegram deletion check could not read group %s: %s", group_id, error)
        return None
    present, gone = set(), set()
    for message in getattr(answer, "messages", ()):
        if isinstance(message, types.MessageEmpty):
            gone.add(message.id)
        else:
            present.add(message.id)
    if not present:
        return None
    return present, gone

async def _check_group(client, group_id, entries):
    """Check the relayed originals of one group, ``[(message_db_id, ids)]``,
    and delete the copies of those Telegram confirms are gone."""
    canaries = db.latest_telegram_copy_ids(group_id, _CANARIES)
    wanted = sorted({i for _, ids in entries for i in ids})
    room = max(_IDS_PER_REQUEST - len(canaries), 1)
    gone = set()
    for start in range(0, len(wanted), room):
        chunk = wanted[start:start + room]
        answer = await _ask(client, group_id, list(dict.fromkeys(chunk + canaries)))
        if answer is not None:
            gone |= answer[1] & set(chunk)
        await asyncio.sleep(_REQUEST_PAUSE)
    deleted = [db_id for db_id, ids in entries if all(i in gone for i in ids)]
    if not deleted:
        return
    from discord_bot.relay import delete_all_copies_and_origin

    for db_id in deleted[:_GROUP_DELETION_CAP]:
        logger.info("Telegram original of relayed message %s (group %s) is gone; "
                    "deleting its copies", db_id, group_id)
        await delete_all_copies_and_origin(db_id, origin_deleted=True)

async def _check_slice(client, rows):
    """Check one slice of `db.telegram_origins_created_between`, group by
    group."""
    groups = {}
    for db_id, group_id, ids in rows:
        groups.setdefault(group_id, []).append((db_id, ids))
    for group_id, entries in groups.items():
        await _check_group(client, group_id, entries)

async def _run_sweeps(client):
    """Walk the sweeps for as long as the session stays connected."""
    cursors = [0] * len(_SWEEPS)
    resume_at = [0.0] * len(_SWEEPS)
    while client.is_connected():
        for index, (max_age, pause) in enumerate(_SWEEPS):
            now = time.time()
            if now < resume_at[index]:
                continue
            min_age = _SWEEPS[index - 1][0] if index else 0
            rows = db.telegram_origins_created_between(
                now - max_age, now - min_age, cursors[index], _SLICE_ROWS)
            if not rows:
                cursors[index] = 0
                resume_at[index] = now + pause
                continue
            cursors[index] = rows[-1][0]
            await _check_slice(client, rows)
        await asyncio.sleep(_TICK_SECONDS)

async def watch():
    """Run the deletion check beside the bridge for as long as the process
    lives — main.py stops the whole bot when one of its tasks returns, so
    this one never does. A lost session is logged in again after a pause,
    which keeps a broken one from turning into a login every few seconds.

    It starts once Discord is ready: a deletion before that would find no
    Discord channel to delete the copies from and drop their records
    anyway, leaving the copies behind for good."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from discord_bot.client import bot as discord_bot

    await discord_bot.wait_until_ready()
    api_id = int(os.environ["TELEGRAM_MTPROTO_API_ID"])
    api_hash = os.environ["TELEGRAM_MTPROTO_API_HASH"]
    while True:
        client = TelegramClient(MemorySession(), api_id, api_hash, receive_updates=False)
        try:
            await client.start(bot_token=TELEGRAM_TOKEN)
            logger.info("Telegram deletion check connected")
            await _run_sweeps(client)
            logger.warning("Telegram deletion check lost its session")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Telegram deletion check stopped: %s", error)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(_RELOGIN_PAUSE)
