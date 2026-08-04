"""Poll storage: definitions, the per-chat poll messages and the votes.

Votes are anonymous by construction — nothing here ever returns who voted,
only counts. The poll UI and result rendering live in
discord_bot/commands/polls.py and telegram_bot/commands/polls.py.
"""
import time

from db import conn, cur

def create_poll(bridge_id, question, options_json, ends_at):
    """Create a poll and return its id. `options_json` is the JSON-encoded
    option list — stored verbatim so every platform renders the same order."""
    c = cur.execute(
        "INSERT INTO polls (bridge_id, question, options, created_at, ends_at, closed) VALUES (?,?,?,?,?,0)",
        (bridge_id, question, options_json, int(time.time()), ends_at)
    )
    conn.commit()
    return c.lastrowid

def get_poll(poll_id):
    """The poll row, or None (voting handlers treat None as 'closed')."""
    return cur.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()

def add_poll_message(poll_id, platform, chat_id, message_id):
    """Remember the interactive poll message posted in one chat — needed to
    reply with the results there and to delete the poll everywhere."""
    cur.execute(
        "INSERT OR REPLACE INTO poll_messages (poll_id, platform, chat_id, message_id) VALUES (?,?,?,?)",
        (poll_id, platform, chat_id, str(message_id))
    )
    conn.commit()

def get_poll_messages(poll_id):
    """Every chat's poll message for this poll."""
    return cur.execute("SELECT * FROM poll_messages WHERE poll_id=?", (poll_id,)).fetchall()

def get_poll_by_message(platform, chat_id, message_id):
    """The poll a chat message belongs to, or None — how deleting any poll
    message in any chat is recognized and closes the poll everywhere."""
    row = cur.execute(
        "SELECT poll_id FROM poll_messages WHERE platform=? AND chat_id=? AND message_id=?",
        (platform, chat_id, str(message_id))
    ).fetchone()
    return row["poll_id"] if row else None

def record_poll_vote(poll_id, platform, user_id, option_index):
    """Record (or replace — re-voting is allowed) a user's vote."""
    cur.execute(
        "INSERT OR REPLACE INTO poll_votes (poll_id, platform, user_id, option_index) VALUES (?,?,?,?)",
        (poll_id, platform, str(user_id), int(option_index))
    )
    conn.commit()

def get_poll_results(poll_id, num_options):
    """Vote counts as a list aligned with the option list. Out-of-range
    indexes (options edited between versions) are silently dropped."""
    rows = cur.execute(
        "SELECT option_index, COUNT(*) AS cnt FROM poll_votes WHERE poll_id=? GROUP BY option_index",
        (poll_id,)
    ).fetchall()
    counts = [0] * num_options
    for r in rows:
        idx = r["option_index"]
        if idx is not None and 0 <= idx < num_options:
            counts[idx] = r["cnt"]
    return counts

def close_poll(poll_id):
    """Mark a poll closed — votes stop being accepted immediately."""
    cur.execute("UPDATE polls SET closed=1 WHERE id=?", (poll_id,))
    conn.commit()

def get_expired_open_polls():
    """Open polls past their end time, for poll_loop to post results and
    close."""
    now = int(time.time())
    return cur.execute(
        "SELECT * FROM polls WHERE closed=0 AND ends_at IS NOT NULL AND ends_at<=?",
        (now,)
    ).fetchall()

def get_open_polls():
    """All open polls — setup_hook re-registers their persistent vote buttons
    from this after a restart."""
    return cur.execute("SELECT * FROM polls WHERE closed=0").fetchall()

def delete_poll(poll_id):
    """Remove a poll with its votes and message records (the chat messages
    are deleted by the caller — close_and_delete_poll)."""
    cur.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
    cur.execute("DELETE FROM poll_messages WHERE poll_id=?", (poll_id,))
    cur.execute("DELETE FROM polls WHERE id=?", (poll_id,))
    conn.commit()

def cleanup_old_polls(max_age_seconds=7 * 24 * 3600):
    """Remove closed polls (and their votes/messages) a week after they ended."""
    cutoff = int(time.time()) - max_age_seconds
    rows = cur.execute(
        "SELECT id FROM polls WHERE closed=1 AND ends_at IS NOT NULL AND ends_at < ?",
        (cutoff,)
    ).fetchall()
    for r in rows:
        delete_poll(r["id"])
