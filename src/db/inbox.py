"""Storage for the inbox system: registered receiver bots, the chats they
open conversations in, the conversations themselves, their staff
anonymization indexes and their per-bot bans.

The *runtime* — polling those bots, opening threads and topics, relaying
between a private chat and them — lives in inbox.py; this module only
answers what the database knows. The one piece of policy that belongs here
is the reserved bridge-id range: an inbox conversation is an ordinary bridge
whose number is allocated above every other allocator's ceiling.

Tokens are stored encrypted (backup_crypto.encrypt_secret) and this module
never looks inside them: it takes and returns the stored string as it is.
"""
import time

from db import conn, cur

INBOX_BRIDGE_ID_FLOOR = 1000000

def claim_inbox_bridge_id():
    """Take the next bridge id in the reserved inbox range, or None if the
    row could not be claimed.

    Conversation bridges live at and above INBOX_BRIDGE_ID_FLOOR, the third
    and topmost region of the one id space: hand-numbered ordinary bridges
    stay below APPEAL_BRIDGE_ID_FLOOR, appeal bridges between the two floors.
    Simple max+1 — conversations close after 30 days of silence and the range
    is effectively unbounded, so holes need no reuse here.

    The number is claimed by a single INSERT … SELECT, for the reason
    db/bridges.py: attach_chat_to_new_bridge spells out: SQLite evaluates one
    statement under its write lock, so two people writing to the same receiver
    bot in the same instant cannot be handed one number and have their
    conversations merged into a single thread. Unlike the /atb allocator this
    one is reached without any human in the loop, which makes the race a
    matter of ordinary traffic rather than of two admins colliding."""
    claimed = cur.execute(
        "INSERT INTO bridges (id)"
        " SELECT COALESCE(MAX(id) + 1, :floor) FROM bridges WHERE id >= :floor",
        {"floor": INBOX_BRIDGE_ID_FLOOR}
    )
    conn.commit()
    if not claimed.rowcount:
        return None
    return int(claimed.lastrowid)

def is_inbox_bridge(bridge_id):
    """Whether a bridge number belongs to the inbox range — the cheap test
    every inbound handler uses to tell a conversation from an ordinary
    bridge, before touching a table."""
    try:
        return int(bridge_id) >= INBOX_BRIDGE_ID_FLOOR
    except (TypeError, ValueError):
        return False

def inbox_chat_id(bot_id, user_id):
    """The bot-wide chat key of a private chat with a receiver bot.

    Deliberately shaped like every other key ('prefix:suffix') so the generic
    helpers keep working, and paired with platform 'inbox' so nothing routes
    it to the main Telegram bot, which cannot reach that conversation."""
    return f"{bot_id}:{user_id}"

