import sqlite3

conn = sqlite3.connect("bridge.db", check_same_thread=False)
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
        "INSERT INTO chats(platform, chat_id, bridge_id) VALUES(?,?,?)",
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

