"""Bookkeeping of the seven-day setup deadline: when the bot was added to a
community, whether that community was ever set up, and the moment the rule
itself came into force.

The policy that reads all of this lives in `setup_deadline.py`; this module
only stores and answers.

Two asymmetries are worth knowing. First, Discord tells the bot when it
joined a server (`Guild.me.joined_at`) and Telegram does not, so a Telegram
row is written by the `my_chat_member` update that adds the bot and a Discord
row is written by `on_guild_join` merely as a record — the sweep trusts
Discord's own timestamp, which no restart can lose. Second, `rule_since` is
planted on the first start of the version that introduced the rule and is
never rewritten: every community the bot was already sitting in joined before
that moment and is therefore out of the rule's reach for good.
"""
import time

from db import conn, cur

def rule_since():
    """The unix time the setup deadline came into force, planted on first
    call and stable ever after.

    Everything the bot joined before that instant is grandfathered: the sweep
    compares a community's join time against this number and leaves the older
    ones alone, which is what keeps a deployment from walking out of the
    servers it was already in the day the rule shipped."""
    row = cur.execute(
        "SELECT value FROM bot_settings WHERE key='setup_rule_since'"
    ).fetchone()
    if row and row["value"]:
        try:
            return int(row["value"])
        except ValueError:
            pass
    now = int(time.time())
    cur.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('setup_rule_since', ?)",
        (str(now),)
    )
    conn.commit()
    return now

def record_join(platform, server_id, joined_at=None):
    """Remember that the bot has just been added to a community.

    Does nothing when a row already exists: a Telegram promotion that follows
    the join, or a Discord GUILD_CREATE the library replays, must not restart
    a deadline — least of all a settled one."""
    cur.execute(
        "INSERT OR IGNORE INTO setup_deadlines (platform, server_id, joined_at)"
        " VALUES (?,?,?)",
        (platform, str(server_id), int(joined_at if joined_at is not None else time.time()))
    )
    conn.commit()

def get_deadline_row(platform, server_id):
    """The community's deadline row, or None when it has never been recorded
    (which is what every community from before the rule looks like)."""
    return cur.execute(
        "SELECT * FROM setup_deadlines WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    ).fetchone()

def get_pending_deadlines(platform):
    """Communities of this platform still under the deadline: recorded, and
    not yet found configured."""
    return cur.execute(
        "SELECT * FROM setup_deadlines WHERE platform=? AND settled_at IS NULL",
        (platform,)
    ).fetchall()

def mark_settled(platform, server_id):
    """Note that the community has been set up, which takes it out of the
    rule for good. Writes a settled row even for a community that has none —
    a server from before the rule that gets set up later then carries the
    same proof as any other."""
    now = int(time.time())
    cur.execute(
        "INSERT INTO setup_deadlines (platform, server_id, joined_at, settled_at)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(platform, server_id) DO UPDATE SET settled_at=excluded.settled_at",
        (platform, str(server_id), now, now)
    )
    conn.commit()

def forget_deadline(platform, server_id):
    """Drop the row — used once the bot has left the community, so a later
    re-invitation is a fresh start rather than an instant second eviction."""
    cur.execute(
        "DELETE FROM setup_deadlines WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    )
    conn.commit()

_CONFIGURED_QUERIES = (
    ("SELECT 1 FROM chats WHERE platform=? AND chat_id LIKE ? LIMIT 1", "like"),
    ("SELECT 1 FROM feeds WHERE platform=? AND chat_id LIKE ? LIMIT 1", "like"),
    ("SELECT 1 FROM inbox_hosts WHERE platform=? AND chat_id LIKE ? LIMIT 1", "like"),
    ("SELECT 1 FROM chat_admins WHERE platform=? AND chat_id LIKE ? LIMIT 1", "like"),
    ("SELECT 1 FROM server_admins WHERE platform=? AND server_id=? LIMIT 1", "exact"),
    ("SELECT 1 FROM server_bridge_admins WHERE platform=? AND server_id=? LIMIT 1", "exact"),
)

def community_is_configured(platform, server_id):
    """Whether a Bot Admin has ever done anything with this server or group.

    The six tables asked are exactly the ones no one but a Bot Admin can put
    a first row into: a bridge attachment (`/atb`), a followed source
    (`/set*feed`), an inbox host (`/setinboxchat`), a Bridge Admin grant
    (`/setadmin`, in either scope) and a Local Admin grant
    (`/setlocaladmin`). Everything else a community can configure — language,
    dead-chat pings, webhooks, file consent — is open to Chat and Bridge
    Admins, and those exist only because one of the grants above created
    them, so a row in any of those tables implies a row in one of these.

    `server_id` is the bare community id: a Discord guild id or a Telegram
    group id. Chat keys are '<community>:<channel|topic>', so the LIKE is
    anchored by the colon and cannot reach a longer id that merely starts
    with the same digits."""
    prefix = str(server_id)
    like = f"{prefix}:%"
    for sql, shape in _CONFIGURED_QUERIES:
        if cur.execute(sql, (platform, like if shape == "like" else prefix)).fetchone():
            return True
    return False
