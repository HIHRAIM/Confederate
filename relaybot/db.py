import sqlite3
import time

conn = sqlite3.connect("bridge.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

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
    """)
    conn.commit()

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
        "DELETE FROM messages WHERE created_at IS NOT NULL AND created_at < ?",
        (limit,)
    )
    conn.commit()

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

def add_pending_consent(platform, prefix, user_id, bot_message_id, chat_key):
    now = int(time.time())
    cur.execute(
        "INSERT OR REPLACE INTO pending_consents (platform, prefix, user_id, bot_message_id, chat_key, created_at) VALUES (?,?,?,?,?,?)",
        (platform, str(prefix), str(user_id), str(bot_message_id), str(chat_key), now)
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
