"""Persistent rendered outgoing copies awaiting sponsor delivery capacity."""
import json
import time

from db import conn, cur

def enqueue_sponsor_copy(owner, bridge_id, message_id, chat, payload, kind="main"):
    """Store only message content and IDs; callbacks and credentials stay out."""
    cur.execute(
        "INSERT INTO sponsor_relay_queue"
        " (discord_id,bridge_id,message_id,platform,chat_id,payload,kind,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (str(owner), int(bridge_id), int(message_id), chat["platform"],
         str(chat["chat_id"]), json.dumps(payload, ensure_ascii=False), kind, int(time.time())),
    )
    conn.commit()

def queued_sponsor_copies(owner):
    """Current backlog, shared across all of a subscription's bridges."""
    return cur.execute("SELECT COUNT(*) FROM sponsor_relay_queue WHERE discord_id=?",
                       (str(owner),)).fetchone()[0]
