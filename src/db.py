import sqlite3
import threading
import time

_db_lock = threading.RLock()
_raw_conn = sqlite3.connect("bridge.db", check_same_thread=False)
_raw_conn.execute("PRAGMA journal_mode=WAL;")
_raw_conn.execute("PRAGMA synchronous=NORMAL;")
_raw_conn.row_factory = sqlite3.Row

class _LockingConnection:
    """Thread-safe facade over sqlite3.Connection.

    `execute()` returns a brand new cursor on every call, so chained
    `.fetchone()/.fetchall()/.lastrowid` always operate on a private cursor.
    All access is guarded by a re-entrant lock to make concurrent use from the
    Telegram bot, the Discord bot and the background loops safe.
    """

    def __init__(self, raw_conn, lock):
        self._conn = raw_conn
        self._lock = lock

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)

conn = _LockingConnection(_raw_conn, _db_lock)
cur = conn

def init():
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS bridges (
        id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT,
        chat_id TEXT UNIQUE,
        bridge_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id INTEGER,
        origin_platform TEXT,
        origin_chat_id TEXT,
        origin_message_id TEXT,
        origin_sender_id TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS message_copies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        platform TEXT,
        chat_id TEXT,
        message_id_platform TEXT
    );

    CREATE TABLE IF NOT EXISTS chat_admins (
        platform TEXT,
        chat_id TEXT,
        user_id TEXT,
        PRIMARY KEY (platform, chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS server_admins (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (platform, server_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS localizers (
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS dead_chats (
        chat_id TEXT PRIMARY KEY,
        role_id TEXT,
        hours INTEGER,
        last_message_ts INTEGER
    );

    CREATE TABLE IF NOT EXISTS news_chats (
        chat_id TEXT PRIMARY KEY,
        emojis TEXT
    );

    CREATE TABLE IF NOT EXISTS bridge_rules (
        bridge_id INTEGER PRIMARY KEY,
        content TEXT,
        format TEXT,
        origin_platform TEXT,
        origin_chat_id TEXT,
        origin_message_id TEXT,
        hours INTEGER,
        messages INTEGER,
        last_post_ts INTEGER,
        message_counter INTEGER
    );

    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id TEXT PRIMARY KEY,
        lang TEXT
    );

    CREATE TABLE IF NOT EXISTS verified_users (
        platform TEXT,
        user_id TEXT,
        prefix TEXT,
        verified_at INTEGER,
        expires_at INTEGER,
        PRIMARY KEY (platform, user_id, prefix)
    );

    CREATE TABLE IF NOT EXISTS pending_consents (
        platform TEXT,
        prefix TEXT,
        user_id TEXT,
        bot_message_id TEXT,
        chat_key TEXT,
        first_message_id TEXT,
        first_message_payload TEXT,
        created_at INTEGER,
        PRIMARY KEY (platform, prefix, user_id)
    );

    CREATE TABLE IF NOT EXISTS bridge_admins (
        bridge_id INTEGER,
        user_id TEXT,
        PRIMARY KEY (bridge_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS server_bridge_admins (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (platform, server_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS shadow_bans (
        platform TEXT,
        user_id TEXT,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS inaccessible_chats (
        platform TEXT,
        chat_id TEXT PRIMARY KEY,
        first_failed_ts INTEGER,
        last_failed_ts INTEGER
    );

    CREATE TABLE IF NOT EXISTS deadtopic_chats (
        chat_id TEXT PRIMARY KEY,
        last_message_ts INTEGER,
        bot_last_sent_ts INTEGER
    );

    CREATE TABLE IF NOT EXISTS media_group_members (
        chat_id TEXT,
        message_id_platform TEXT,
        message_id INTEGER,
        PRIMARY KEY (chat_id, message_id_platform)
    );

    CREATE TABLE IF NOT EXISTS loc_suggestions (
        code TEXT PRIMARY KEY,
        platform TEXT,
        user_id TEXT,
        username TEXT,
        lang TEXT,
        rkey TEXT,
        suggestion TEXT,
        ui_lang TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bridge_id INTEGER,
        question TEXT,
        options TEXT,
        created_at INTEGER,
        ends_at INTEGER,
        closed INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS poll_messages (
        poll_id INTEGER,
        platform TEXT,
        chat_id TEXT,
        message_id TEXT,
        PRIMARY KEY (poll_id, platform, chat_id)
    );

    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_id INTEGER,
        platform TEXT,
        user_id TEXT,
        option_index INTEGER,
        PRIMARY KEY (poll_id, platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS appeals (
        user_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        bridge_id INTEGER NOT NULL,
        lang TEXT,
        created_at INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        verdict_at INTEGER,
        verdict_by TEXT
    );

    CREATE TABLE IF NOT EXISTS appeal_consuls (
        thread_id TEXT NOT NULL,
        consul_user_id TEXT NOT NULL,
        ord INTEGER NOT NULL,
        PRIMARY KEY (thread_id, consul_user_id)
    );

    CREATE TABLE IF NOT EXISTS consul_names (
        user_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        normalized TEXT NOT NULL,
        set_by TEXT,
        set_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS server_file_consents (
        platform TEXT NOT NULL,
        server_id TEXT NOT NULL,
        enabled_by TEXT,
        enabled_at INTEGER,
        PRIMARY KEY (platform, server_id)
    );

    CREATE TABLE IF NOT EXISTS bridge_file_consents (
        bridge_id INTEGER PRIMARY KEY,
        enabled_by TEXT,
        enabled_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS gallery_uploads (
        message_id INTEGER PRIMARY KEY,
        channel_id TEXT,
        gallery_message_id TEXT,
        urls TEXT,
        source_message_ids TEXT,
        file_ids TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS user_privacy (
        platform TEXT NOT NULL,
        user_id TEXT NOT NULL,
        hide_whois INTEGER NOT NULL DEFAULT 0,
        hide_avatar INTEGER NOT NULL DEFAULT 0,
        block_mention INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER,
        PRIMARY KEY (platform, user_id)
    );

    CREATE TABLE IF NOT EXISTS feeds (
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        source_id TEXT,
        title TEXT,
        last_post_id TEXT,
        live INTEGER NOT NULL DEFAULT 0,
        added_by TEXT,
        added_at INTEGER,
        PRIMARY KEY (kind, source, chat_id)
    );

    CREATE TABLE IF NOT EXISTS server_webhooks (
        server_id TEXT PRIMARY KEY,
        enabled_by TEXT,
        enabled_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS bridge_webhooks (
        server_id TEXT NOT NULL,
        bridge_id INTEGER NOT NULL,
        enabled_by TEXT,
        enabled_at INTEGER,
        PRIMARY KEY (server_id, bridge_id)
    );
    """)
    conn.commit()

    cols = [r["name"] for r in cur.execute("PRAGMA table_info(messages)").fetchall()]
    if "origin_sender_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN origin_sender_id TEXT")
        conn.commit()
    if "origin_sender_name" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN origin_sender_name TEXT")
        conn.commit()
    if "reply_to_message_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN reply_to_message_id INTEGER")
        conn.commit()
    if "forward_type" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN forward_type TEXT")
        conn.commit()
    if "forward_name" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN forward_name TEXT")
        conn.commit()

    pending_cols = [r["name"] for r in cur.execute("PRAGMA table_info(pending_consents)").fetchall()]
    if "first_message_id" not in pending_cols:
        cur.execute("ALTER TABLE pending_consents ADD COLUMN first_message_id TEXT")
        conn.commit()
    if "first_message_payload" not in pending_cols:
        cur.execute("ALTER TABLE pending_consents ADD COLUMN first_message_payload TEXT")
        conn.commit()

    copy_cols = [r["name"] for r in cur.execute("PRAGMA table_info(message_copies)").fetchall()]
    if "kind" not in copy_cols:
        cur.execute("ALTER TABLE message_copies ADD COLUMN kind TEXT DEFAULT 'main'")
        conn.commit()

    cs_cols = [r["name"] for r in cur.execute("PRAGMA table_info(chat_settings)").fetchall()]
    if "allow_bots" not in cs_cols:
        cur.execute("ALTER TABLE chat_settings ADD COLUMN allow_bots INTEGER DEFAULT 0")
        conn.commit()
    if "webhooks" not in cs_cols:
        cur.execute("ALTER TABLE chat_settings ADD COLUMN webhooks INTEGER DEFAULT 0")
        conn.commit()

def set_verify_list_enabled(enabled):
    cur.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('verify_list_enabled', ?)",
        ("1" if enabled else "0",)
    )
    conn.commit()

def is_verify_list_enabled():
    """Whether (un)verified user IDs are published to the VERIFIED/UNVERIFIED
    channels for guard_bot to mirror. Enabled by default."""
    row = cur.execute(
        "SELECT value FROM bot_settings WHERE key='verify_list_enabled'"
    ).fetchone()
    return row is None or row["value"] == "1"

def chat_exists(chat_id):
    return cur.execute(
        "SELECT 1 FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone() is not None

def attach_chat(platform, chat_id, bridge_id):
    cur.execute(
        "INSERT OR IGNORE INTO bridges(id) VALUES(?)",
        (bridge_id,)
    )
    cur.execute(
        "INSERT OR REPLACE INTO chats(platform, chat_id, bridge_id) VALUES(?,?,?)",
        (platform, chat_id, bridge_id)
    )
    conn.commit()

def get_bridge_chats(bridge_id):
    return cur.execute(
        "SELECT * FROM chats WHERE bridge_id=?",
        (bridge_id,)
    ).fetchall()

def cleanup_old_messages(days=30):
    import time
    limit = int(time.time()) - days * 86400

    cur.execute(
        "DELETE FROM message_copies WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?)",
        (limit,)
    )
    cur.execute(
        "DELETE FROM media_group_members WHERE message_id IN "
        "(SELECT id FROM messages WHERE created_at IS NOT NULL AND created_at < ?)",
        (limit,)
    )
    cur.execute(
        "DELETE FROM messages WHERE created_at IS NOT NULL AND created_at < ?",
        (limit,)
    )
    conn.commit()

def record_media_group_members(chat_id, platform_message_ids, message_db_id):
    """Map every Telegram message_id of an album to the single relayed message.

    A Telegram media group is delivered as several separate messages but relayed
    as one. Recording each constituent message_id lets a reply to *any* file in
    the album resolve to that one relayed message instead of being treated as a
    reply to an unknown message."""
    for pid in platform_message_ids:
        cur.execute(
            "INSERT OR REPLACE INTO media_group_members (chat_id, message_id_platform, message_id) VALUES (?,?,?)",
            (chat_id, str(pid), message_db_id)
        )
    conn.commit()

def find_message_db_id_by_media_member(chat_id, platform_message_id):
    row = cur.execute(
        "SELECT message_id FROM media_group_members WHERE chat_id=? AND message_id_platform=?",
        (chat_id, str(platform_message_id))
    ).fetchone()
    return row["message_id"] if row else None

def set_chat_lang(chat_id, lang_code):
    cur.execute(
        "INSERT INTO chat_settings (chat_id, lang) VALUES (?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang",
        (chat_id, lang_code)
    )
    conn.commit()

def get_chat_lang(chat_id):
    """
    Lookup language for given chat_id. Resolution order:
      1. exact chat_id (channel/thread/topic — set with /locallang),
      2. bare community prefix (guild id / group chat id — set with /lang),
      3. legacy '<group_id>:0' key (old group-wide behavior).
    If not found → returns None.
    """
    row = cur.execute(
        "SELECT lang FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if row and row["lang"]:
        return row["lang"]

    if ":" in chat_id:
        prefix = chat_id.split(":", 1)[0]
        for fallback_key in (prefix, f"{prefix}:0"):
            row = cur.execute(
                "SELECT lang FROM chat_settings WHERE chat_id=?",
                (fallback_key,)
            ).fetchone()
            if row and row["lang"]:
                return row["lang"]

    return None

def remove_chat_settings_for_prefix(prefix):
    """
    Remove chat_settings rows where chat_id LIKE '<prefix>:%', plus the bare
    '<prefix>' row holding the community-wide /lang setting.
    prefix example: guild_id for discord, chat.id for telegram
    """
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

def get_telegram_chat_count():
    """Возвращает количество уникальных чатов Telegram, подключенных к боту."""
    row = cur.execute(
        "SELECT COUNT(*) as cnt FROM chats WHERE platform='telegram'"
    ).fetchone()
    return row['cnt'] if row else 0

def get_telegram_group_count():
    """Количество уникальных Telegram-групп (без учета топиков)."""
    row = cur.execute(
        """
        SELECT COUNT(DISTINCT SUBSTR(chat_id, 1, INSTR(chat_id, ':') - 1)) AS cnt
        FROM chats
        WHERE platform='telegram'
        """
    ).fetchone()
    return row['cnt'] if row else 0

def get_telegram_group_ids():
    rows = cur.execute(
        """
        SELECT DISTINCT SUBSTR(chat_id, 1, INSTR(chat_id, ':') - 1) AS group_id
        FROM chats
        WHERE platform='telegram'
        """
    ).fetchall()
    return [r['group_id'] for r in rows if r['group_id']]

def add_verified_user(platform, user_id, prefix, days_valid=365):
    now = int(time.time())
    expires = now + days_valid * 86400
    cur.execute(
        "INSERT OR REPLACE INTO verified_users (platform, user_id, prefix, verified_at, expires_at) VALUES (?,?,?,?,?)",
        (platform, str(user_id), str(prefix), now, expires)
    )
    conn.commit()

def is_user_verified(platform, user_id, prefix=None):
    """
    Возвращает True, если у пользователя есть непросроченная запись верификации
    на данной платформе. Согласие на пересылку одно на всю платформу, поэтому
    prefix (чат/сервер, где оно было дано) на проверку не влияет — параметр
    оставлен только для совместимости сигнатуры.
    """
    now = int(time.time())
    row = cur.execute(
        """
        SELECT expires_at FROM verified_users
        WHERE platform=? AND user_id=?
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (platform, str(user_id))
    ).fetchone()
    if not row:
        return False
    try:
        return int(row["expires_at"]) >= now
    except Exception:
        return False

def remove_verified_user(platform, user_id, prefix):
    cur.execute(
        "DELETE FROM verified_users WHERE platform=? AND user_id=? AND prefix=?",
        (platform, str(user_id), str(prefix))
    )
    conn.commit()

def add_pending_consent(
    platform,
    prefix,
    user_id,
    bot_message_id,
    chat_key,
    first_message_id=None,
    first_message_payload=None
):
    now = int(time.time())
    cur.execute(
        "INSERT OR REPLACE INTO pending_consents (platform, prefix, user_id, bot_message_id, chat_key, first_message_id, first_message_payload, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            platform,
            str(prefix),
            str(user_id),
            str(bot_message_id),
            str(chat_key),
            str(first_message_id) if first_message_id is not None else None,
            str(first_message_payload) if first_message_payload is not None else None,
            now
        )
    )
    conn.commit()

def get_pending_consent(platform, prefix, user_id):
    return cur.execute(
        "SELECT * FROM pending_consents WHERE platform=? AND prefix=? AND user_id=?",
        (platform, str(prefix), str(user_id))
    ).fetchone()

def remove_pending_consent(platform, prefix, user_id):
    cur.execute(
        "DELETE FROM pending_consents WHERE platform=? AND prefix=? AND user_id=?",
        (platform, str(prefix), str(user_id))
    )
    conn.commit()

def get_all_pending_consents_for_user(platform, user_id):
    return cur.execute(
        "SELECT * FROM pending_consents WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchall()

def get_expired_pending_consents(older_than_seconds=24*3600):
    cutoff = int(time.time()) - older_than_seconds
    return cur.execute(
        "SELECT * FROM pending_consents WHERE created_at<?",
        (cutoff,)
    ).fetchall()

def cleanup_expired_verified():
    now = int(time.time())
    cur.execute(
        "DELETE FROM verified_users WHERE expires_at<?",
        (now,)
    )
    conn.commit()

def delete_pending(platform, prefix, user_id):
    remove_pending_consent(platform, prefix, user_id)

def add_bridge_admin(bridge_id, user_id):
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

def server_bridge_ids(platform, server_id):
    """The bridges a server/group takes part in."""
    return [r["bridge_id"] for r in cur.execute(
        "SELECT DISTINCT bridge_id FROM chats"
        " WHERE platform=? AND (chat_id LIKE ? OR chat_id=?)",
        (platform, f"{server_id}:%", str(server_id))
    ).fetchall() if r["bridge_id"] is not None]

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
    cur.execute(
        "DELETE FROM server_admins WHERE platform=? AND server_id=? AND user_id=?",
        (platform, str(server_id), str(user_id))
    )
    conn.commit()

def is_server_admin(platform, server_id, user_id):
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
    return cur.execute(
        "SELECT 1 FROM localizers WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone() is not None

def remove_bridge_admin(bridge_id, user_id):
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

_old_attach_chat = attach_chat
def attach_chat(platform, chat_id, bridge_id):
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

def add_shadow_ban(platform, user_id):
    cur.execute(
        "INSERT OR IGNORE INTO shadow_bans (platform, user_id) VALUES (?,?)",
        (platform, str(user_id))
    )
    conn.commit()

def remove_shadow_ban(platform, user_id):
    cur.execute(
        "DELETE FROM shadow_bans WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    )
    conn.commit()

def is_shadow_banned(platform, user_id):
    row = cur.execute(
        "SELECT 1 FROM shadow_bans WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone()
    return row is not None

def attach_chat_to_bridge(platform, chat_id, bridge_id):
    if chat_exists(chat_id):
        raise ValueError("chat_already_attached")

    attach_chat(platform, chat_id, bridge_id)

def get_targets(bridge_id, exclude_chat_id):
    chats = get_bridge_chats(bridge_id)
    return [c for c in chats if c["chat_id"] != exclude_chat_id]

def mark_chat_inaccessible(platform, chat_id):
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
    cur.execute("DELETE FROM inaccessible_chats WHERE chat_id=?", (chat_id,))
    conn.commit()

def get_allow_bots(chat_id):
    row = cur.execute(
        "SELECT allow_bots FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    return bool(row and row["allow_bots"])

def set_allow_bots(chat_id, enabled: bool):
    cur.execute(
        "INSERT INTO chat_settings (chat_id, allow_bots) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET allow_bots=excluded.allow_bots",
        (chat_id, 1 if enabled else 0)
    )
    conn.commit()

def get_webhooks_enabled(chat_id):
    """Whether relayed copies in this Discord chat arrive as webhook messages.

    Three scopes answer yes, widest first: the whole server (`/webhooks enable`),
    the server's chats in one bridge (`/webhooks enable local`), and the single
    channel rows written by older versions of the command. Never cached — a chat
    may have joined its bridge a minute ago."""
    row = cur.execute(
        "SELECT webhooks FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if row and row["webhooks"]:
        return True

    server_id = chat_server_id("discord", chat_id)
    if not server_id:
        return False
    if get_server_webhooks(server_id):
        return True

    bridge = cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    if bridge and bridge["bridge_id"] is not None:
        return get_bridge_webhooks(server_id, bridge["bridge_id"])
    return False

def set_webhooks_enabled(chat_id, enabled: bool):
    cur.execute(
        "INSERT INTO chat_settings (chat_id, webhooks) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET webhooks=excluded.webhooks",
        (chat_id, 1 if enabled else 0)
    )
    conn.commit()

def set_server_file_consent(platform, server_id, enabled: bool, enabled_by=None):
    """Server/group-wide consent to the GALLERY file re-upload (`/allow-files`).
    Covers every chat of that Discord server or Telegram group, including ones
    attached to a bridge later."""
    if enabled:
        cur.execute(
            "INSERT INTO server_file_consents (platform, server_id, enabled_by, enabled_at)"
            " VALUES (?,?,?,strftime('%s','now'))"
            " ON CONFLICT(platform, server_id) DO UPDATE SET"
            " enabled_by=excluded.enabled_by, enabled_at=excluded.enabled_at",
            (platform, str(server_id), str(enabled_by) if enabled_by is not None else None)
        )
    else:
        cur.execute(
            "DELETE FROM server_file_consents WHERE platform=? AND server_id=?",
            (platform, str(server_id))
        )
    conn.commit()

def get_server_file_consent(platform, server_id) -> bool:
    return cur.execute(
        "SELECT 1 FROM server_file_consents WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    ).fetchone() is not None

def set_bridge_file_consent(bridge_id, enabled: bool, enabled_by=None):
    """Bridge-wide consent to the GALLERY file re-upload (`/allow-files local`).
    Covers every chat of the bridge, including ones attached later."""
    if enabled:
        cur.execute(
            "INSERT INTO bridge_file_consents (bridge_id, enabled_by, enabled_at)"
            " VALUES (?,?,strftime('%s','now'))"
            " ON CONFLICT(bridge_id) DO UPDATE SET"
            " enabled_by=excluded.enabled_by, enabled_at=excluded.enabled_at",
            (int(bridge_id), str(enabled_by) if enabled_by is not None else None)
        )
    else:
        cur.execute("DELETE FROM bridge_file_consents WHERE bridge_id=?", (int(bridge_id),))
    conn.commit()

def get_bridge_file_consent(bridge_id) -> bool:
    return cur.execute(
        "SELECT 1 FROM bridge_file_consents WHERE bridge_id=?",
        (int(bridge_id),)
    ).fetchone() is not None

def chat_server_id(platform, chat_id):
    """The server/group a chat belongs to: guild id for Discord, group id for
    Telegram. ``None`` for DM chats (appeal bridges), which belong to no server
    and can therefore never carry a server-wide consent."""
    chat_id = str(chat_id)
    if chat_id.startswith("dm:"):
        return None
    return chat_id.split(":", 1)[0] or None

def bridge_file_relay_enabled(bridge_id) -> bool:
    """Whether Telegram files may be re-uploaded to GALLERY for this bridge.

    Every chat of the bridge must be covered — by the bridge-wide consent or by
    its own server/group consent — because the mechanic both takes files out of
    one chat and posts public CDN links into all the others. Never cached: a
    chat may have joined the bridge a minute ago."""
    if get_bridge_file_consent(bridge_id):
        return True

    chats = get_bridge_chats(bridge_id)
    if not chats:
        return False

    for c in chats:
        server_id = chat_server_id(c["platform"], c["chat_id"])
        if not server_id or not get_server_file_consent(c["platform"], server_id):
            return False
    return True

def add_gallery_upload(message_id, channel_id, gallery_message_id, urls_json,
                       source_message_ids_json, file_ids_json=None):
    """Record the GALLERY message holding a relayed message's re-uploaded files,
    together with the CDN links handed out in its copies. The Telegram file ids
    are kept so that an edit can tell a changed attachment from a changed
    caption and leave working links alone."""
    cur.execute(
        "INSERT OR REPLACE INTO gallery_uploads"
        " (message_id, channel_id, gallery_message_id, urls, source_message_ids, file_ids, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (int(message_id), str(channel_id), str(gallery_message_id),
         urls_json, source_message_ids_json, file_ids_json, int(time.time()))
    )
    conn.commit()

def get_gallery_upload(message_id):
    return cur.execute(
        "SELECT * FROM gallery_uploads WHERE message_id=?",
        (int(message_id),)
    ).fetchone()

def delete_gallery_upload(message_id):
    cur.execute("DELETE FROM gallery_uploads WHERE message_id=?", (int(message_id),))
    conn.commit()

PRIVACY_FLAGS = ("hide_whois", "hide_avatar", "block_mention")

def get_user_privacy(platform, user_id):
    """The user's `/privacy` switches as a plain dict. Users who never touched
    the command have no row — everything is off, which is the behaviour the bot
    had before `/privacy` existed."""
    row = cur.execute(
        "SELECT * FROM user_privacy WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone()
    if not row:
        return {flag: False for flag in PRIVACY_FLAGS}
    return {flag: bool(row[flag]) for flag in PRIVACY_FLAGS}

def get_privacy_flag(platform, user_id, flag) -> bool:
    if flag not in PRIVACY_FLAGS:
        raise ValueError(f"unknown privacy flag: {flag}")
    row = cur.execute(
        f"SELECT {flag} FROM user_privacy WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone()
    return bool(row and row[flag])

def set_privacy_flag(platform, user_id, flag, enabled: bool):
    if flag not in PRIVACY_FLAGS:
        raise ValueError(f"unknown privacy flag: {flag}")
    cur.execute(
        f"INSERT INTO user_privacy (platform, user_id, {flag}, updated_at)"
        f" VALUES (?,?,?,strftime('%s','now'))"
        f" ON CONFLICT(platform, user_id) DO UPDATE SET"
        f" {flag}=excluded.{flag}, updated_at=excluded.updated_at",
        (platform, str(user_id), 1 if enabled else 0)
    )
    conn.commit()

def toggle_privacy_flag(platform, user_id, flag) -> bool:
    """Flip one switch and return its new value."""
    enabled = not get_privacy_flag(platform, user_id, flag)
    set_privacy_flag(platform, user_id, flag, enabled)
    return enabled

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
    if not cur.execute(
        "SELECT 1 FROM feeds WHERE kind=? AND source=? AND chat_id=?",
        (kind, source.lower(), str(chat_id))
    ).fetchone():
        return False
    cur.execute(
        "DELETE FROM feeds WHERE kind=? AND source=? AND chat_id=?",
        (kind, source.lower(), str(chat_id))
    )
    conn.commit()
    return True

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

def get_all_feeds(kind=None):
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

def set_server_webhooks(server_id, enabled: bool, enabled_by=None):
    """Server-wide `/webhooks`: every chat of that Discord server, in any bridge,
    including ones attached later."""
    if enabled:
        cur.execute(
            "INSERT INTO server_webhooks (server_id, enabled_by, enabled_at)"
            " VALUES (?,?,strftime('%s','now'))"
            " ON CONFLICT(server_id) DO UPDATE SET"
            " enabled_by=excluded.enabled_by, enabled_at=excluded.enabled_at",
            (str(server_id), str(enabled_by) if enabled_by is not None else None)
        )
    else:
        cur.execute("DELETE FROM server_webhooks WHERE server_id=?", (str(server_id),))
    conn.commit()

def get_server_webhooks(server_id) -> bool:
    return cur.execute(
        "SELECT 1 FROM server_webhooks WHERE server_id=?", (str(server_id),)
    ).fetchone() is not None

def set_bridge_webhooks(server_id, bridge_id, enabled: bool, enabled_by=None):
    """`/webhooks enable local`: the server's chats in one bridge only."""
    if enabled:
        cur.execute(
            "INSERT INTO bridge_webhooks (server_id, bridge_id, enabled_by, enabled_at)"
            " VALUES (?,?,?,strftime('%s','now'))"
            " ON CONFLICT(server_id, bridge_id) DO UPDATE SET"
            " enabled_by=excluded.enabled_by, enabled_at=excluded.enabled_at",
            (str(server_id), int(bridge_id),
             str(enabled_by) if enabled_by is not None else None)
        )
    else:
        cur.execute(
            "DELETE FROM bridge_webhooks WHERE server_id=? AND bridge_id=?",
            (str(server_id), int(bridge_id))
        )
    conn.commit()

def get_bridge_webhooks(server_id, bridge_id) -> bool:
    return cur.execute(
        "SELECT 1 FROM bridge_webhooks WHERE server_id=? AND bridge_id=?",
        (str(server_id), int(bridge_id))
    ).fetchone() is not None

def is_relay_copy(platform: str, chat_id: str, message_id_platform: str) -> bool:
    """Return True if the given message was sent by the bridge bot as a relay copy."""
    row = cur.execute(
        "SELECT 1 FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=?",
        (platform, chat_id, message_id_platform)
    ).fetchone()
    return row is not None

def add_loc_suggestion(code, platform, user_id, username, lang, rkey, suggestion, ui_lang):
    cur.execute(
        "INSERT OR REPLACE INTO loc_suggestions "
        "(code, platform, user_id, username, lang, rkey, suggestion, ui_lang, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (code, platform, str(user_id), username, lang, rkey, suggestion, ui_lang, int(time.time()))
    )
    conn.commit()

def get_loc_suggestion(code):
    return cur.execute(
        "SELECT * FROM loc_suggestions WHERE code=?",
        (code,)
    ).fetchone()

def delete_loc_suggestion(code):
    cur.execute("DELETE FROM loc_suggestions WHERE code=?", (code,))
    conn.commit()

def cleanup_old_loc_suggestions(max_age_seconds=365 * 24 * 3600):
    """Localization-suggestion dialog codes are kept at most a year."""
    cutoff = int(time.time()) - max_age_seconds
    cur.execute(
        "DELETE FROM loc_suggestions WHERE created_at IS NOT NULL AND created_at < ?",
        (cutoff,)
    )
    conn.commit()

def create_poll(bridge_id, question, options_json, ends_at):
    c = cur.execute(
        "INSERT INTO polls (bridge_id, question, options, created_at, ends_at, closed) VALUES (?,?,?,?,?,0)",
        (bridge_id, question, options_json, int(time.time()), ends_at)
    )
    conn.commit()
    return c.lastrowid

def get_poll(poll_id):
    return cur.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()

def add_poll_message(poll_id, platform, chat_id, message_id):
    cur.execute(
        "INSERT OR REPLACE INTO poll_messages (poll_id, platform, chat_id, message_id) VALUES (?,?,?,?)",
        (poll_id, platform, chat_id, str(message_id))
    )
    conn.commit()

def get_poll_messages(poll_id):
    return cur.execute("SELECT * FROM poll_messages WHERE poll_id=?", (poll_id,)).fetchall()

def get_poll_by_message(platform, chat_id, message_id):
    row = cur.execute(
        "SELECT poll_id FROM poll_messages WHERE platform=? AND chat_id=? AND message_id=?",
        (platform, chat_id, str(message_id))
    ).fetchone()
    return row["poll_id"] if row else None

def record_poll_vote(poll_id, platform, user_id, option_index):
    cur.execute(
        "INSERT OR REPLACE INTO poll_votes (poll_id, platform, user_id, option_index) VALUES (?,?,?,?)",
        (poll_id, platform, str(user_id), int(option_index))
    )
    conn.commit()

def get_poll_results(poll_id, num_options):
    rows = cur.execute(
        "SELECT option_index, COUNT(*) AS cnt FROM poll_votes WHERE poll_id=? GROUP BY option_index",
        (poll_id,)
    ).fetchall()
    counts = [0] * num_options
    for r in rows:
        idx = r["option_index"]
        if idx is not None and 0 <= idx < num_options:
            counts[idx] = r["cnt"]
    return counts

def close_poll(poll_id):
    cur.execute("UPDATE polls SET closed=1 WHERE id=?", (poll_id,))
    conn.commit()

def get_expired_open_polls():
    now = int(time.time())
    return cur.execute(
        "SELECT * FROM polls WHERE closed=0 AND ends_at IS NOT NULL AND ends_at<=?",
        (now,)
    ).fetchall()

def get_open_polls():
    return cur.execute("SELECT * FROM polls WHERE closed=0").fetchall()

def delete_poll(poll_id):
    cur.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
    cur.execute("DELETE FROM poll_messages WHERE poll_id=?", (poll_id,))
    cur.execute("DELETE FROM polls WHERE id=?", (poll_id,))
    conn.commit()

def cleanup_old_polls(max_age_seconds=7 * 24 * 3600):
    """Remove closed polls (and their votes/messages) a week after they ended."""
    cutoff = int(time.time()) - max_age_seconds
    rows = cur.execute(
        "SELECT id FROM polls WHERE closed=1 AND ends_at IS NOT NULL AND ends_at < ?",
        (cutoff,)
    ).fetchall()
    for r in rows:
        delete_poll(r["id"])

APPEAL_BRIDGE_ID_FLOOR = 100000

def next_appeal_bridge_id():
    row = cur.execute(
        "SELECT MAX(id) AS mx FROM bridges WHERE id >= ?",
        (APPEAL_BRIDGE_ID_FLOOR,)
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
    return cur.execute(
        "SELECT * FROM appeals WHERE user_id=?",
        (str(user_id),)
    ).fetchone()

def get_open_appeal(user_id):
    return cur.execute(
        "SELECT * FROM appeals WHERE user_id=? AND status='open'",
        (str(user_id),)
    ).fetchone()

def get_appeal_by_thread(thread_id):
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
    row = get_appeal(user_id)
    if row:
        cur.execute("DELETE FROM appeal_consuls WHERE thread_id=?", (row["thread_id"],))
    cur.execute("DELETE FROM appeals WHERE user_id=?", (str(user_id),))
    conn.commit()
    return row

def get_open_appeals():
    return cur.execute("SELECT * FROM appeals WHERE status='open'").fetchall()

def get_resolved_appeals_older_than(max_age_seconds):
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

def remove_chat_from_bridge(chat_id):
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
