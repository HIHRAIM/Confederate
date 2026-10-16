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

def observe_community_role(discord_id, present):
    """Track first observation of either qualifying role across restarts."""
    owner = str(discord_id)
    now = int(time.time())
    row = get_sponsor_state(owner)
    if row is None and not present:
        return
    if row is None:
        cur.execute("INSERT INTO sponsor_state"
                    " (discord_id,tier,community_role_since,community_role_present,updated_at)"
                    " VALUES (?,0,?,1,?)", (owner, now, now))
    elif present and not row["community_role_present"]:
        cur.execute("UPDATE sponsor_state SET community_role_since=?,community_role_present=1"
                    " WHERE discord_id=?", (now, owner))
    elif not present and row["community_role_present"]:
        cur.execute("UPDATE sponsor_state SET community_role_present=0 WHERE discord_id=?",
                    (owner,))
    conn.commit()

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

def claim_community(platform, server_id, discord_id, takeover_from=None):
    """Take a community's slot for a sponsor. Returns True, or False when
    somebody else already holds it."""
    if takeover_from is None:
        cur.execute(
            "INSERT OR IGNORE INTO sponsor_communities"
            " (platform, server_id, discord_id, claimed_at) VALUES (?,?,?,?)",
            (platform, str(server_id), str(discord_id), int(time.time()))
        )
    else:
        cur.execute(
            "UPDATE sponsor_communities SET discord_id=?,claimed_at=?"
            " WHERE platform=? AND server_id=? AND discord_id=?",
            (str(discord_id), int(time.time()), platform, str(server_id),
             str(takeover_from)),
        )
    conn.commit()
    row = get_community_claim(platform, server_id)
    return bool(row and str(row["discord_id"]) == str(discord_id))

def set_sponsor_retention_due(discord_id, due_at):
    """Persist the cleanup deadline independently of the work grace clock."""
    cur.execute("UPDATE sponsor_state SET retention_due_at=? WHERE discord_id=?",
                (int(due_at) if due_at is not None else None, str(discord_id)))
    conn.commit()

def due_sponsor_communities(now=None):
    """Claims still held by a fully lapsed account after retention expires."""
    return cur.execute(
        "SELECT sc.* FROM sponsor_communities sc JOIN sponsor_state ss"
        " ON ss.discord_id=sc.discord_id WHERE ss.tier=0"
        " AND ss.retention_due_at IS NOT NULL AND ss.retention_due_at<=?"
        " ORDER BY sc.platform,sc.server_id",
        (int(now if now is not None else time.time()),),
    ).fetchall()

def purge_sponsor_community(platform, server_id, discord_id):
    """Erase one expired community's database footprint after the bot leaves.

    Every query is bound to the exact platform and community prefix. Shared
    bridges retain other chats and their messages; only records originating
    here or delivered here are scrubbed. The ownership check prevents a
    pending cleanup from deleting a server another sponsor claimed.
    """
    owner = str(discord_id)
    server = str(server_id)
    claim = get_community_claim(platform, server)
    if claim is None or str(claim["discord_id"]) != owner:
        return False
    state = get_sponsor_state(owner)
    if (state is None or int(state["tier"] or 0) != 0
            or state["retention_due_at"] is None
            or int(state["retention_due_at"]) > int(time.time())):
        return False

    pattern = f"{server}:%"
    chats = cur.execute(
        "SELECT chat_id,bridge_id FROM chats WHERE platform=? AND chat_id LIKE ?",
        (platform, pattern),
    ).fetchall()
    affected_bridges = {int(row["bridge_id"]) for row in chats
                        if row["bridge_id"] is not None}
    origin_ids = (
        "SELECT id FROM messages WHERE origin_platform=? AND origin_chat_id LIKE ?"
    )
    for table, column in (("message_copies", "message_id"),
                          ("sponsor_relay_queue", "message_id"),
                          ("media_group_members", "message_id"),
                          ("gallery_uploads", "message_id")):
        cur.execute(f"DELETE FROM {table} WHERE {column} IN ({origin_ids})",
                    (platform, pattern))
    cur.execute("DELETE FROM messages WHERE origin_platform=? AND origin_chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM message_copies WHERE platform=? AND chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM sponsor_relay_queue WHERE platform=? AND chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM media_group_members WHERE chat_id LIKE ?", (pattern,))
    cur.execute("DELETE FROM poll_messages WHERE platform=? AND chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM wiki_feed_settings WHERE chat_id LIKE ?", (pattern,))
    cur.execute("DELETE FROM feeds WHERE platform=? AND chat_id LIKE ?",
                (platform, pattern))
    cur.execute("UPDATE bridge_rules SET origin_platform=NULL,"
                " origin_chat_id=NULL,origin_message_id=NULL"
                " WHERE origin_platform=? AND origin_chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM inbox_hosts WHERE platform=? AND chat_id LIKE ?",
                (platform, pattern))
    cur.execute("DELETE FROM inbox_topics WHERE host_chat_id LIKE ?"
                " OR topic_chat_id LIKE ?", (pattern, pattern))
    for row in chats:
        from db.bridges import remove_chat_from_bridge
        remove_chat_from_bridge(row["chat_id"])
    for bridge_id in affected_bridges:
        if cur.execute("SELECT 1 FROM bridges WHERE id=?", (bridge_id,)).fetchone():
            continue
        cur.execute("DELETE FROM poll_votes WHERE poll_id IN"
                    " (SELECT id FROM polls WHERE bridge_id=?)", (bridge_id,))
        cur.execute("DELETE FROM poll_messages WHERE poll_id IN"
                    " (SELECT id FROM polls WHERE bridge_id=?)", (bridge_id,))
        cur.execute("DELETE FROM polls WHERE bridge_id=?", (bridge_id,))
    for table, column in (("dead_chats", "chat_id"),
                          ("news_chats", "chat_id"),
                          ("deadtopic_chats", "chat_id"),
                          ("chat_settings", "chat_id"),
                          ("chat_admins", "chat_id"),
                          ("inaccessible_chats", "chat_id")):
        cur.execute(f"DELETE FROM {table} WHERE {column} LIKE ?", (pattern,))
    cur.execute("DELETE FROM chat_settings WHERE chat_id=?", (server,))
    cur.execute("DELETE FROM verified_users WHERE platform=? AND prefix=?",
                (platform, server))
    cur.execute("DELETE FROM pending_consents WHERE platform=? AND prefix=?",
                (platform, server))
    for table in ("server_admins", "server_bridge_admins",
                  "server_file_consents", "setup_deadlines"):
        cur.execute(f"DELETE FROM {table} WHERE platform=? AND server_id=?",
                    (platform, server))
    cur.execute("DELETE FROM server_webhooks WHERE server_id=?", (server,))
    cur.execute("DELETE FROM bridge_webhooks WHERE server_id=?", (server,))
    cur.execute("DELETE FROM sponsor_communities WHERE platform=?"
                " AND server_id=? AND discord_id=?", (platform, server, owner))
    conn.commit()
    return True

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

