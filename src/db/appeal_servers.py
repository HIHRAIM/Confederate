"""Independent sponsor appeal servers with the operator config as fallback."""
import time

from config import (
    APPEAL_BANINFO_CHANNELS, APPEAL_CHANNEL_ID, APPEAL_PARDON_CHANNELS,
    CONSULS, PURGATORIUM_GUILD_ID, PURGATORIUM_INVITE_URL,
)
from db import conn, cur

def appeal_owner(guild_id):
    """None for operator, sponsor Discord ID for a registered server, or False."""
    if str(guild_id) == str(PURGATORIUM_GUILD_ID):
        return None
    row = cur.execute("SELECT owner_id FROM appeal_servers WHERE guild_id=?",
                      (str(guild_id),)).fetchone()
    return row["owner_id"] if row else False

def appeal_settings(guild_id=None):
    """One server's effective settings; None when it is not registered."""
    if guild_id is None or str(guild_id) == str(PURGATORIUM_GUILD_ID):
        return {
            "guild_id": str(PURGATORIUM_GUILD_ID), "owner_id": None,
            "thread_channel_id": str(APPEAL_CHANNEL_ID),
            "invite_url": PURGATORIUM_INVITE_URL,
            "pardon_channels": {str(x) for x in APPEAL_PARDON_CHANNELS.get("discord", set())},
            "baninfo_channels": {str(x) for x in APPEAL_BANINFO_CHANNELS.get("discord", set())},
            "consul_roles": {int(x) for x in CONSULS}, "accepting": True,
        }
    row = cur.execute("SELECT * FROM appeal_servers WHERE guild_id=?",
                      (str(guild_id),)).fetchone()
    if row is None:
        return None
    return {
        "guild_id": row["guild_id"], "owner_id": row["owner_id"],
        "thread_channel_id": row["thread_channel_id"],
        "invite_url": row["invite_url"],
        "pardon_channels": {row["pardon_channel_id"]} if row["pardon_channel_id"] else set(),
        "baninfo_channels": {row["baninfo_channel_id"]} if row["baninfo_channel_id"] else set(),
        "consul_roles": {int(x) for x in (row["consul_roles"] or "").split(",") if x.isdecimal()},
        "accepting": bool(row["accepting"]),
    }

def all_appeal_servers():
    """The default operator server and every registered sponsor server."""
    return [appeal_settings()] + [appeal_settings(row["guild_id"]) for row in
                                  cur.execute("SELECT guild_id FROM appeal_servers").fetchall()]

def appeal_server_for_owner(owner_id):
    """The one sponsor server owned by this account, if registered."""
    return cur.execute("SELECT * FROM appeal_servers WHERE owner_id=?",
                       (str(owner_id),)).fetchone()

def register_appeal_server(guild_id, owner_id, thread_channel_id, invite_url,
                           allow_existing=False):
    """Update an owned server; Bot Admin may edit without taking ownership."""
    now = int(time.time())
    result = cur.execute(
        "INSERT INTO appeal_servers"
        " (guild_id,owner_id,thread_channel_id,invite_url,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET"
        " thread_channel_id=excluded.thread_channel_id,"
        " invite_url=excluded.invite_url,updated_at=excluded.updated_at"
        " WHERE owner_id=excluded.owner_id OR ?",
        (str(guild_id), str(owner_id), str(thread_channel_id), invite_url, now, now,
         int(bool(allow_existing)))
    )
    conn.commit()
    return bool(result.rowcount)

def set_appeal_channels(guild_id, pardon_id, baninfo_id, role_ids):
    """Store protocol channels and consul roles after command validation."""
    cur.execute(
        "UPDATE appeal_servers SET pardon_channel_id=?,baninfo_channel_id=?,"
        " consul_roles=?,updated_at=? WHERE guild_id=?",
        (str(pardon_id), str(baninfo_id), ",".join(str(x) for x in role_ids),
         int(time.time()), str(guild_id))
    )
    conn.commit()

def set_appeal_accepting(guild_id, accepting):
    """Pause or resume new appeals without interrupting open ones."""
    cur.execute("UPDATE appeal_servers SET accepting=?,updated_at=? WHERE guild_id=?",
                (int(bool(accepting)), int(time.time()), str(guild_id)))
    conn.commit()
