"""Appeal storage: the appeals themselves, consul anonymization indexes,
consul aliases and the reserved appeal-bridge id range.

The appeal *flow* — threads, verdict buttons, kicks and bans — lives in
discord_bot/appeals.py; this module only answers what the database knows.
"""
import time

from db import conn, cur
from db.inbox import INBOX_BRIDGE_ID_FLOOR

APPEAL_BRIDGE_ID_FLOOR = 100000

def next_appeal_bridge_id():
    """Allocate the next bridge id in the reserved appeal range.

    Appeal bridges live at and above APPEAL_BRIDGE_ID_FLOOR so they can never
    collide with hand-numbered ordinary bridges, which must stay strictly
    below the floor. Simple max+1 — appeal bridges are short-lived and the
    range is roomy, so holes need no reuse here.

    The range is bounded on both sides: INBOX_BRIDGE_ID_FLOOR and everything
    above it belongs to inbox conversations, so the max+1 must be taken among
    appeal bridges alone. Reading the whole tail instead would hand the next
    appeal a number one past the newest conversation, and the two allocators
    would then walk over each other."""
    row = cur.execute(
        "SELECT MAX(id) AS mx FROM bridges WHERE id >= ? AND id < ?",
        (APPEAL_BRIDGE_ID_FLOOR, INBOX_BRIDGE_ID_FLOOR)
    ).fetchone()
    if row and row["mx"] is not None:
        return int(row["mx"]) + 1
    return APPEAL_BRIDGE_ID_FLOOR

def create_appeal(user_id, thread_id, bridge_id, lang):
    """Open an appeal for a user, replacing any previous (resolved) record."""
    cur.execute(
        "INSERT OR REPLACE INTO appeals "
        "(user_id, thread_id, bridge_id, lang, created_at, status, verdict_at, verdict_by) "
        "VALUES (?,?,?,?,?,'open',NULL,NULL)",
        (str(user_id), str(thread_id), int(bridge_id), lang, int(time.time()))
    )
    conn.commit()

def get_appeal(user_id):
    """The user's appeal record whatever its status, or None."""
    return cur.execute(
        "SELECT * FROM appeals WHERE user_id=?",
        (str(user_id),)
    ).fetchone()

def get_open_appeal(user_id):
    """The user's appeal only while it awaits a verdict — the check behind
    'you already have an open appeal' and behind relaying their DMs."""
    return cur.execute(
        "SELECT * FROM appeals WHERE user_id=? AND status='open'",
        (str(user_id),)
    ).fetchone()

def get_appeal_by_thread(thread_id):
    """The appeal a Purgatorium thread belongs to, or None — how on_message
    tells an appeal thread from an ordinary bridged chat."""
    return cur.execute(
        "SELECT * FROM appeals WHERE thread_id=?",
        (str(thread_id),)
    ).fetchone()

def resolve_appeal(user_id, status, verdict_by):
    """Mark an open appeal as 'pardoned' or 'condemned'. Returns True if a row changed."""
    c = cur.execute(
        "UPDATE appeals SET status=?, verdict_at=?, verdict_by=? WHERE user_id=? AND status='open'",
        (status, int(time.time()), str(verdict_by), str(user_id))
    )
    conn.commit()
    return c.rowcount > 0

def has_any_appeal(user_id):
    """Whether the user ever filed an appeal (any status) — used by the
    Purgatorium 7-day kick sweep."""
    return get_appeal(user_id) is not None

def delete_appeal(user_id):
    """Drop the appeal record and its per-thread consul indexes; returns the
    deleted row so the caller can also detach the bridge chats. Consul
    *aliases* are deliberately left alone (see set_consul_name)."""
    row = get_appeal(user_id)
    if row:
        cur.execute("DELETE FROM appeal_consuls WHERE thread_id=?", (row["thread_id"],))
    cur.execute("DELETE FROM appeals WHERE user_id=?", (str(user_id),))
    conn.commit()
    return row

def get_open_appeals():
    """All appeals awaiting a verdict — setup_hook re-registers their
    persistent verdict buttons from this after a restart."""
    return cur.execute("SELECT * FROM appeals WHERE status='open'").fetchall()

def get_resolved_appeals_older_than(max_age_seconds):
    """Resolved appeals past the retention window, for the daily maintenance
    pass to garbage-collect together with their bridges."""
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM appeals WHERE status != 'open' AND verdict_at IS NOT NULL AND verdict_at < ?",
        (cutoff,)
    ).fetchall()

def get_consul_ord(thread_id, consul_user_id):
    """Stable per-thread anonymization index of a consul (0 → 'Consul A', ...).

    The first time a consul writes in an appeal thread they get the next free
    index; afterwards the same index is always returned.
    """
    row = cur.execute(
        "SELECT ord FROM appeal_consuls WHERE thread_id=? AND consul_user_id=?",
        (str(thread_id), str(consul_user_id))
    ).fetchone()
    if row:
        return row["ord"]
    nxt = cur.execute(
        "SELECT COALESCE(MAX(ord) + 1, 0) AS nxt FROM appeal_consuls WHERE thread_id=?",
        (str(thread_id),)
    ).fetchone()["nxt"]
    cur.execute(
        "INSERT INTO appeal_consuls (thread_id, consul_user_id, ord) VALUES (?,?,?)",
        (str(thread_id), str(consul_user_id), nxt)
    )
    conn.commit()
    return nxt

def set_consul_name(user_id, name, normalized, set_by=None):
    """Store a consul's `/setname` alias — the fixed signature appellants see
    instead of 'Consul A/B/…'. One alias per consul, shared by every appeal.

    Deliberately not touched by `delete_appeal`, unlike `appeal_consuls`: the
    alias has to outlive the appeals it was used in."""
    cur.execute(
        "INSERT INTO consul_names (user_id, name, normalized, set_by, set_at)"
        " VALUES (?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(user_id) DO UPDATE SET"
        " name=excluded.name, normalized=excluded.normalized,"
        " set_by=excluded.set_by, set_at=excluded.set_at",
        (str(user_id), name, normalized,
         str(set_by) if set_by is not None else None)
    )
    conn.commit()

def get_consul_name(user_id):
    """The consul's alias, or None when they sign as 'Consul A/B/…'."""
    row = cur.execute(
        "SELECT name FROM consul_names WHERE user_id=?",
        (str(user_id),)
    ).fetchone()
    return row["name"] if row else None

def remove_consul_name(user_id):
    """Drop a consul's alias, sending them back to the anonymized label.
    Returns True when there was one."""
    removed = cur.execute(
        "DELETE FROM consul_names WHERE user_id=?",
        (str(user_id),)
    ).rowcount
    conn.commit()
    return removed > 0

def find_consul_name_owner(normalized):
    """Who already holds this alias, compared case-insensitively. ``None`` when
    it is free."""
    row = cur.execute(
        "SELECT user_id FROM consul_names WHERE normalized=?",
        (normalized,)
    ).fetchone()
    return row["user_id"] if row else None
