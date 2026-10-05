"""Appeal storage: the appeals themselves, consul anonymization indexes,
consul aliases and the reserved appeal-bridge id range.

The appeal *flow* — threads, verdict buttons, kicks and bans — lives in
discord_bot/appeals.py; this module only answers what the database knows.
"""
import time

from config import PURGATORIUM_GUILD_ID
from db import conn, cur
from db.ranges import APPEAL_BRIDGE_ID_FLOOR, bridge_range

def next_appeal_bridge_id():
    """Allocate the next bridge id in the reserved appeal range.

    Appeal bridges live in the third of the four regions db/ranges.py cuts
    the id space into: ordinary and sponsor bridges stay below this floor,
    inbox conversations above the next one. Simple max+1 — appeal bridges are
    short-lived and the range is roomy, so holes need no reuse here.

    The range is bounded on BOTH sides, and that is the part worth keeping.
    Everything above the inbox floor belongs to conversations, so the max+1
    must be taken among appeal bridges alone; reading the whole tail instead
    would hand the next appeal a number one past the newest conversation, and
    the two allocators would then walk over each other."""
    floor, ceiling = bridge_range("appeal")
    row = cur.execute(
        "SELECT MAX(id) AS mx FROM bridges WHERE id >= ? AND id < ?",
        (floor, ceiling)
    ).fetchone()
    if row and row["mx"] is not None:
        return int(row["mx"]) + 1
    return APPEAL_BRIDGE_ID_FLOOR

def create_appeal(user_id, thread_id, bridge_id, lang, guild_id=None):
    """Open an appeal for a user, replacing any previous (resolved) record."""
    cur.execute("DELETE FROM appeal_flood_state WHERE user_id=?", (str(user_id),))
    cur.execute(
        "INSERT OR REPLACE INTO appeals "
        "(user_id, thread_id, bridge_id, lang, created_at, status, verdict_at, verdict_by, guild_id) "
        "VALUES (?,?,?,?,?,'open',NULL,NULL,?)",
        (str(user_id), str(thread_id), int(bridge_id), lang, int(time.time()),
         str(guild_id) if guild_id is not None else None)
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

def has_any_appeal(user_id, guild_id=None):
    """Whether the user filed an appeal in this server, in any status."""
    if guild_id is None:
        return get_appeal(user_id) is not None
    return cur.execute(
        "SELECT 1 FROM appeals WHERE user_id=? AND "
        "(guild_id=? OR (guild_id IS NULL AND ?=?)) LIMIT 1",
        (str(user_id), str(guild_id), str(guild_id), str(PURGATORIUM_GUILD_ID))
    ).fetchone() is not None

def delete_appeal(user_id):
    """Drop the appeal record and its per-thread consul indexes; returns the
    deleted row so the caller can also detach the bridge chats. Consul
    *aliases* are deliberately left alone (see set_consul_name)."""
    row = get_appeal(user_id)
    if row:
        cur.execute("DELETE FROM appeal_consuls WHERE thread_id=?", (row["thread_id"],))
    cur.execute("DELETE FROM appeals WHERE user_id=?", (str(user_id),))
    cur.execute("DELETE FROM appeal_flood_state WHERE user_id=?", (str(user_id),))
    conn.commit()
    return row

def count_appeal_message(appeal, window_seconds=600):
    """Count unanswered appellant messages in a ten-minute burst.

    A consul reply resets the counter. At 20 callers warn the writer; at 30
    they stop forwarding and ask Guard to ban. The burst window prevents a
    slow, unanswered conversation over days from being treated as flooding.
    """
    user_id = str(appeal["user_id"])
    now = int(time.time())
    row = cur.execute("SELECT * FROM appeal_flood_state WHERE user_id=?",
                      (user_id,)).fetchone()
    if row and row["blocked"]:
        return 30
    count = (int(row["unanswered"]) + 1 if row and
             now - int(row["window_started"]) < window_seconds else 1)
    started = int(row["window_started"]) if row and count > 1 else now
    cur.execute(
        "INSERT INTO appeal_flood_state"
        " (user_id,guild_id,bridge_id,window_started,unanswered,blocked)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
        " guild_id=excluded.guild_id,bridge_id=excluded.bridge_id,"
        " window_started=excluded.window_started,unanswered=excluded.unanswered,"
        " blocked=excluded.blocked",
        (user_id, str(appeal["guild_id"]), int(appeal["bridge_id"]),
         started, count, int(count >= 30)),
    )
    conn.commit()
    return count

def reset_appeal_flood(user_id):
    """A consul's answer gives the appellant a fresh conversation window."""
    cur.execute("DELETE FROM appeal_flood_state WHERE user_id=? AND blocked=0",
                (str(user_id),))
    conn.commit()

def pending_appeal_flood_bans():
    """Bans awaiting independent confirmation from the appeal server."""
    return cur.execute("SELECT * FROM appeal_flood_state WHERE blocked=1"
                       " AND ban_confirmed=0").fetchall()

def confirm_appeal_flood_ban(user_id, bridge_id):
    """Purge only the banned user's records inside their exact appeal bridge.

    This does not remove other users' messages, the appeal itself, its verdict
    buttons, or copies still present in Discord. A live ban is checked first
    by the caller, so a failed Guard sync cannot cause premature deletion.
    """
    owner = str(user_id)
    bridge = int(bridge_id)
    cur.execute("UPDATE appeal_flood_state SET ban_confirmed=1,"
                " unanswered=0,window_started=0"
                " WHERE user_id=? AND bridge_id=?", (owner, bridge))
    predicate = "SELECT id FROM messages WHERE bridge_id=? AND origin_sender_id=?"
    for table, column in (("message_copies", "message_id"),
                          ("media_group_members", "message_id"),
                          ("gallery_uploads", "message_id")):
        cur.execute(f"DELETE FROM {table} WHERE {column} IN ({predicate})",
                    (bridge, owner))
    cur.execute("DELETE FROM messages WHERE bridge_id=? AND origin_sender_id=?",
                (bridge, owner))
    conn.commit()

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

def get_appeal_consul_alias(user_id, guild_id):
    """A sponsor appeal server's own consul alias, if set."""
    row = cur.execute(
        "SELECT name FROM appeal_consul_aliases WHERE guild_id=? AND user_id=?",
        (str(guild_id), str(user_id))
    ).fetchone()
    return row["name"] if row else None

def find_appeal_alias_owner(guild_id, normalized):
    """Who owns an alias on one sponsor server, if anyone."""
    row = cur.execute(
        "SELECT user_id FROM appeal_consul_aliases WHERE guild_id=? AND normalized=?",
        (str(guild_id), normalized)
    ).fetchone()
    return row["user_id"] if row else None

def set_appeal_consul_alias(guild_id, user_id, name, normalized, set_by):
    """Store one scoped alias without affecting the operator aliases."""
    cur.execute(
        "INSERT INTO appeal_consul_aliases"
        " (guild_id,user_id,name,normalized,set_by,set_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(guild_id,user_id) DO UPDATE SET"
        " name=excluded.name,normalized=excluded.normalized,"
        " set_by=excluded.set_by,set_at=excluded.set_at",
        (str(guild_id), str(user_id), name, normalized, str(set_by), int(time.time()))
    )
    conn.commit()

def remove_appeal_consul_alias(guild_id, user_id):
    """Remove only the alias on this appeal server."""
    changed = cur.execute(
        "DELETE FROM appeal_consul_aliases WHERE guild_id=? AND user_id=?",
        (str(guild_id), str(user_id))
    )
    conn.commit()
    return bool(changed.rowcount)
