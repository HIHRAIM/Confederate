"""What the database knows about sponsors: the Discord <-> Telegram identity
of a person, their Patreon tier as the bot last saw it, the communities they
have claimed and the bridge numbers they own.

The *policy* -- how a tier is read off Discord's roles, what a tier is worth,
when a lapsed subscription freezes and when its grace runs out -- lives in
src/sponsors.py. This module only stores and answers.

One idea runs through all of it, and getting it wrong is the whole security
of the feature: **the Discord account is the owner**. Patreon binds a
subscription to a Discord account and offers nothing for Telegram, so a
Telegram account is a sponsor only by being linked to one, and a linked pair
holds ONE set of rights and quotas between them, never one each. That is what
`canonical_sponsor_id` is for, and every sponsor question in the codebase
starts by asking it.
"""
import time

from db import conn, cur
from db.ranges import bridge_range

LINK_WINDOW_SECONDS = 30 * 60

def link_accounts(discord_id, telegram_id):
    """Bind one Discord account to one Telegram account, replacing whatever
    either of them was bound to before.

    One-to-one in both directions on purpose: two Telegram accounts sharing
    one Discord account would be two people holding one subscription's
    rights, which is exactly what the linking exists to prevent."""
    cur.execute(
        "DELETE FROM account_links WHERE discord_id=? OR telegram_id=?",
        (str(discord_id), str(telegram_id))
    )
    cur.execute(
        "INSERT INTO account_links (discord_id, telegram_id, linked_at) VALUES (?,?,?)",
        (str(discord_id), str(telegram_id), int(time.time()))
    )
    conn.commit()

def get_link_by_discord(discord_id):
    """The link row a Discord account belongs to, or None."""
    return cur.execute(
        "SELECT * FROM account_links WHERE discord_id=?", (str(discord_id),)
    ).fetchone()

def get_link_by_telegram(telegram_id):
    """The same relation read from the Telegram side, or None."""
    return cur.execute(
        "SELECT * FROM account_links WHERE telegram_id=?", (str(telegram_id),)
    ).fetchone()

def unlink_accounts(platform, user_id):
    """Break the link either side belongs to. Returns the row that was
    removed, or None when there was none.

    The effect is immediate and deliberate: a Telegram account holds sponsor
    rights only through the link, so dropping it takes those rights away in
    the same breath. Nothing else is deleted -- claimed communities and owned
    bridges belong to the Discord account and stay with it."""
    row = (get_link_by_discord(user_id) if platform == "discord"
           else get_link_by_telegram(user_id))
    if not row:
        return None
    cur.execute("DELETE FROM account_links WHERE discord_id=?", (row["discord_id"],))
    conn.commit()
    return row

def canonical_sponsor_id(platform, user_id):
    """The Discord account that owns this person's sponsor status, or None
    when there is none to own it.

    A Discord caller is their own answer. A Telegram caller is answered by
    their link, and by nothing else: an unlinked Telegram account cannot be a
    sponsor however much its owner pays, because the bot has no way to know
    the two are the same person."""
    if platform == "discord":
        return str(user_id)
    row = get_link_by_telegram(user_id)
    return str(row["discord_id"]) if row and row["discord_id"] else None

def add_pending_link(platform, user_id, username, claimed_other):
    """Store one half of the linking handshake, replacing this account's
    previous claim if it had one."""
    cur.execute(
        "INSERT OR REPLACE INTO pending_links"
        " (platform, user_id, username, claimed_other, created_at) VALUES (?,?,?,?,?)",
        (platform, str(user_id), username, claimed_other, int(time.time()))
    )
    conn.commit()

def get_pending_links(platform, max_age_seconds=LINK_WINDOW_SECONDS):
    """The still-valid half-handshakes made from one platform.

    The thirty minutes are enforced here, in the query, and not only by the
    sweep: an expired row the sweep has not reached yet must not be able to
    complete a link."""
    cutoff = int(time.time()) - max_age_seconds
    return cur.execute(
        "SELECT * FROM pending_links WHERE platform=? AND created_at>=?",
        (platform, cutoff)
    ).fetchall()

