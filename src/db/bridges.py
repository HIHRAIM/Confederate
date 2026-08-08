"""Bridges and their member chats: attach/detach, id allocation, target
resolution, reachability bookkeeping and whole-community cleanup.

Not this module's zone: admin grants (db/admins.py), per-chat switches
(db/settings.py), the appeal system (db/appeals.py) — though the ceiling on
ordinary bridge numbers is its constant, imported below, because the two
allocators divide one id space between them.
"""
import time

from db import conn, cur
from db.appeals import APPEAL_BRIDGE_ID_FLOOR

def chat_exists(chat_id):
    """Whether the chat is already attached to some bridge — the check behind
    every 'this chat is already in a bridge' refusal (/atb on both platforms)."""
    return cur.execute(
        "SELECT 1 FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone() is not None

def chat_server_id(platform, chat_id):
    """The server/group a chat belongs to: guild id for Discord, group id for
    Telegram. ``None`` for the one-person chats — a DM (appeal bridges) and a
    receiver bot's private chat (platform 'inbox', whose key starts with the
    *bot's* id, not a server's). Neither belongs to a community, so neither
    can ever carry a server-wide consent or admin grant."""
    chat_id = str(chat_id)
    if platform == "inbox" or chat_id.startswith("dm:"):
        return None
    return chat_id.split(":", 1)[0] or None

def attach_chat(platform, chat_id, bridge_id):
    """Put a chat into a bridge, creating the bridge row if needed.

    Also materializes the standing admin grants into per-chat rows: existing
    Bridge Admins of this bridge get a chat_admins row for the new chat, and
    server-wide Bridge Admins of the chat's server are copied into
    bridge_admins + chat_admins — this is what makes '/setadmin' grants cover
    bridges the server joins later."""
    cur.execute(
        "INSERT OR IGNORE INTO bridges(id) VALUES(?)",
        (bridge_id,)
    )
    cur.execute(
        "INSERT OR REPLACE INTO chats(platform, chat_id, bridge_id) VALUES(?,?,?)",
        (platform, chat_id, bridge_id)
    )
    rows = cur.execute("SELECT user_id FROM bridge_admins WHERE bridge_id=?", (bridge_id,)).fetchall()
    for r in rows:
        cur.execute(
            "INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id) VALUES (?,?,?)",
            (platform, chat_id, r["user_id"])
        )
    server_id = chat_server_id(platform, chat_id)
    if server_id:
        for r in cur.execute(
            "SELECT user_id FROM server_bridge_admins WHERE platform=? AND server_id=?",
            (platform, server_id)
        ).fetchall():
            cur.execute(
                "INSERT OR IGNORE INTO bridge_admins (bridge_id, user_id) VALUES(?,?)",
                (bridge_id, r["user_id"])
            )
            cur.execute(
                "INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id) VALUES (?,?,?)",
                (platform, chat_id, r["user_id"])
            )
    conn.commit()

def attach_chat_to_bridge(platform, chat_id, bridge_id):
    """attach_chat with the 'already attached' check folded in; raises
    ValueError('chat_already_attached') instead of silently re-attaching."""
    if chat_exists(chat_id):
        raise ValueError("chat_already_attached")

    attach_chat(platform, chat_id, bridge_id)

_FREE_BRIDGE_ID_CANDIDATES = """
    SELECT 1 AS candidate
     WHERE 1 < :floor
       AND NOT EXISTS (SELECT 1 FROM bridges WHERE id = 1)
    UNION ALL
    SELECT b.id + 1
      FROM bridges b
     WHERE b.id + 1 >= 1
       AND b.id + 1 < :floor
       AND NOT EXISTS (SELECT 1 FROM bridges b2 WHERE b2.id = b.id + 1)
"""

_NEXT_FREE_BRIDGE_ID_SQL = (
    f"SELECT candidate FROM ({_FREE_BRIDGE_ID_CANDIDATES}) ORDER BY candidate LIMIT 1"
)

_CLAIM_FREE_BRIDGE_ID_SQL = (
    f"INSERT INTO bridges (id)"
    f" SELECT candidate FROM ({_FREE_BRIDGE_ID_CANDIDATES}) ORDER BY candidate LIMIT 1"
)

def next_free_bridge_id():
    """The lowest positive number no bridge occupies, or None when none is
    left. The twin of db/appeals.py: next_appeal_bridge_id, which allocates
    from the other end of the same id space.

    Two constraints, both easy to get wrong:

      * Holes are reused. With bridges 1, 2 and 5 the answer is 3, not 6. A
        bridge disappears together with its last chat, and its number should
        come back into circulation rather than push a counter up forever.
      * The answer is strictly below APPEAL_BRIDGE_ID_FLOOR. That line and
        everything above it belongs to the appeal system, and an ordinary
        bridge landing there would collide with the next appeal opened.

    The query works off the observation that the lowest free number is either
    1 or one past some occupied number, so it never has to walk the range.

    Read-only, and therefore racy on its own: two callers asking at the same
    moment get the same answer. Use `attach_chat_to_new_bridge` to actually
    take a number — it claims the row in the same statement.
    """
    row = cur.execute(
        _NEXT_FREE_BRIDGE_ID_SQL, {"floor": APPEAL_BRIDGE_ID_FLOOR}
    ).fetchone()
    return int(row["candidate"]) if row else None

def attach_chat_to_new_bridge(platform, chat_id):
    """Create a bridge on the lowest free number and attach `chat_id` to it.

    Returns the new bridge id, or None when every number below
    APPEAL_BRIDGE_ID_FLOOR is taken — the caller is expected to say so rather
    than fall back to the appeal range.

    The number is claimed by a single INSERT … SELECT. SQLite evaluates one
    statement under its write lock, so two administrators running `/atb new`
    at the same moment — in this process, or one of them through the control
    panel — cannot be handed the same number; the second statement re-reads
    the table and picks the next hole. Doing it as a SELECT followed by an
    INSERT would leave exactly that gap open.

    Attaching the chat afterwards is a separate write on purpose: if it were
    to fail, the reserved number is merely a hole, and holes are reused.
    """
    claimed = cur.execute(
        _CLAIM_FREE_BRIDGE_ID_SQL, {"floor": APPEAL_BRIDGE_ID_FLOOR}
    )
    conn.commit()
    if not claimed.rowcount:
        return None

    bridge_id = int(claimed.lastrowid)
    attach_chat(platform, chat_id, bridge_id)
    return bridge_id

def get_bridge_chats(bridge_id):
    """All member chats of a bridge — the target list of every relay, poll
    and notification fan-out."""
    return cur.execute(
        "SELECT * FROM chats WHERE bridge_id=?",
        (bridge_id,)
    ).fetchall()

def get_targets(bridge_id, exclude_chat_id):
    """The bridge's chats minus the origin chat — the copies to deliver."""
    chats = get_bridge_chats(bridge_id)
    return [c for c in chats if c["chat_id"] != exclude_chat_id]

def server_bridge_ids(platform, server_id):
    """The bridges a server/group takes part in."""
    return [r["bridge_id"] for r in cur.execute(
        "SELECT DISTINCT bridge_id FROM chats"
        " WHERE platform=? AND (chat_id LIKE ? OR chat_id=?)",
        (platform, f"{server_id}:%", str(server_id))
    ).fetchall() if r["bridge_id"] is not None]

def remove_chat_from_bridge(chat_id):
    """Detach a chat and clean up everything keyed on it (settings, admins,
    reachability, pending consents). When the last chat leaves, the bridge
    itself and its per-bridge state (admins, rules, webhooks) are deleted too.

    Returns None when the chat was not in a bridge, otherwise
    ``{"bridge_id", "bridge_deleted"}`` so the caller can announce the removal.
    Used by the daily reachability sweep and the appeal-record cleanup."""
    row = cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        return None

    bridge_id = row["bridge_id"]
    cur.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    cur.execute("DELETE FROM chat_settings WHERE chat_id=?", (chat_id,))
    cur.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
    cur.execute("DELETE FROM inaccessible_chats WHERE chat_id=?", (chat_id,))
    cur.execute("DELETE FROM pending_consents WHERE chat_key=?", (chat_id,))

    left = cur.execute("SELECT COUNT(*) AS cnt FROM chats WHERE bridge_id=?", (bridge_id,)).fetchone()
    bridge_deleted = False
    if not left or int(left["cnt"]) == 0:
        cur.execute("DELETE FROM bridges WHERE id=?", (bridge_id,))
        cur.execute("DELETE FROM bridge_admins WHERE bridge_id=?", (bridge_id,))
        cur.execute("DELETE FROM bridge_rules WHERE bridge_id=?", (bridge_id,))
        cur.execute("DELETE FROM bridge_webhooks WHERE bridge_id=?", (bridge_id,))
        bridge_deleted = True

    conn.commit()
    return {"bridge_id": bridge_id, "bridge_deleted": bridge_deleted}

def remove_chat_settings_for_prefix(prefix):
    """Drop everything keyed on a whole community when the bot leaves it:
    chat_settings rows under '<prefix>:%' plus the bare '<prefix>' row holding
    the community-wide /lang, the community's feeds, and its webhook scopes.
    prefix example: guild_id for discord, chat.id for telegram."""
    cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ? OR chat_id=?",
        (f"{prefix}:%", str(prefix))
    )
    cur.execute(
        "DELETE FROM feeds WHERE chat_id LIKE ? OR chat_id=?",
        (f"{prefix}:%", str(prefix))
    )
    cur.execute("DELETE FROM server_webhooks WHERE server_id=?", (str(prefix),))
    cur.execute("DELETE FROM bridge_webhooks WHERE server_id=?", (str(prefix),))
    conn.commit()

