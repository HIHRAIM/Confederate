"""Relayed-message bookkeeping: media-group mapping, copy identification,
GALLERY upload records and retention.

The messages/message_copies rows themselves are written by
message_relay.relay_message with inline SQL; this module holds the named
helpers around them. Not this module's zone: delivering or editing the
actual chat messages (discord_bot/relay.py, telegram_bot/relay.py).
"""
import time

from db import conn, cur
from db.ranges import bridge_range

def cleanup_old_messages(days=30):
    """Retention sweep: drop relayed-message records older than `days`,
    together with their copies and media-group mappings. Runs at start-up and
    daily (main.py). Origins with queued sponsor copies remain until those copies
    are sent or cancelled. Only the *records* go — the chat messages themselves stay, they
    just can no longer be edited/deleted through the bridge or answered by
    whois."""
    limit = int(time.time()) - days * 86400

    cur.execute(
        "DELETE FROM message_copies WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?"
        " AND id NOT IN (SELECT message_id FROM sponsor_relay_queue))",
        (limit,)
    )
    cur.execute(
        "DELETE FROM media_group_members WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?"
        " AND id NOT IN (SELECT message_id FROM sponsor_relay_queue))",
        (limit,)
    )
    cur.execute(
        "DELETE FROM gallery_uploads WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?"
        " AND id NOT IN (SELECT message_id FROM sponsor_relay_queue))",
        (limit,)
    )
    cur.execute(
        "DELETE FROM messages WHERE created_at IS NOT NULL AND created_at < ?"
        " AND id NOT IN (SELECT message_id FROM sponsor_relay_queue)",
        (limit,)
    )
    conn.commit()

def cleanup_appeal_message_history(days=7):
    """Keep at most seven days of appeal relay bookkeeping, even for open cases.

    The Discord thread and DM messages remain available to their participants.
    Only IDs used for cross-platform reply, edit and delete propagation leave
    the database. This bounds DB growth without refusing an appeal or ending an
    ongoing one. Closed appeal metadata is swept separately after seven days.
    """
    floor, ceiling = bridge_range("appeal")
    cutoff = int(time.time()) - days * 86400
    old = (floor, ceiling, cutoff)
    predicate = ("SELECT id FROM messages WHERE bridge_id>=? AND bridge_id<?"
                 " AND created_at IS NOT NULL AND created_at<?")
    for table in ("message_copies", "media_group_members", "gallery_uploads"):
        cur.execute(f"DELETE FROM {table} WHERE message_id IN ({predicate})", old)
    removed = cur.execute(
        "DELETE FROM messages WHERE bridge_id>=? AND bridge_id<?"
        " AND created_at IS NOT NULL AND created_at<?", old
    ).rowcount
    cur.execute("DELETE FROM sponsor_appeal_usage")
    cur.execute("DELETE FROM sponsor_appeal_reservations")
    conn.commit()
    return removed

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

def _telegram_group_pattern(group_id):
    """``<group>:%`` — every topic of one Telegram group. A supergroup numbers
    its messages once for all its topics, so the group and a message id are
    enough to find a message whichever topic it was written in."""
    return f"{int(group_id)}:%"

def find_telegram_message_db_id(group_id, platform_message_id):
    """The relayed message one Telegram message of a group stands for, or None.

    The id may be the original itself, any part of an album, or one of the
    bridge's own copies in that group — which is what a Telegram reply to a
    message of another chat points at, and the other chat may hold either."""
    pattern = _telegram_group_pattern(group_id)
    message_id = str(int(platform_message_id))
    row = cur.execute(
        "SELECT id FROM messages WHERE origin_platform='telegram' AND origin_chat_id LIKE ?"
        " AND origin_message_id=? ORDER BY id DESC LIMIT 1",
        (pattern, message_id)
    ).fetchone()
    if row:
        return row["id"]
    row = cur.execute(
        "SELECT message_id FROM media_group_members WHERE chat_id LIKE ? AND message_id_platform=?"
        " LIMIT 1",
        (pattern, message_id)
    ).fetchone()
    if row:
        return row["message_id"]
    row = cur.execute(
        "SELECT message_id FROM message_copies WHERE platform='telegram' AND chat_id LIKE ?"
        " AND message_id_platform=? ORDER BY id DESC LIMIT 1",
        (pattern, message_id)
    ).fetchone()
    return row["message_id"] if row else None

def telegram_origins_created_between(since, until, after_id=0, limit=300):
    """Telegram originals from supergroups relayed in ``[since, until)``, in id
    order after `after_id`, at most `limit` of them — the slices
    telegram_deletions.py checks against Telegram.

    Each comes as ``(message_db_id, group_id, [telegram ids])``: an album is
    one relayed message standing for several Telegram messages, listed from
    `media_group_members`; any other message stands for its own id. Basic
    groups are left out — their ids are not the group's own and cannot be
    asked about."""
    rows = cur.execute(
        "SELECT id, origin_chat_id, origin_message_id FROM messages"
        " WHERE origin_platform='telegram' AND origin_chat_id LIKE '-100%'"
        " AND created_at>=? AND created_at<? AND id>? ORDER BY id LIMIT ?",
        (int(since), int(until), int(after_id), int(limit))
    ).fetchall()
    if not rows:
        return []
    members = {}
    marks = ",".join("?" for _ in rows)
    for member in cur.execute(
            f"SELECT message_id, message_id_platform FROM media_group_members"
            f" WHERE message_id IN ({marks})",
            [row["id"] for row in rows]).fetchall():
        members.setdefault(member["message_id"], []).append(member["message_id_platform"])
    result = []
    for row in rows:
        ids = [int(i) for i in members.get(row["id"], [row["origin_message_id"]])
               if str(i).isdigit()]
        if ids:
            result.append((row["id"], int(row["origin_chat_id"].split(":", 1)[0]), ids))
    return result

def latest_telegram_copy_ids(group_id, limit=3):
    """The ids of the newest copies the bridge posted into a Telegram group —
    messages the bot wrote itself and can always read back, which is what
    lets telegram_deletions.py tell an answer it may trust from one it may
    not."""
    rows = cur.execute(
        "SELECT message_id_platform FROM message_copies WHERE platform='telegram'"
        " AND chat_id LIKE ? ORDER BY id DESC LIMIT ?",
        (_telegram_group_pattern(group_id), int(limit))
    ).fetchall()
    return [int(row["message_id_platform"]) for row in rows
            if str(row["message_id_platform"]).isdigit()]

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
    without bound.

    The per-chat cut is one ranking pass over the wiki copies, newest first,
    and everything past `keep_per_chat` goes. It used to count the newer
    copies again for every row, which on a real database held the event loop
    for twenty seconds on each run — this function is called on the loop, so
    that was twenty seconds of no commands answered and no heartbeat sent."""
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
        "  SELECT id FROM ("
        "    SELECT c.id AS id,"
        "           ROW_NUMBER() OVER (PARTITION BY c.chat_id"
        "                              ORDER BY c.id DESC) AS newest_first"
        "    FROM message_copies c"
        "    JOIN messages m ON m.id = c.message_id"
        "    WHERE m.origin_platform LIKE 'feed:wiki%'"
        "      AND c.chat_id IS NOT NULL)"
        "  WHERE newest_first > ?)",
        (keep_per_chat,)
    )
    cur.execute(
        "DELETE FROM messages WHERE origin_platform LIKE 'feed:wiki%'"
        " AND id NOT IN (SELECT message_id FROM message_copies)"
        " AND id NOT IN (SELECT message_id FROM sponsor_relay_queue)"
    )
    conn.commit()