def add_inbox_bot(bot_id, username, title, token, owner_platform, owner_id):
    """Register a receiver bot, or re-register one whose token changed.

    `token` is expected to be the ENCRYPTED form. Re-registering keeps the
    original owner and added_at: the row's identity is the bot, and a Bot
    Admin refreshing a token on someone's behalf must not take the bot from
    them."""
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO inbox_bots
        (bot_id, username, title, token, anonymize, owner_platform, owner_id, added_at, updated_at)
        VALUES (?,?,?,?,0,?,?,?,?)
        ON CONFLICT(bot_id) DO UPDATE SET
            username=excluded.username,
            title=excluded.title,
            token=excluded.token,
            updated_at=excluded.updated_at
        """,
        (str(bot_id), username, title, token, str(owner_platform), str(owner_id), now, now)
    )
    conn.commit()

def get_inbox_bot(bot_id):
    """One registered receiver bot by its numeric id, or None."""
    return cur.execute(
        "SELECT * FROM inbox_bots WHERE bot_id=?",
        (str(bot_id),)
    ).fetchone()

def find_inbox_bot(identifier):
    """Resolve what an admin typed — a numeric id, '@name' or a bare
    username — to a registered receiver bot, or None. Usernames are compared
    case-insensitively, as Telegram treats them."""
    raw = str(identifier or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        row = get_inbox_bot(raw)
        if row:
            return row
    name = raw.lstrip("@").lower()
    return cur.execute(
        "SELECT * FROM inbox_bots WHERE LOWER(username)=?",
        (name,)
    ).fetchone()

def get_inbox_bots():
    """Every registered receiver bot — the list the runtime starts pollers
    from and /inboxlist prints."""
    return cur.execute("SELECT * FROM inbox_bots ORDER BY username").fetchall()

def set_inbox_anonymize(bot_id, enabled):
    """Turn staff anonymization on or off for one receiver bot."""
    cur.execute(
        "UPDATE inbox_bots SET anonymize=?, updated_at=? WHERE bot_id=?",
        (1 if enabled else 0, int(time.time()), str(bot_id))
    )
    conn.commit()

def remove_inbox_bot(bot_id):
    """Unregister a receiver bot with everything keyed on it: hosts,
    conversation records, staff indexes and bans.

    The conversation *bridges* are not touched here — their chats have to be
    detached one by one so the threads and topics can be closed first, which
    is the runtime's job (inbox.py: close_inbox_conversation). Returns True
    when a bot was removed."""
    removed = cur.execute("DELETE FROM inbox_bots WHERE bot_id=?", (str(bot_id),)).rowcount
    cur.execute("DELETE FROM inbox_hosts WHERE bot_id=?", (str(bot_id),))
    cur.execute("DELETE FROM inbox_conversations WHERE bot_id=?", (str(bot_id),))
    cur.execute("DELETE FROM inbox_topics WHERE bot_id=?", (str(bot_id),))
    cur.execute("DELETE FROM inbox_bans WHERE bot_id=?", (str(bot_id),))
    conn.commit()
    return removed > 0

def add_inbox_host(bot_id, platform, chat_id, added_by):
    """Make a chat one of the places a receiver bot opens conversations in."""
    cur.execute(
        """
        INSERT INTO inbox_hosts (bot_id, platform, chat_id, added_by, added_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(bot_id, chat_id) DO UPDATE SET
            platform=excluded.platform,
            added_by=excluded.added_by,
            added_at=excluded.added_at
        """,
        (str(bot_id), platform, str(chat_id), str(added_by), int(time.time()))
    )
    conn.commit()

def remove_inbox_host(bot_id, chat_id):
    """Stop opening conversations of this bot in that chat. Threads and
    topics already open are left alone — they are ordinary bridge chats and
    keep working until their conversation closes. Returns True when there was
    a host row."""
    removed = cur.execute(
        "DELETE FROM inbox_hosts WHERE bot_id=? AND chat_id=?",
        (str(bot_id), str(chat_id))
    ).rowcount
    cur.execute(
        "DELETE FROM inbox_topics WHERE bot_id=? AND host_chat_id=?",
        (str(bot_id), str(chat_id))
    )
    conn.commit()
    return removed > 0

def get_inbox_hosts(bot_id):
    """The chats a receiver bot opens conversations in."""
    return cur.execute(
        "SELECT * FROM inbox_hosts WHERE bot_id=? ORDER BY platform, chat_id",
        (str(bot_id),)
    ).fetchall()

def get_inbox_host(bot_id, chat_id):
    """One host row, or None — the 'is this chat already a host' check."""
    return cur.execute(
        "SELECT * FROM inbox_hosts WHERE bot_id=? AND chat_id=?",
        (str(bot_id), str(chat_id))
    ).fetchone()

def get_inbox_host_of_community(bot_id, platform, chat_id):
    """The host row a conversation chat belongs to, found through the
    server/group they share.

    A conversation's thread is keyed `guild:thread` and its host `guild:
    channel` — different chats with a common prefix, and nothing in the
    database links them directly. The prefix is enough for what this answers,
    because `/close-header` is a per-community setting: 'this team, this
    receiver bot'. A community hosting one bot in two channels gets one answer
    for both, which is the scope asked for rather than a limitation of it."""
    prefix = str(chat_id).split(":", 1)[0]
    return cur.execute(
        "SELECT * FROM inbox_hosts WHERE bot_id=? AND platform=?"
        " AND (chat_id LIKE ? OR chat_id=?) LIMIT 1",
        (str(bot_id), platform, f"{prefix}:%", prefix)
    ).fetchone()

def set_inbox_header_hidden(bot_id, chat_id, hidden):
    """Turn the relay header of a receiver bot's conversations on or off for
    one community. Returns True when a host row was touched."""
    changed = cur.execute(
        "UPDATE inbox_hosts SET hide_header=? WHERE bot_id=? AND chat_id=?",
        (1 if hidden else 0, str(bot_id), str(chat_id))
    ).rowcount
    conn.commit()
    return changed > 0

def inbox_header_hidden(bot_id, platform, chat_id):
    """Whether copies delivered into this conversation chat should go without
    the ``[Messenger | DM] Name:`` header."""
    row = get_inbox_host_of_community(bot_id, platform, chat_id)
    return bool(row and row["hide_header"])

def get_inbox_hosts_of_chat(chat_id):
    """Every receiver bot that opens its conversations in this chat.

    Usually one; the list exists so /reminboxchat and /inboxban can work
    without a bot argument when the chat leaves no doubt."""
    return cur.execute(
        "SELECT * FROM inbox_hosts WHERE chat_id=?",
        (str(chat_id),)
    ).fetchall()

def create_inbox_conversation(bot_id, user_id, bridge_id, lang, title):
    """Open a conversation record, replacing any older one of the same pair.
    A fresh conversation always starts on the writer's side — they are the
    one who opened it — so its mark is 'user'."""
    now = int(time.time())
    cur.execute(
        """
        INSERT OR REPLACE INTO inbox_conversations
        (bot_id, user_id, bridge_id, lang, created_at, last_message_ts, title, status)
        VALUES (?,?,?,?,?,?,?,'user')
        """,
        (str(bot_id), str(user_id), int(bridge_id), lang, now, now, title)
    )
    conn.commit()

def set_inbox_conversation_status(bot_id, user_id, status):
    """Record which side spoke last. Returns True when it changed — the
    caller renames the thread and topic only then, because Discord allows a
    thread just two renames per ten minutes and a lively conversation would
    otherwise spend that budget on every message."""
    row = get_inbox_conversation(bot_id, user_id)
    if row is None or row["status"] == status:
        return False
    cur.execute(
        "UPDATE inbox_conversations SET status=? WHERE bot_id=? AND user_id=?",
        (status, str(bot_id), str(user_id))
    )
    conn.commit()
    return True

def get_inbox_conversation(bot_id, user_id):
    """The open conversation of a user with one receiver bot, or None."""
    return cur.execute(
        "SELECT * FROM inbox_conversations WHERE bot_id=? AND user_id=?",
        (str(bot_id), str(user_id))
    ).fetchone()

def get_inbox_conversation_by_bridge(bridge_id):
    """The conversation a bridge belongs to, or None — how the inbound
    handlers tell a conversation thread from an ordinary bridged chat."""
    return cur.execute(
        "SELECT * FROM inbox_conversations WHERE bridge_id=?",
        (int(bridge_id),)
    ).fetchone()

def get_inbox_conversations_of_bot(bot_id):
    """Every open conversation of one receiver bot — walked when the bot is
    unregistered or one of its users is banned."""
    return cur.execute(
        "SELECT * FROM inbox_conversations WHERE bot_id=?",
        (str(bot_id),)
    ).fetchall()

def touch_inbox_conversation(bot_id, user_id):
    """Record activity, restarting the 30-day silence window."""
    cur.execute(
        "UPDATE inbox_conversations SET last_message_ts=? WHERE bot_id=? AND user_id=?",
        (int(time.time()), str(bot_id), str(user_id))
    )
    conn.commit()

def delete_inbox_conversation(bot_id, user_id):
    """Drop a conversation record and its staff indexes; returns the deleted
    row so the caller can also detach the bridge chats and close the thread."""
    row = get_inbox_conversation(bot_id, user_id)
    if row:
        cur.execute("DELETE FROM inbox_staff WHERE bridge_id=?", (int(row["bridge_id"]),))
    cur.execute(
        "DELETE FROM inbox_conversations WHERE bot_id=? AND user_id=?",
        (str(bot_id), str(user_id))
    )
    conn.commit()
    return row

def get_silent_inbox_conversations(max_silence_seconds):
    """Conversations nobody has written in for longer than the window — what
    the daily sweep closes."""
    cutoff = int(time.time()) - int(max_silence_seconds)
    return cur.execute(
        "SELECT * FROM inbox_conversations WHERE COALESCE(last_message_ts, created_at) < ?",
        (cutoff,)
    ).fetchall()

def remember_inbox_topic(bot_id, user_id, host_chat_id, topic_chat_id):
    """Record which Telegram topic of a host group belongs to one writer, so
    a later message can reopen it instead of opening a second one."""
    cur.execute(
        """
        INSERT OR REPLACE INTO inbox_topics
        (bot_id, user_id, host_chat_id, topic_chat_id, created_at)
        VALUES (?,?,?,?,?)
        """,
        (str(bot_id), str(user_id), str(host_chat_id), str(topic_chat_id), int(time.time()))
    )
    conn.commit()

def get_inbox_topic(bot_id, user_id, host_chat_id):
    """The topic this writer already owns in that host group, or None."""
    row = cur.execute(
        "SELECT topic_chat_id FROM inbox_topics"
        " WHERE bot_id=? AND user_id=? AND host_chat_id=?",
        (str(bot_id), str(user_id), str(host_chat_id))
    ).fetchone()
    return row["topic_chat_id"] if row else None

def forget_inbox_topic(bot_id, user_id, host_chat_id):
    """Drop the remembered topic — called when reopening it turns out to be
    impossible (someone deleted it) and a new one has to be made."""
    cur.execute(
        "DELETE FROM inbox_topics WHERE bot_id=? AND user_id=? AND host_chat_id=?",
        (str(bot_id), str(user_id), str(host_chat_id))
    )
    conn.commit()

def get_inbox_staff_ord(bridge_id, platform, user_id):
    """Stable per-conversation anonymization index of a staff member
    (0 → 'Staff A', …).

    The first time they write in the conversation they take the next free
    index; afterwards the same one always comes back. Reserved even while
    anonymization is off, so turning it on mid-conversation does not
    reshuffle who is who."""
    row = cur.execute(
        "SELECT ord FROM inbox_staff WHERE bridge_id=? AND platform=? AND user_id=?",
        (int(bridge_id), platform, str(user_id))
    ).fetchone()
    if row:
        return row["ord"]
    nxt = cur.execute(
        "SELECT COALESCE(MAX(ord) + 1, 0) AS nxt FROM inbox_staff WHERE bridge_id=?",
        (int(bridge_id),)
    ).fetchone()["nxt"]
    cur.execute(
        "INSERT INTO inbox_staff (bridge_id, platform, user_id, ord) VALUES (?,?,?,?)",
        (int(bridge_id), platform, str(user_id), nxt)
    )
    conn.commit()
    return nxt

def inbox_file_relay_enabled(bridge_id):
    """Whether files may be re-uploaded to GALLERY inside a conversation.

    The plain `bridge_file_relay_enabled` cannot answer this: it demands that
    *every* chat of the bridge be covered by an `/allow-files` consent, and
    the private chat at the heart of a conversation belongs to no community
    and so can never be covered. Nothing is lost by leaving it out — the
    consent exists to protect the communities whose files would end up on a
    public CDN, and here the only community involved is the one hosting the
    conversation. So the question asked is the same one, over the host chats
    alone: the bridge-wide consent, or every host's own server/group consent.

    A conversation with no host chat left answers no, rather than answering
    'every chat is covered' about an empty list."""
    from db.bridges import chat_server_id, get_bridge_chats
    from db.settings import get_bridge_file_consent, get_server_file_consent

    if get_bridge_file_consent(bridge_id):
        return True

    hosts = [c for c in get_bridge_chats(bridge_id) if c["platform"] != "inbox"]
    if not hosts:
        return False

    for chat in hosts:
        server_id = chat_server_id(chat["platform"], chat["chat_id"])
        if not server_id or not get_server_file_consent(chat["platform"], server_id):
            return False
    return True

def add_inbox_ban(bot_id, user_id, banned_by):
    """Bar a user from writing to one receiver bot."""
    cur.execute(
        """
        INSERT OR REPLACE INTO inbox_bans (bot_id, user_id, banned_by, banned_at)
        VALUES (?,?,?,?)
        """,
        (str(bot_id), str(user_id), str(banned_by), int(time.time()))
    )
    conn.commit()

def remove_inbox_ban(bot_id, user_id):
    """Lift a ban. Returns True when there was one."""
    removed = cur.execute(
        "DELETE FROM inbox_bans WHERE bot_id=? AND user_id=?",
        (str(bot_id), str(user_id))
    ).rowcount
    conn.commit()
    return removed > 0

def is_inbox_banned(bot_id, user_id):
    """Whether this user is barred from this receiver bot — checked on every
    message it receives."""
    return cur.execute(
        "SELECT 1 FROM inbox_bans WHERE bot_id=? AND user_id=?",
        (str(bot_id), str(user_id))
    ).fetchone() is not None