def remove_pending_link(platform, user_id):
    """Drop one half-handshake; `register_link_attempt` removes both once a
    link completes."""
    cur.execute(
        "DELETE FROM pending_links WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    )
    conn.commit()

def cleanup_old_pending_links(max_age_seconds=LINK_WINDOW_SECONDS):
    """Sweep the handshake window. Runs from main.py: pending_cleanup_loop
    beside the other expiries; the reads enforce the deadline themselves, so
    this only keeps the table small."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute("DELETE FROM pending_links WHERE created_at<?", (cutoff,))
    conn.commit()

def _name_eq(a, b):
    """Compare two usernames the way a person types them: ignoring case,
    surrounding spaces and a leading '@'. Both halves of the handshake are
    typed by hand on the other platform's keyboard, so exact matching would
    fail far more often than it would protect anything."""
    return (a or "").strip().lstrip("@").lower() == (b or "").strip().lstrip("@").lower()

def register_link_attempt(platform, user_id, username, claimed_other):
    """Record the caller's half of the handshake and complete the link if the
    matching half is already waiting.

    The match has to be MUTUAL -- this side named the other and is itself the
    one the other side named -- which is what makes it impossible to attach
    somebody else's account to your own by naming it loudly enough. Returns
    ``('linked', discord_id, telegram_id)`` or ``('pending', None, None)``."""
    add_pending_link(platform, str(user_id), username, claimed_other)
    other = "telegram" if platform == "discord" else "discord"
    for p in get_pending_links(other):
        if _name_eq(p["claimed_other"], username) and _name_eq(p["username"], claimed_other):
            discord_id = str(user_id) if platform == "discord" else str(p["user_id"])
            telegram_id = str(user_id) if platform == "telegram" else str(p["user_id"])
            link_accounts(discord_id, telegram_id)
            remove_pending_link(platform, str(user_id))
            remove_pending_link(other, p["user_id"])
            return "linked", discord_id, telegram_id
    return "pending", None, None

def get_sponsor_state(discord_id):
    """The stored tier and freeze bookkeeping of one Discord account, or
    None when the bot has never seen it hold a tier."""
    return cur.execute(
        "SELECT * FROM sponsor_state WHERE discord_id=?", (str(discord_id),)
    ).fetchone()

def save_sponsor_state(discord_id, *, tier, since=None, grace_from_tier=None,
                       grace_since=None, notified_grace=0, notified_expired=0,
                       slot_changed_at=None):
    """Write one account's whole sponsor state.

    Whole rather than field-by-field because the fields are one statement
    about the account -- a tier with a grace period attached to it -- and a
    partial write is how the two halves come to disagree."""
    cur.execute(
        "INSERT INTO sponsor_state (discord_id, tier, since, grace_from_tier,"
        " grace_since, notified_grace, notified_expired, slot_changed_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(discord_id) DO UPDATE SET tier=excluded.tier,"
        " since=excluded.since, grace_from_tier=excluded.grace_from_tier,"
        " grace_since=excluded.grace_since, notified_grace=excluded.notified_grace,"
        " notified_expired=excluded.notified_expired,"
        " slot_changed_at=excluded.slot_changed_at, updated_at=excluded.updated_at",
        (str(discord_id), int(tier), since, grace_from_tier, grace_since,
         int(notified_grace), int(notified_expired), slot_changed_at, int(time.time()))
    )
    conn.commit()

def mark_sponsor_notified(discord_id, field):
    """Record that one of the two notices has gone out. Called right after
    the message is sent, so a bot that dies mid-notice repeats it rather than
    swallowing it -- the failure everyone would rather have."""
    if field not in ("notified_grace", "notified_expired"):
        raise ValueError(field)
    cur.execute(
        f"UPDATE sponsor_state SET {field}=1, updated_at=? WHERE discord_id=?",
        (int(time.time()), str(discord_id))
    )
    conn.commit()

def touch_sponsor_slot_change(discord_id):
    """Stamp the moment a community slot was released, which is what the
    rotation limit counts from."""
    cur.execute(
        "UPDATE sponsor_state SET slot_changed_at=?, updated_at=? WHERE discord_id=?",
        (int(time.time()), int(time.time()), str(discord_id))
    )
    conn.commit()

def get_known_sponsors():
    """Every account the bot has ever recorded a tier for.

    The reconciliation pass walks this together with the current role
    holders: somebody whose role has just gone is in here and in no role, and
    that difference is the entire point of the pass."""
    return cur.execute("SELECT * FROM sponsor_state").fetchall()

def get_sponsors_in_grace():
    """The accounts whose tier has dropped and whose grace period has not
    been resolved yet."""
    return cur.execute(
        "SELECT * FROM sponsor_state WHERE grace_since IS NOT NULL"
    ).fetchall()

def claim_community(platform, server_id, discord_id):
    """Take a community's slot for a sponsor. Returns True, or False when
    somebody else already holds it."""
    cur.execute(
        "INSERT OR IGNORE INTO sponsor_communities"
        " (platform, server_id, discord_id, claimed_at) VALUES (?,?,?,?)",
        (platform, str(server_id), str(discord_id), int(time.time()))
    )
    conn.commit()
    row = get_community_claim(platform, server_id)
    return bool(row and str(row["discord_id"]) == str(discord_id))

def release_community(platform, server_id, discord_id=None):
    """Give a community's slot back. With `discord_id` the release only
    happens if that account is the holder, so one sponsor cannot free
    another's slot."""
    if discord_id is None:
        deleted = cur.execute(
            "DELETE FROM sponsor_communities WHERE platform=? AND server_id=?",
            (platform, str(server_id))
        )
    else:
        deleted = cur.execute(
            "DELETE FROM sponsor_communities WHERE platform=? AND server_id=? AND discord_id=?",
            (platform, str(server_id), str(discord_id))
        )
    conn.commit()
    return bool(deleted.rowcount)

def get_community_claim(platform, server_id):
    """Who holds this community's slot, or None."""
    return cur.execute(
        "SELECT * FROM sponsor_communities WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    ).fetchone()

def get_sponsor_communities(discord_id):
    """Everything one sponsor has claimed, oldest claim first.

    The order is not cosmetic: when a downgrade leaves more slots claimed
    than the new tier allows, the ones released are the newest, and this is
    the order that decides which those are."""
    return cur.execute(
        "SELECT * FROM sponsor_communities WHERE discord_id=? ORDER BY claimed_at, server_id",
        (str(discord_id),)
    ).fetchall()

_CLAIM_SPONSOR_BRIDGE_ID_SQL = """
INSERT INTO bridges (id)
SELECT next FROM (
    SELECT MAX(candidate) AS next FROM (
        SELECT COALESCE(MAX(id) + 1, :floor) AS candidate FROM bridges
         WHERE id >= :floor AND id < :ceiling
        UNION ALL
        SELECT COALESCE(MAX(bridge_id) + 1, :floor) FROM sponsor_bridges
         WHERE bridge_id >= :floor AND bridge_id < :ceiling
    )
) WHERE next < :ceiling
"""

def claim_sponsor_bridge_id(discord_id):
    """Take the next number of the sponsor region for `discord_id`, or None
    when the region is full.

    max+1 like the appeal and inbox allocators, with one difference that is
    the reason the region exists at all: the maximum is taken over
    `sponsor_bridges` as well as over `bridges`. A sponsor bridge that has
    lost its last chat has no `bridges` row any more -- the number would look
    free -- but its ownership row is still there, so the number stays spoken
    for and its owner can bring the bridge back by attaching a chat to it.

    Claimed with a single INSERT ... SELECT for the reason db/bridges.py:
    attach_chat_to_new_bridge spells out: SQLite evaluates one statement
    under its write lock, so two sponsors opening a bridge in the same
    instant cannot be handed one number."""
    floor, ceiling = bridge_range("sponsor")
    claimed = cur.execute(_CLAIM_SPONSOR_BRIDGE_ID_SQL,
                          {"floor": floor, "ceiling": ceiling})
    conn.commit()
    if not claimed.rowcount:
        return None
    bridge_id = int(claimed.lastrowid)
    cur.execute(
        "INSERT OR IGNORE INTO sponsor_bridges (bridge_id, discord_id, created_at)"
        " VALUES (?,?,?)",
        (bridge_id, str(discord_id), int(time.time()))
    )
    conn.commit()
    return bridge_id

def get_sponsor_bridge(bridge_id):
    """The ownership row of a sponsor bridge number, or None. Present for
    every number the allocator ever handed out, whether or not a bridge is
    standing on it right now."""
    return cur.execute(
        "SELECT * FROM sponsor_bridges WHERE bridge_id=?", (int(bridge_id),)
    ).fetchone()

def get_sponsor_bridges(discord_id):
    """Every sponsor bridge number one account owns, in the order taken."""
    return cur.execute(
        "SELECT * FROM sponsor_bridges WHERE discord_id=? ORDER BY bridge_id",
        (str(discord_id),)
    ).fetchall()

def frozen_sponsor_chat_ids():
    """The chats whose feeds must not be relayed right now: those of sponsor
    bridges whose owner is not currently a paying sponsor.

    A frozen feed keeps its row, its settings and its place -- what stops is
    only the delivery, and it starts again by itself the moment the
    subscription comes back. Ordinary relay between the chats of the bridge is
    untouched: people talking to each other is not what the sponsorship paid
    for.

    One query per feed tick, so it is written as one query."""
    rows = cur.execute(
        "SELECT c.chat_id AS chat_id FROM chats c"
        " JOIN sponsor_bridges sb ON sb.bridge_id = c.bridge_id"
        " LEFT JOIN sponsor_state ss ON ss.discord_id = sb.discord_id"
        " WHERE COALESCE(ss.tier, 0) = 0"
    ).fetchall()
    return {r["chat_id"] for r in rows}