def mark_chat_inaccessible(platform, chat_id):
    """Record a failed reachability check, keeping the timestamp of the first
    failure of the streak. Returns the (first_failed_ts, last_failed_ts) row so
    the daily sweep can decide whether the 24-hour grace period has passed."""
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO inaccessible_chats (platform, chat_id, first_failed_ts, last_failed_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            platform=excluded.platform,
            last_failed_ts=excluded.last_failed_ts
        """,
        (platform, chat_id, now, now)
    )
    conn.commit()
    return cur.execute(
        "SELECT first_failed_ts, last_failed_ts FROM inaccessible_chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

def clear_chat_inaccessible(chat_id):
    """Forget a chat's failure streak — called on every successful check, so
    only uninterrupted failure ever crosses the auto-detach threshold."""
    cur.execute("DELETE FROM inaccessible_chats WHERE chat_id=?", (chat_id,))
    conn.commit()

def get_telegram_chat_count():
    """Number of Telegram chats (topics counted separately) attached to
    bridges — a statistic for the status line."""
    row = cur.execute(
        "SELECT COUNT(*) as cnt FROM chats WHERE platform='telegram'"
    ).fetchone()
    return row['cnt'] if row else 0

def get_telegram_group_count():
    """Number of distinct Telegram groups (topics of one group collapse into
    one) — the 'communities' half of the status line."""
    row = cur.execute(
        """
        SELECT COUNT(DISTINCT SUBSTR(chat_id, 1, INSTR(chat_id, ':') - 1)) AS cnt
        FROM chats
        WHERE platform='telegram'
        """
    ).fetchone()
    return row['cnt'] if row else 0

def get_telegram_group_ids():
    """Distinct Telegram group ids the bot bridges — used by the status loop
    to sum member counts via the Bot API."""
    rows = cur.execute(
        """
        SELECT DISTINCT SUBSTR(chat_id, 1, INSTR(chat_id, ':') - 1) AS group_id
        FROM chats
        WHERE platform='telegram'
        """
    ).fetchall()
    return [r['group_id'] for r in rows if r['group_id']]
