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
        "INSERT OR REPLACE INTO chat_settings (chat_id, lang) VALUES (?,?)",
        (chat_id, lang_code)
    )
    conn.commit()

def get_chat_lang(chat_id):
    """
    Lookup language for given chat_id. First try exact chat_id (channel/thread),
    then fallback to group/server-level key '<group_id>:0' (so lang is preserved
    for whole Discord server or whole Telegram chat).
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
        group_key = f"{prefix}:0"
        row = cur.execute(
            "SELECT lang FROM chat_settings WHERE chat_id=?",
            (group_key,)
        ).fetchone()
        if row and row["lang"]:
            return row["lang"]

    return None

def remove_chat_settings_for_prefix(prefix):
    """
    Remove chat_settings rows where chat_id LIKE '<prefix>:%'
    prefix example: guild_id for discord, chat.id for telegram
    """
    cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ?",
        (f"{prefix}:%",)
    )
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

def is_user_verified(platform, user_id, prefix):
    """
    Возвращает True, если для данной платформы и user_id есть запись,
    которая либо привязана к конкретному prefix, либо глобальная (prefix='*'),
    и срок ещё не истёк.
    """
    now = int(time.time())
    row = cur.execute(
        """
        SELECT expires_at FROM verified_users
        WHERE platform=? AND user_id=? AND (prefix=? OR prefix=?)
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (platform, str(user_id), str(prefix), "*")
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
    return [r["user_id"] for r in cur.execute("SELECT user_id FROM bridge_admins WHERE bridge_id=?", (bridge_id,)).fetchall()]

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
    row = cur.execute(
        "SELECT webhooks FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    return bool(row and row["webhooks"])

def set_webhooks_enabled(chat_id, enabled: bool):
    cur.execute(
        "INSERT INTO chat_settings (chat_id, webhooks) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET webhooks=excluded.webhooks",
        (chat_id, 1 if enabled else 0)
    )
    conn.commit()

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
        bridge_deleted = True

    conn.commit()
    return {"bridge_id": bridge_id, "bridge_deleted": bridge_deleted}
