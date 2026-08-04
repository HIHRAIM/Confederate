"""Delegated roles: Bridge Admins (per-bridge and server-wide), Local Admins
and Localizers.

The role model is described in ARCHITECTURE.md. The recurring trick here is
materialization: a grant is stored once in its own table *and* copied into
the per-chat chat_admins rows that utils.is_chat_admin reads, so permission
checks stay one indexed lookup. attach_chat (db/bridges.py) does the same
copying when a chat joins a bridge later.

Not this module's zone: Bot Admins (hard-coded in config.ADMINS, checked by
utils.is_admin) and shadow bans (db/users.py).
"""
from db import conn, cur
from db.bridges import chat_server_id, get_bridge_chats, server_bridge_ids

def add_bridge_admin(bridge_id, user_id):
    """Grant Bridge Admin for one bridge (`/setadmin scope: local`), fanning
    the grant out into chat_admins rows for every current member chat."""
    cur.execute(
        "INSERT OR IGNORE INTO bridge_admins (bridge_id, user_id) VALUES(?,?)",
        (bridge_id, str(user_id))
    )
    rows = cur.execute("SELECT platform, chat_id FROM chats WHERE bridge_id=?", (bridge_id,)).fetchall()
    for r in rows:
        cur.execute(
            "INSERT OR IGNORE INTO chat_admins (platform, chat_id, user_id) VALUES (?,?,?)",
            (r["platform"], r["chat_id"], str(user_id))
        )
    conn.commit()

def remove_bridge_admin(bridge_id, user_id):
    """Revoke a per-bridge grant and its chat_admins fan-out. Note this also
    removes rows that a still-standing server-wide grant would re-create — the
    server-wide path calls this per bridge and re-materializes on next attach."""
    cur.execute(
        "DELETE FROM bridge_admins WHERE bridge_id=? AND user_id=?",
        (bridge_id, str(user_id))
    )
    rows = cur.execute("SELECT platform, chat_id FROM chats WHERE bridge_id=?", (bridge_id,)).fetchall()
    for r in rows:
        cur.execute(
            "DELETE FROM chat_admins WHERE platform=? AND chat_id=? AND user_id=?",
            (r["platform"], r["chat_id"], str(user_id))
        )
    conn.commit()

def get_bridge_admins(bridge_id):
    """Everyone holding Bridge Admin rights in this bridge — granted for the
    bridge itself with `/setadmin scope: local`, or across a server/group with
    plain `/setadmin`, which covers the bridges it joins later too."""
    ids = {r["user_id"] for r in cur.execute(
        "SELECT user_id FROM bridge_admins WHERE bridge_id=?", (bridge_id,)
    ).fetchall()}
    for chat in get_bridge_chats(bridge_id):
        server_id = chat_server_id(chat["platform"], chat["chat_id"])
        if not server_id:
            continue
        ids.update(r["user_id"] for r in cur.execute(
            "SELECT user_id FROM server_bridge_admins WHERE platform=? AND server_id=?",
            (chat["platform"], server_id)
        ).fetchall())
    return sorted(ids)

def add_server_bridge_admin(platform, server_id, user_id, added_by=None):
    """Bridge Admin rights across a whole server/group: every bridge it takes
    part in now, and every bridge it joins later.

    The bridges it is in right now also get an ordinary `bridge_admins` row, so
    the per-bridge and per-chat lookups elsewhere see the grant immediately."""
    cur.execute(
        "INSERT INTO server_bridge_admins (platform, server_id, user_id, added_by, added_at)"
        " VALUES (?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(platform, server_id, user_id) DO UPDATE SET"
        " added_by=excluded.added_by, added_at=excluded.added_at",
        (platform, str(server_id), str(user_id),
         str(added_by) if added_by is not None else None)
    )
    for bridge_id in server_bridge_ids(platform, server_id):
        add_bridge_admin(bridge_id, user_id)
    conn.commit()

def remove_server_bridge_admin(platform, server_id, user_id) -> bool:
    """Revoke a server-wide grant together with the per-bridge rows it
    materialized. Returns True when a grant actually existed."""
    existed = cur.execute(
        "SELECT 1 FROM server_bridge_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    ).fetchone() is not None
    cur.execute(
        "DELETE FROM server_bridge_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    for bridge_id in server_bridge_ids(platform, server_id):
        remove_bridge_admin(bridge_id, user_id)
    conn.commit()
    return existed

def is_server_bridge_admin(platform, server_id, user_id) -> bool:
    """Whether the user holds the server-wide Bridge Admin grant itself (not
    merely a materialized per-bridge row)."""
    return cur.execute(
        "SELECT 1 FROM server_bridge_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    ).fetchone() is not None

def add_server_admin(platform, server_id, user_id, username=None, added_by=None):
    """Delegate server-wide Local Admin rights (set with /setlocaladmin).
    The username, when known, is kept for the control panel's username login."""
    cur.execute(
        "INSERT INTO server_admins (platform, server_id, user_id, username, added_by, added_at)"
        " VALUES (?,?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(platform, server_id, user_id) DO UPDATE SET"
        " username=COALESCE(excluded.username, server_admins.username)",
        (platform, str(server_id), str(user_id), username,
         str(added_by) if added_by is not None else None)
    )
    conn.commit()

def remove_server_admin(platform, server_id, user_id):
    """Revoke a Local Admin grant (/remlocaladmin)."""
    cur.execute(
        "DELETE FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    conn.commit()

def is_server_admin(platform, server_id, user_id):
    """Whether the user is a Local Admin of the server — one of the checks
    inside utils.is_chat_admin, and the panel's scoped-login criterion."""
    return cur.execute(
        "SELECT 1 FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    ).fetchone() is not None

def add_localizer(platform, user_id, username=None, added_by=None):
    """Grant localizer status (set with /localizer-add): the user may edit
    this bot's localization through the control panel.  The username, when
    known, is kept for the panel's username login."""
    cur.execute(
        "INSERT INTO localizers (platform, user_id, username, added_by, added_at)"
        " VALUES (?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(platform, user_id) DO UPDATE SET"
        " username=COALESCE(excluded.username, localizers.username)",
        (platform, str(user_id), username,
         str(added_by) if added_by is not None else None)
    )
    conn.commit()

def remove_localizer(platform, user_id):
    """Revoke a delegated localizer status.  Returns True when a row existed
    (admins are localizers implicitly and have no row to remove)."""
    removed = cur.execute(
        "DELETE FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).rowcount
    conn.commit()
    return removed > 0

def is_localizer(platform, user_id):
    """Whether the user holds a delegated localizer grant (bot admins pass the
    panel's own check without a row here)."""
    return cur.execute(
        "SELECT 1 FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone() is not None
