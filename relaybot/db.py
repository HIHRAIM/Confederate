import sqlite3

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
