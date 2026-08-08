"""The bot's single SQLite connection and the public db API.

This package replaces the old monolithic db.py. The split is by domain —
see each submodule's docstring — but the *interface* is unchanged: every
helper is re-imported here, so call sites keep saying ``db.add_feed(...)``
and ``db.cur.execute(...)`` exactly as before.

Connection model: one process-wide connection in WAL mode, wrapped in
_LockingConnection so the Discord half, the Telegram half and the background
loops can use it concurrently. ``cur`` is an alias of ``conn`` — the facade
returns a fresh cursor from every execute(), so chained fetches never share
cursor state between threads. The database file is opened by RELATIVE path:
the process must run with cwd = src/ (main.py and the control panel both do).

Import order at the bottom matters: submodules do ``from db import conn,
cur`` against this partially-initialized module, which works only because
conn/cur are defined above those imports. Keep new submodule imports at the
bottom, and re-export new helpers here.
"""
import sqlite3
import threading

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
        """Wrap `raw_conn`; every locking method below shares `lock`."""
        self._conn = raw_conn
        self._lock = lock

    def execute(self, sql, params=()):
        """Locked sqlite3.Connection.execute — returns a fresh cursor."""
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        """Locked executescript (used only by the schema DDL)."""
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        """Locked commit."""
        with self._lock:
            return self._conn.commit()

    def __getattr__(self, name):
        """Everything else (row_factory, backup, …) passes through unlocked —
        acceptable because nothing mutates connection state at runtime."""
        return getattr(self._conn, name)

conn = _LockingConnection(_raw_conn, _db_lock)
cur = conn

def init():
    """Bring the schema up to date (create missing tables, add missing
    columns — strictly additive, see db/schema.py). Called once from main.py
    before either bot starts."""
    from db import schema
    schema.create_all(cur, conn)

from db.bridges import (
    attach_chat,
    attach_chat_to_bridge,
    attach_chat_to_new_bridge,
    chat_exists,
    chat_server_id,
    clear_chat_inaccessible,
    get_bridge_chats,
    get_targets,
    get_telegram_chat_count,
    get_telegram_group_count,
    get_telegram_group_ids,
    mark_chat_inaccessible,
    next_free_bridge_id,
    remove_chat_from_bridge,
    remove_chat_settings_for_prefix,
    server_bridge_ids,
)
from db.messages import (
    add_gallery_upload,
    cleanup_old_messages,
    cleanup_wiki_relay_records,
    delete_gallery_upload,
    find_message_db_id_by_media_member,
    get_gallery_upload,
    is_relay_copy,
    record_media_group_members,
)
from db.admins import (
    add_bridge_admin,
    add_localizer,
    add_server_admin,
    add_server_bridge_admin,
    get_bridge_admins,
    is_localizer,
    is_server_admin,
    is_server_bridge_admin,
    remove_bridge_admin,
    remove_localizer,
    remove_server_admin,
    remove_server_bridge_admin,
)
from db.users import (
    PRIVACY_FLAGS,
    add_pending_consent,
    add_shadow_ban,
    add_verified_user,
    cleanup_expired_verified,
    delete_pending,
    get_all_pending_consents_for_user,
    get_expired_pending_consents,
    get_pending_consent,
    get_privacy_flag,
    get_user_privacy,
    is_shadow_banned,
    is_user_verified,
    remove_pending_consent,
    remove_shadow_ban,
    remove_verified_user,
    set_privacy_flag,
    toggle_privacy_flag,
)
from db.feeds import (
    add_feed,
    feed_targets,
    find_feed,
    get_all_feeds,
    get_bridge_feeds,
    get_chat_feeds,
    get_feeds_by_source_id,
    get_wiki_feed_settings,
    remove_feed,
    set_feed_last_post,
    set_wiki_feed_settings,
    wiki_settings_chat,
)
from db.appeals import (
    APPEAL_BRIDGE_ID_FLOOR,
    create_appeal,
    delete_appeal,
    find_consul_name_owner,
    get_appeal,
    get_appeal_by_thread,
    get_consul_name,
    get_consul_ord,
    get_open_appeal,
    get_open_appeals,
    get_resolved_appeals_older_than,
    has_any_appeal,
    next_appeal_bridge_id,
    remove_consul_name,
    resolve_appeal,
    set_consul_name,
)
from db.inbox import (
    INBOX_BRIDGE_ID_FLOOR,
    add_inbox_ban,
    add_inbox_bot,
    add_inbox_host,
    claim_inbox_bridge_id,
    create_inbox_conversation,
    delete_inbox_conversation,
    find_inbox_bot,
    forget_inbox_topic,
    get_inbox_bot,
    get_inbox_bots,
    get_inbox_conversation,
    get_inbox_conversation_by_bridge,
    get_inbox_conversations_of_bot,
    get_inbox_host,
    get_inbox_host_of_community,
    get_inbox_hosts,
    get_inbox_hosts_of_chat,
    get_inbox_staff_ord,
    get_inbox_topic,
    get_silent_inbox_conversations,
    inbox_chat_id,
    inbox_file_relay_enabled,
    inbox_header_hidden,
    is_inbox_banned,
    is_inbox_bridge,
    remember_inbox_topic,
    remove_inbox_ban,
    remove_inbox_bot,
    remove_inbox_host,
    set_inbox_anonymize,
    set_inbox_conversation_status,
    set_inbox_header_hidden,
    touch_inbox_conversation,
)
from db.settings import (
    add_loc_suggestion,
    bridge_file_relay_enabled,
    cleanup_old_loc_suggestions,
    delete_loc_suggestion,
    get_allow_bots,
    get_bridge_file_consent,
    get_bridge_webhooks,
    get_chat_lang,
    get_loc_suggestion,
    get_server_file_consent,
    get_server_webhooks,
    get_webhooks_enabled,
    is_verify_list_enabled,
    set_allow_bots,
    set_bridge_file_consent,
    set_bridge_webhooks,
    set_chat_lang,
    set_server_file_consent,
    set_server_webhooks,
    set_verify_list_enabled,
    set_webhooks_enabled,
)
from db.onboarding import (
    community_is_configured,
    forget_deadline,
    get_deadline_row,
    get_pending_deadlines,
    mark_settled,
    record_join,
    rule_since,
)
from db.polls import (
    add_poll_message,
    cleanup_old_polls,
    close_poll,
    create_poll,
    delete_poll,
    get_expired_open_polls,
    get_open_polls,
    get_poll,
    get_poll_by_message,
    get_poll_messages,
    get_poll_results,
    record_poll_vote,
)
