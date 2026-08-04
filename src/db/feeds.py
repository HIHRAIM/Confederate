"""Followed-source rows (the feeds table) and their target resolution.

A feed is attached to one chat but delivers to that chat's whole bridge,
resolved per post by feed_targets — that is what makes a subscription follow
bridge membership with no migration code. Not this module's zone: fetching
and rendering the posts (sources/*, discord_bot/feeds.py) or the polling
schedule (main.py: feed_loop).
"""
from db import conn, cur
from db.bridges import get_bridge_chats

def add_feed(kind, source, platform, chat_id, source_id=None, title=None,
             last_post_id=None, live=False, added_by=None):
    """Attach an outside source (a Bluesky account, a YouTube or Telegram channel) to a chat.

    `last_post_id` is the newest post at the moment of attaching, stored so the
    feed starts with what comes next instead of replaying the backlog. `live`
    marks a source that pushes its posts to the bot — a Telegram channel the bot
    administrates — and is therefore never polled."""
    cur.execute(
        "INSERT INTO feeds (kind, source, chat_id, platform, source_id, title,"
        " last_post_id, live, added_by, added_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,strftime('%s','now'))"
        " ON CONFLICT(kind, source, chat_id) DO UPDATE SET"
        " platform=excluded.platform, source_id=excluded.source_id,"
        " title=excluded.title, live=excluded.live,"
        " added_by=excluded.added_by, added_at=excluded.added_at",
        (kind, source.lower(), str(chat_id), platform,
         str(source_id) if source_id is not None else None, title,
         str(last_post_id) if last_post_id is not None else None,
         1 if live else 0,
         str(added_by) if added_by is not None else None)
    )
    conn.commit()

def remove_feed(kind, source, chat_id) -> bool:
    """Detach a source from the chat it was attached in. Returns False when
    there was nothing to remove — the /rem*feed commands turn that into a
    'not attached' reply.

    A wiki's per-subscription settings go with it: leaving them behind would
    silently resurrect old filters if the same wiki were attached again."""
    if not cur.execute(
        "SELECT 1 FROM feeds WHERE kind=? AND source=? AND chat_id=?",
        (kind, source.lower(), str(chat_id))
    ).fetchone():
        return False
    cur.execute(
        "DELETE FROM feeds WHERE kind=? AND source=? AND chat_id=?",
        (kind, source.lower(), str(chat_id))
    )
    cur.execute(
        "DELETE FROM wiki_feed_settings WHERE kind=? AND source=? AND chat_id=?",
        (kind, source.lower(), str(chat_id))
    )
    conn.commit()
    return True

_WIKI_SETTING_COLUMNS = (
    "event_types", "namespaces", "show_bots", "show_minor",
    "discord_format", "delivery", "webhook",
)

def wiki_settings_chat(source, chat_id):
    """The chat id a wiki's settings row is keyed on, given any chat that can
    see the subscription.

    A wiki is followed once per bridge and configured once per bridge, so its
    settings belong to the subscription rather than to whichever chat an admin
    happened to type the command in: `find_feed` walks the bridge and hands
    back the feeds row, and that row's chat is the key. A chat with no
    subscription in reach keys on itself — there is nothing else to key on,
    and the commands refuse the change long before a row could be written."""
    feed = find_feed("wiki", source, chat_id)
    return str(feed["chat_id"]) if feed else str(chat_id)

def get_wiki_feed_settings(source, chat_id):
    """The filters and output settings of one wiki subscription, as seen from
    any chat of its bridge.

    Always returns a usable dict, whether or not anyone has configured
    anything: the defaults live here rather than in the table, so changing
    them does not need a migration. `event_types` comes back as a set of
    group names and `namespaces` as a set of numbers or None for 'every
    namespace' — the two shapes `wiki_events.event_matches` expects.
    `configured` says whether a row existed, which is what lets the settings
    listing show 'default' instead of repeating the defaults back."""
    import wiki_events

    row = cur.execute(
        "SELECT * FROM wiki_feed_settings WHERE kind='wiki' AND source=? AND chat_id=?",
        (str(source).lower(), wiki_settings_chat(source, chat_id))
    ).fetchone()

    default_groups = {g for g in wiki_events.EVENT_GROUPS
                      if g not in wiki_events.DEFAULT_DISABLED_GROUPS}
    if not row:
        return {"event_types": default_groups, "namespaces": None,
                "show_bots": False, "show_minor": True,
                "discord_format": "embed", "delivery": "auto",
                "webhook": "own", "configured": False}

    raw_groups = row["event_types"]
    raw_namespaces = row["namespaces"]
    return {
        "event_types": ({g for g in str(raw_groups).split(",") if g}
                        if raw_groups is not None else default_groups),
        "namespaces": ({int(n) for n in str(raw_namespaces).split(",") if n}
                       if raw_namespaces else None),
        "show_bots": bool(row["show_bots"]),
        "show_minor": bool(row["show_minor"]),
        "discord_format": row["discord_format"] or "embed",
        "delivery": row["delivery"] or "auto",
        "webhook": (row["webhook"] if "webhook" in row.keys() else None) or "own",
        "configured": True,
    }

