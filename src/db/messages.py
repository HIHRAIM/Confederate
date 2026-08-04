"""Relayed-message bookkeeping: media-group mapping, copy identification,
GALLERY upload records and retention.

The messages/message_copies rows themselves are written by
message_relay.relay_message with inline SQL; this module holds the named
helpers around them. Not this module's zone: delivering or editing the
actual chat messages (discord_bot/relay.py, telegram_bot/relay.py).
"""
import time

from db import conn, cur

def cleanup_old_messages(days=30):
    """Retention sweep: drop relayed-message records older than `days`,
    together with their copies and media-group mappings. Runs once at start-up
    (main.py). Only the *records* go — the chat messages themselves stay, they
    just can no longer be edited/deleted through the bridge or answered by
    whois."""
    limit = int(time.time()) - days * 86400

    cur.execute(
        "DELETE FROM message_copies WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?)",
        (limit,)
    )
    cur.execute(
        "DELETE FROM media_group_members WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?)",
        (limit,)
    )
    cur.execute(
        "DELETE FROM messages WHERE created_at IS NOT NULL AND created_at < ?",
        (limit,)
    )
    conn.commit()

def record_media_group_members(chat_id, platform_message_ids, message_db_id):
    """Map every Telegram message_id of an album to the single relayed message.

    A Telegram media group is delivered as several separate messages but relayed
    as one. Recording each constituent message_id lets a reply to *any* file in
    the album resolve to that one relayed message instead of being treated as a
    reply to an unknown message."""
    for pid in platform_message_ids:
        cur.execute(
            "INSERT OR REPLACE INTO media_group_members (chat_id, message_id_platform, message_id) VALUES (?,?,?)",
            (chat_id, str(pid), message_db_id)
        )
    conn.commit()

def find_message_db_id_by_media_member(chat_id, platform_message_id):
    """The relayed message an album member belongs to, or None — the reply
    resolver's fallback when the replied-to Telegram id has no messages row of
    its own."""
    row = cur.execute(
        "SELECT message_id FROM media_group_members WHERE chat_id=? AND message_id_platform=?",
        (chat_id, str(platform_message_id))
    ).fetchone()
    return row["message_id"] if row else None

def is_relay_copy(platform: str, chat_id: str, message_id_platform: str) -> bool:
    """Return True if the given message was sent by the bridge bot as a relay
    copy. This is what keeps /allow-bots chats from relaying the bridge's own
    copies back again in an endless loop."""
    row = cur.execute(
        "SELECT 1 FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=?",
        (platform, chat_id, message_id_platform)
    ).fetchone()
    return row is not None

def add_gallery_upload(message_id, channel_id, gallery_message_id, urls_json,
                       source_message_ids_json, file_ids_json=None):
    """Record the GALLERY message holding a relayed message's re-uploaded files,
    together with the CDN links handed out in its copies. The Telegram file ids
    are kept so that an edit can tell a changed attachment from a changed
    caption and leave working links alone."""
    cur.execute(
        "INSERT OR REPLACE INTO gallery_uploads"
        " (message_id, channel_id, gallery_message_id, urls, source_message_ids, file_ids, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (int(message_id), str(channel_id), str(gallery_message_id),
         urls_json, source_message_ids_json, file_ids_json, int(time.time()))
    )
    conn.commit()

def get_gallery_upload(message_id):
    """The GALLERY upload record of a relayed message, or None when its files
    were never re-uploaded."""
    return cur.execute(
        "SELECT * FROM gallery_uploads WHERE message_id=?",
        (int(message_id),)
    ).fetchone()

def delete_gallery_upload(message_id):
    """Drop the upload record (the caller deletes the GALLERY message itself —
    see discord_bot/feeds.py: drop_gallery_upload)."""
    cur.execute("DELETE FROM gallery_uploads WHERE message_id=?", (int(message_id),))
    conn.commit()

def cleanup_wiki_relay_records(keep_per_chat=100, days=30):
    """Trim the bookkeeping wiki activity leaves behind.

    A busy wiki produces a relayed message every few seconds, and each one
    costs a `messages` row and a `message_copies` row per chat — bookkeeping
    that exists to let an edit or a deletion of the *origin* propagate.
    Wiki activity has no such origin to follow: a change already made cannot
    be edited into the chat afterwards, so the rows are dead weight almost
    immediately.

    Each chat therefore keeps only its newest `keep_per_chat` wiki copies,
    and nothing older than `days` at all. What is lost with a pruned row is
    the link between the copies of one change in different chats: deleting a
    relayed wiki message in one chat stops taking the others with it once the
    row is gone. That is the deliberate trade for not letting the table grow
    without bound."""
    cutoff = int(time.time()) - days * 86400
    cur.execute(
        "DELETE FROM message_copies WHERE message_id IN ("
        "  SELECT id FROM messages"
        "   WHERE origin_platform LIKE 'feed:wiki%'"
        "     AND created_at IS NOT NULL AND created_at < ?)",
        (cutoff,)
    )
    cur.execute(
        "DELETE FROM message_copies WHERE id IN ("
        "  SELECT c.id FROM message_copies c"
        "  JOIN messages m ON m.id = c.message_id"
        "  WHERE m.origin_platform LIKE 'feed:wiki%'"
        "    AND (SELECT COUNT(*) FROM message_copies c2"
        "         JOIN messages m2 ON m2.id = c2.message_id"
        "         WHERE c2.chat_id = c.chat_id"
        "           AND m2.origin_platform LIKE 'feed:wiki%'"
        "           AND c2.id > c.id) >= ?)",
        (keep_per_chat,)
    )
    cur.execute(
        "DELETE FROM messages WHERE origin_platform LIKE 'feed:wiki%'"
        " AND id NOT IN (SELECT message_id FROM message_copies)"
    )
    conn.commit()