def get_sponsor_usage(discord_id):
    """Active bridges, chats and sources for one owner in two indexed reads.

    Historical bridge numbers never disappear, so walking each of them in
    Python would make every quota check grow without bound over time.
    """
    owner = str(discord_id)
    places = cur.execute(
        "SELECT COUNT(DISTINCT c.bridge_id) AS bridges, COUNT(c.chat_id) AS chats"
        " FROM sponsor_bridges sb LEFT JOIN chats c ON c.bridge_id=sb.bridge_id"
        " WHERE sb.discord_id=?", (owner,)
    ).fetchone()
    sources = cur.execute(
        "SELECT f.kind AS kind, COUNT(*) AS used FROM sponsor_bridges sb"
        " JOIN chats c ON c.bridge_id=sb.bridge_id"
        " JOIN feeds f ON f.chat_id=c.chat_id"
        " WHERE sb.discord_id=? GROUP BY f.kind", (owner,)
    ).fetchall()
    counts = {r["kind"]: int(r["used"]) for r in sources}
    return {"bridges": int(places["bridges"]), "chats": int(places["chats"]),
            "wikis": counts.get("wiki", 0),
            "socials": sum(counts.get(kind, 0) for kind in
                           ("bluesky", "youtube", "telegram"))}

def sponsor_relay_usage(discord_id, day):
    """Delivery units spent by this subscription in the named UTC day."""
    return cur.execute(
        "SELECT day_units, minute_started, minute_units FROM sponsor_relay_usage"
        " WHERE discord_id=? AND day=?", (str(discord_id), day)
    ).fetchone()

def claim_sponsor_relay_notice(bridge_id, day):
    """Claim the one bridge-wide daily quota notice before sending it."""
    changed = cur.execute(
        "INSERT OR IGNORE INTO sponsor_relay_notices (bridge_id,day) VALUES (?,?)",
        (int(bridge_id), str(day)),
    )
    conn.commit()
    return bool(changed.rowcount)

def spend_sponsor_relay(discord_id, day, now, units, minute_limit, day_limit):
    """Reserve a whole fan-out before sending any copy.

    One conditional SQLite statement makes the budget atomic even if two
    processes touch the database. A refused relay spends nothing.
    """
    if units <= 0 or units > minute_limit or units > day_limit:
        return False
    changed = cur.execute(
        """INSERT INTO sponsor_relay_usage
           (discord_id, day, day_units, minute_started, minute_units)
           VALUES (:owner, :day, :units, :now, :units)
           ON CONFLICT(discord_id, day) DO UPDATE SET
             day_units = sponsor_relay_usage.day_units + :units,
             minute_started = CASE
               WHEN :now - sponsor_relay_usage.minute_started >= 60 THEN :now
               ELSE sponsor_relay_usage.minute_started END,
             minute_units = CASE
               WHEN :now - sponsor_relay_usage.minute_started >= 60 THEN :units
               ELSE sponsor_relay_usage.minute_units + :units END
           WHERE sponsor_relay_usage.day_units + :units <= :day_limit
             AND (CASE
               WHEN :now - sponsor_relay_usage.minute_started >= 60 THEN :units
               ELSE sponsor_relay_usage.minute_units + :units END) <= :minute_limit""",
        {"owner": str(discord_id), "day": day, "now": now,
         "units": units, "minute_limit": minute_limit, "day_limit": day_limit}
    )
    conn.commit()
    return bool(changed.rowcount)

def frozen_sponsor_chat_ids():
    """The chats whose feeds must not be relayed right now: those of sponsor
    bridges whose owner is not currently a paying sponsor.

    A frozen feed keeps its row, its settings and its place -- what stops is
    delivery, and it starts again by itself the moment the subscription comes
    back. Ordinary messages are also paused by message_relay.relay_message.

    One query per feed tick, so it is written as one query."""
    rows = cur.execute(
        "SELECT c.chat_id AS chat_id FROM chats c"
        " JOIN sponsor_bridges sb ON sb.bridge_id = c.bridge_id"
        " LEFT JOIN sponsor_state ss ON ss.discord_id = sb.discord_id"
        " WHERE COALESCE(ss.tier, 0) = 0"
    ).fetchall()
    return {r["chat_id"] for r in rows}