def set_wiki_feed_settings(source, chat_id, changes, updated_by=None):
    """Apply a partial settings change, creating the row on first use. The
    change is made for the whole bridge, from whichever of its chats the admin
    typed the command in.

    `changes` holds only the settings the admin actually named; everything
    else keeps whatever it had, so `/wikifeed-settings … bots:on` does not
    quietly reset the namespace filter. Sets and lists are flattened to the
    comma-separated form the column stores, and a `namespaces` of None means
    'every namespace' rather than 'no namespaces'."""
    stored = {}
    for column in _WIKI_SETTING_COLUMNS:
        if column not in changes:
            continue
        value = changes[column]
        if column in ("event_types", "namespaces"):
            stored[column] = (",".join(str(v) for v in sorted(value))
                              if value is not None else None)
        elif column in ("show_bots", "show_minor"):
            stored[column] = 1 if value else 0
        else:
            stored[column] = str(value)
    if not stored:
        return

    settings_chat = wiki_settings_chat(source, chat_id)
    cur.execute(
        "INSERT OR IGNORE INTO wiki_feed_settings (kind, source, chat_id) VALUES ('wiki',?,?)",
        (str(source).lower(), settings_chat)
    )
    assignments = ", ".join(f"{column}=?" for column in stored)
    cur.execute(
        f"UPDATE wiki_feed_settings SET {assignments},"
        f" updated_by=?, updated_at=strftime('%s','now')"
        f" WHERE kind='wiki' AND source=? AND chat_id=?",
        (*stored.values(), str(updated_by) if updated_by is not None else None,
         str(source).lower(), settings_chat)
    )
    conn.commit()

def feed_targets(platform, chat_id):
    """The chats a feed attached in `chat_id` delivers to.

    Every chat of its bridge when the chat has one — including chats that join
    the bridge later, since this is resolved on each post — and otherwise the
    chat it was attached in, on its own."""
    row = cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if row and row["bridge_id"] is not None:
        chats = get_bridge_chats(row["bridge_id"])
        if chats:
            return chats
    return [{"platform": platform, "chat_id": str(chat_id)}]

def find_feed(kind, source, chat_id):
    """The feed row that already delivers `source` into `chat_id` — attached
    there, or in another chat of the same bridge. Used to keep one bridge from
    following the same source twice and posting everything double."""
    source = source.lower()
    row = cur.execute(
        "SELECT * FROM feeds WHERE kind=? AND source=? AND chat_id=?",
        (kind, source, str(chat_id))
    ).fetchone()
    if row:
        return row

    bridge = cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if not bridge or bridge["bridge_id"] is None:
        return None
    for chat in get_bridge_chats(bridge["bridge_id"]):
        row = cur.execute(
            "SELECT * FROM feeds WHERE kind=? AND source=? AND chat_id=?",
            (kind, source, chat["chat_id"])
        ).fetchone()
        if row:
            return row
    return None

def get_bridge_feeds(bridge_id):
    """Feeds attached in any chat of a bridge, for `/bridge`."""
    return cur.execute(
        "SELECT f.* FROM feeds f JOIN chats c ON c.chat_id = f.chat_id"
        " WHERE c.bridge_id=? ORDER BY f.kind, f.source",
        (int(bridge_id),)
    ).fetchall()

def get_chat_feeds(kind, chat_id):
    """Feeds of one kind attached in this exact chat — what a chat outside a
    bridge can list, where `get_bridge_feeds` has no bridge to work from."""
    return cur.execute(
        "SELECT * FROM feeds WHERE kind=? AND chat_id=? ORDER BY source",
        (kind, str(chat_id))
    ).fetchall()

def get_all_feeds(kind=None):
    """Every feed row, optionally of one kind — the poll scheduler's working
    set (it skips live rows itself)."""
    if kind is None:
        return cur.execute("SELECT * FROM feeds ORDER BY kind, source").fetchall()
    return cur.execute(
        "SELECT * FROM feeds WHERE kind=? ORDER BY source", (kind,)
    ).fetchall()

def get_feeds_by_source_id(kind, source_id):
    """Every attachment of one source, found by the id its platform uses — how a
    live Telegram channel post is matched to the chats waiting for it."""
    return cur.execute(
        "SELECT * FROM feeds WHERE kind=? AND source_id=?", (kind, str(source_id))
    ).fetchall()

def set_feed_last_post(kind, source, chat_id, last_post_id, title=None):
    """Advance a feed's read position (and refresh the stored title when the
    fetch produced one). Each attachment advances its own position, so two
    chats following one source never skip posts for each other."""
    if title is None:
        cur.execute(
            "UPDATE feeds SET last_post_id=? WHERE kind=? AND source=? AND chat_id=?",
            (str(last_post_id), kind, source.lower(), str(chat_id))
        )
    else:
        cur.execute(
            "UPDATE feeds SET last_post_id=?, title=? WHERE kind=? AND source=? AND chat_id=?",
            (str(last_post_id), title, kind, source.lower(), str(chat_id))
        )
    conn.commit()
