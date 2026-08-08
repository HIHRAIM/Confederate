"""Every CREATE TABLE of the bot, plus the additive column migrations.

This module owns the shape of bridge.db and nothing else — no queries, no
business logic. `db.init()` calls `create_all()` once at start-up.

The migration policy is strict because the database is live production data:
new tables only via CREATE TABLE IF NOT EXISTS, new columns only via a
PRAGMA table_info check followed by ALTER TABLE ADD COLUMN. Never DROP, never
rebuild a table, never an ALTER that can lose data. Table documentation lives
in `--` comments inside the SQL string: python-level `#` comments would be
stripped by the parent folder's clean_code.py, SQL comments inside a string
literal survive.
"""

SCHEMA_SQL = """
-- bridges: one row per bridge; nothing but the number. A bridge exists as
-- long as at least one chat points at it (see remove_chat_from_bridge).
-- Written by attach_chat, read everywhere. The id space is split in three:
-- ordinary bridges below 100000, appeal bridges in [100000, 1000000)
-- (db/appeals.py: APPEAL_BRIDGE_ID_FLOOR) and inbox conversations at and
-- above 1000000 (db/inbox.py: INBOX_BRIDGE_ID_FLOOR).
CREATE TABLE IF NOT EXISTS bridges (
    id INTEGER PRIMARY KEY
);

-- chats: membership of chats in bridges. chat_id is the bot-wide chat key:
-- 'guild:channel' (Discord), 'group:topic' (Telegram, topic 0 = the plain
-- group), 'dm:<user_id>' (appeal DM side) or '<bot_id>:<user_id>' with
-- platform 'inbox' (the private chat of a receiver bot, see inbox_bots).
-- Written by /atb, the appeal system and the inbox system, deleted by /rfb
-- and the daily reachability sweep.
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT,
    chat_id TEXT UNIQUE,
    bridge_id INTEGER
);

-- messages: one row per relayed origin message. Written by relay_message,
-- read by the edit/delete propagation, whois and reply resolution.
-- origin_sender_name is kept so a webhook reply line can name the author
-- after the member left; reply_to_message_id points at another messages.id;
-- forward_type/forward_name reproduce the "(forwarded from ...)" line on
-- edits. Rows older than 30 days are dropped by cleanup_old_messages.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_id INTEGER,
    origin_platform TEXT,
    origin_chat_id TEXT,
    origin_message_id TEXT,
    origin_sender_id TEXT,
    created_at INTEGER
);

-- message_copies: the per-chat copies of a relayed message. kind='main' is
-- the body copy every edit targets; kind='file' are the extra one-link-per-
-- file follow-up messages on Telegram. Written by relay_message, read by
-- edit/delete propagation, whois and is_relay_copy.
CREATE TABLE IF NOT EXISTS message_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    platform TEXT,
    chat_id TEXT,
    message_id_platform TEXT
);

-- chat_admins: per-chat materialization of admin grants. Not written
-- directly by a command: rows appear as a side effect of add_bridge_admin /
-- add_server_bridge_admin and when a chat joins a bridge (attach_chat).
-- Read by utils.is_chat_admin.
CREATE TABLE IF NOT EXISTS chat_admins (
    platform TEXT,
    chat_id TEXT,
    user_id TEXT,
    PRIMARY KEY (platform, chat_id, user_id)
);

-- server_admins: server-wide Local Admins (/setlocaladmin). Consumed mainly
-- by the external control panel for scoped logins; username is kept for the
-- panel's username login.
CREATE TABLE IF NOT EXISTS server_admins (
    platform TEXT NOT NULL,
    server_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT,
    added_by TEXT,
    added_at INTEGER,
    PRIMARY KEY (platform, server_id, user_id)
);

-- localizers: users allowed to edit this bot's localization through the
-- control panel (/localizer-add). Bot admins are localizers implicitly and
-- have no row here.
CREATE TABLE IF NOT EXISTS localizers (
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT,
    added_by TEXT,
    added_at INTEGER,
    PRIMARY KEY (platform, user_id)
);

-- dead_chats: /deadchat config — ping role_id after `hours` of silence.
-- last_message_ts is refreshed by every on_message and by the ping itself.
CREATE TABLE IF NOT EXISTS dead_chats (
    chat_id TEXT PRIMARY KEY,
    role_id TEXT,
    hours INTEGER,
    last_message_ts INTEGER
);

-- news_chats: /newschat config — emojis (JSON list) the bot reacts with on
-- every message of the chat.
CREATE TABLE IF NOT EXISTS news_chats (
    chat_id TEXT PRIMARY KEY,
    emojis TEXT
);

-- bridge_rules: /remindrules config, one per bridge. NOTE: the column is
-- named `hours` but stores MINUTES (the command parses '2h'/'30m' into
-- minutes). messages = post only after this many messages since last post
-- (NULL = no threshold); message_counter is incremented by every relay.
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

-- chat_settings: per-chat switches. chat_id may also be a bare community
-- prefix (guild/group id) holding the community-wide /lang default —
-- get_chat_lang falls back exact -> prefix -> legacy 'prefix:0'.
-- allow_bots: relay other bots' messages; webhooks: legacy per-channel
-- webhook switch (newer scopes live in server_webhooks/bridge_webhooks).
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id TEXT PRIMARY KEY,
    lang TEXT
);

-- verified_users: forwarding consents. Consent is effectively per-platform
-- (is_user_verified ignores prefix); prefix records where it was given.
-- Expired rows are swept by cleanup_expired_verified.
CREATE TABLE IF NOT EXISTS verified_users (
    platform TEXT,
    user_id TEXT,
    prefix TEXT,
    verified_at INTEGER,
    expires_at INTEGER,
    PRIMARY KEY (platform, user_id, prefix)
);

-- pending_consents: an outstanding consent prompt per (platform, community,
-- user). first_message_id/first_message_payload hold the user's very first
-- message so it can be relayed (Telegram: replayed from the serialized
-- payload) once they press the consent button. Expired prompts are removed
-- by pending_cleanup_loop after 24h.
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

-- bridge_admins: Bridge Admin grants for one bridge (/setadmin scope:local),
-- plus materialized rows from server-wide grants. Read by get_bridge_admins.
CREATE TABLE IF NOT EXISTS bridge_admins (
    bridge_id INTEGER,
    user_id TEXT,
    PRIMARY KEY (bridge_id, user_id)
);

-- server_bridge_admins: Bridge Admin across a whole server/group
-- (/setadmin without scope) — covers bridges the server joins later; the
-- current ones also get bridge_admins/chat_admins rows immediately.
CREATE TABLE IF NOT EXISTS server_bridge_admins (
    platform TEXT NOT NULL,
    server_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    added_by TEXT,
    added_at INTEGER,
    PRIMARY KEY (platform, server_id, user_id)
);

-- shadow_bans: users whose messages are silently deleted instead of
-- relayed (/shadow-ban). Written by bridge admins, read by every inbound
-- handler.
CREATE TABLE IF NOT EXISTS shadow_bans (
    platform TEXT,
    user_id TEXT,
    PRIMARY KEY (platform, user_id)
);

-- inaccessible_chats: bookkeeping of the daily reachability sweep. A chat
-- unreachable for 24h+ is auto-detached from its bridge; first_failed_ts
-- anchors that window, any successful check deletes the row.
CREATE TABLE IF NOT EXISTS inaccessible_chats (
    platform TEXT,
    chat_id TEXT PRIMARY KEY,
    first_failed_ts INTEGER,
    last_failed_ts INTEGER
);

-- deadtopic_chats: /deadtopic config — send-and-delete a phantom message
-- after `days` days of silence so the thread is not archived out from under
-- the conversation. days defaults to 6 (what /deadtopic sets); inbox
-- conversation threads are registered with 3, since an unanswered one must
-- survive a weekend without anybody touching it.
-- bot_last_sent_ts keeps the loop from firing twice in one UTC day.
CREATE TABLE IF NOT EXISTS deadtopic_chats (
    chat_id TEXT PRIMARY KEY,
    last_message_ts INTEGER,
    bot_last_sent_ts INTEGER
);

-- media_group_members: maps every Telegram message id of an album to the
-- one relayed message, so a reply to any file of the album resolves.
CREATE TABLE IF NOT EXISTS media_group_members (
    chat_id TEXT,
    message_id_platform TEXT,
    message_id INTEGER,
    PRIMARY KEY (chat_id, message_id_platform)
);

-- loc_suggestions: /loc-suggest tickets awaiting a /loc-reply, keyed by the
-- short hex code shown to the suggester. ui_lang remembers the language to
-- answer in. Swept after a year.
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

-- polls: /poll definitions. options is a JSON list; closed is set by
-- poll_loop after ends_at (results are posted at that moment). Closed polls
-- are swept a week after they end.
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_id INTEGER,
    question TEXT,
    options TEXT,
    created_at INTEGER,
    ends_at INTEGER,
    closed INTEGER DEFAULT 0
);

-- poll_messages: the interactive poll message posted in each chat of the
-- bridge — needed to reply with results and to delete the poll everywhere.
CREATE TABLE IF NOT EXISTS poll_messages (
    poll_id INTEGER,
    platform TEXT,
    chat_id TEXT,
    message_id TEXT,
    PRIMARY KEY (poll_id, platform, chat_id)
);

-- poll_votes: one vote per (poll, platform, user); re-voting replaces it.
-- Votes are anonymous: only aggregated counts are ever shown.
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id INTEGER,
    platform TEXT,
    user_id TEXT,
    option_index INTEGER,
    PRIMARY KEY (poll_id, platform, user_id)
);

-- bot_settings: free-form key/value switches of the bot itself. Currently
-- only 'verify_list_enabled' (Confederate Guard verification sync).
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- appeals: one appeal per user (a new appeal replaces the resolved one).
-- thread_id is the Purgatorium thread, bridge_id the dedicated appeal
-- bridge (id >= 100000), lang the appellant's client language. status:
-- 'open' | 'pardoned' | 'condemned'. Resolved rows are garbage-collected
-- 30 days after the verdict.
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

-- appeal_consuls: per-thread anonymization indexes — ord 0 renders as
-- 'Consul A', 1 as 'Consul B', ... The index is reserved on a consul's
-- first message in the thread and never reused within it.
CREATE TABLE IF NOT EXISTS appeal_consuls (
    thread_id TEXT NOT NULL,
    consul_user_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (thread_id, consul_user_id)
);

-- consul_names: a consul's /setname alias, shown to appellants instead of
-- 'Consul A'. normalized is the case-folded comparison form used for
-- uniqueness. Deliberately NOT removed with an appeal: the alias outlives
-- the appeals it was used in.
CREATE TABLE IF NOT EXISTS consul_names (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized TEXT NOT NULL,
    set_by TEXT,
    set_at INTEGER
);

-- server_file_consents: /allow-files consent per server/group — permits
-- re-uploading that community's Telegram files to the GALLERY channel and
-- handing out the CDN links.
CREATE TABLE IF NOT EXISTS server_file_consents (
    platform TEXT NOT NULL,
    server_id TEXT NOT NULL,
    enabled_by TEXT,
    enabled_at INTEGER,
    PRIMARY KEY (platform, server_id)
);

-- bridge_file_consents: /allow-files local — the same consent granted for
-- one bridge as a whole.
CREATE TABLE IF NOT EXISTS bridge_file_consents (
    bridge_id INTEGER PRIMARY KEY,
    enabled_by TEXT,
    enabled_at INTEGER
);

-- gallery_uploads: the GALLERY message holding a relayed message's
-- re-uploaded files. urls/source_message_ids/file_ids are JSON lists;
-- file_ids let an edit tell changed attachments from a changed caption and
-- leave working links alone.
CREATE TABLE IF NOT EXISTS gallery_uploads (
    message_id INTEGER PRIMARY KEY,
    channel_id TEXT,
    gallery_message_id TEXT,
    urls TEXT,
    source_message_ids TEXT,
    file_ids TEXT,
    created_at INTEGER
);

-- user_privacy: /privacy switches. No row = everything off (the behaviour
-- the bot had before /privacy existed). hide_whois hides profile details
-- from whois, hide_avatar swaps the webhook avatar for a placeholder,
-- block_mention opts out of /mention.
CREATE TABLE IF NOT EXISTS user_privacy (
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    hide_whois INTEGER NOT NULL DEFAULT 0,
    hide_avatar INTEGER NOT NULL DEFAULT 0,
    block_mention INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    PRIMARY KEY (platform, user_id)
);

-- feeds: followed outside sources (/set*feed). kind: 'bluesky' | 'youtube'
-- | 'telegram' | 'wiki' | 'wikidisc'. source is the normalized handle,
-- chat_id the chat it was attached in — delivery targets are resolved per
-- post via feed_targets, so the whole bridge receives it. last_post_id is
-- the newest post already seen (feeds start from 'now', never replaying
-- history). live=1 marks a Telegram channel the bot administrates: its
-- posts are pushed via channel_post and the source is never polled.
-- source_id keeps the platform's numeric id for that matching.
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

-- wiki_feed_settings: per-subscription filters and output settings for a
-- wiki feed. Keyed on the feeds row it belongs to (kind, source, chat_id);
-- kind is always 'wiki' and is kept so the reference is the whole feeds key
-- rather than part of it. One row therefore serves the whole bridge, exactly
-- as the feeds row does: the activity reaches every chat of the bridge and
-- every one of them sees the same events, whichever chat an admin ran
-- /wikifeed-settings in. A subscription with no row here uses the defaults
-- from wiki_events (every group except patrol, all namespaces, bot edits
-- hidden, minor edits shown, Discord embeds, delivery following the bridge's
-- own /webhooks setting, a webhook of the wiki's own). event_types and
-- namespaces are comma-separated allow-lists; NULL in namespaces means every
-- namespace. webhook: 'own' gives the wiki its own Discord webhook, named
-- after it and wearing its hosting's logo; 'shared' puts it through the
-- bridge's common webhook instead.
-- Written by /wikifeed-settings, read on every relayed change.
CREATE TABLE IF NOT EXISTS wiki_feed_settings (
    kind TEXT NOT NULL DEFAULT 'wiki',
    source TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    event_types TEXT,
    namespaces TEXT,
    show_bots INTEGER NOT NULL DEFAULT 0,
    show_minor INTEGER NOT NULL DEFAULT 1,
    discord_format TEXT NOT NULL DEFAULT 'embed',
    delivery TEXT NOT NULL DEFAULT 'auto',
    webhook TEXT NOT NULL DEFAULT 'own',
    updated_by TEXT,
    updated_at INTEGER,
    PRIMARY KEY (kind, source, chat_id)
);

-- server_webhooks: /webhooks enable for a whole Discord server — relayed
-- copies arrive as webhook messages in every chat of that server, in any
-- bridge, including chats attached later.
CREATE TABLE IF NOT EXISTS server_webhooks (
    server_id TEXT PRIMARY KEY,
    enabled_by TEXT,
    enabled_at INTEGER
);

-- bridge_webhooks: /webhooks enable local — the same, limited to the
-- server's chats in one bridge.
CREATE TABLE IF NOT EXISTS bridge_webhooks (
    server_id TEXT NOT NULL,
    bridge_id INTEGER NOT NULL,
    enabled_by TEXT,
    enabled_at INTEGER,
    PRIMARY KEY (server_id, bridge_id)
);

-- inbox_bots: other Telegram bots whose private chats this bot serves
-- (/setinbox). bot_id is the receiver bot's numeric Telegram id; token is
-- the bot token, ENCRYPTED with BACKUP_KEY (backup_crypto.encrypt_secret) —
-- it is never stored, logged or answered in clear. anonymize=1 signs staff
-- replies as 'Staff A/B/…' instead of their names. owner_platform/owner_id
-- record who handed the token in: they may update it and manage the bot
-- beside the Bot Admins.
CREATE TABLE IF NOT EXISTS inbox_bots (
    bot_id TEXT PRIMARY KEY,
    username TEXT,
    title TEXT,
    token TEXT NOT NULL,
    anonymize INTEGER NOT NULL DEFAULT 0,
    owner_platform TEXT,
    owner_id TEXT,
    added_at INTEGER,
    updated_at INTEGER
);

-- inbox_hosts: the chats a receiver bot opens its conversations in
-- (/setinboxchat). One row per host chat: a Discord channel, where every
-- conversation becomes a thread, or a Telegram forum group, where it becomes
-- a topic. Several rows per bot are the point — one incoming private chat
-- then reaches a thread AND a topic, and the conversation bridge spans them
-- all.
-- hide_header is /close-header: with it on, messages out of the private chat
-- arrive in this host's threads and topics as bare text, without the
-- '[Telegram | DM] Name:' line. It lives here rather than on the thread
-- because a thread is made fresh for every conversation and a setting on one
-- would die with it — and rather than on the bot, because one team may want
-- the headers and another team hosting the same bot may not.
CREATE TABLE IF NOT EXISTS inbox_hosts (
    bot_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    added_by TEXT,
    added_at INTEGER,
    PRIMARY KEY (bot_id, chat_id)
);

-- inbox_conversations: one open conversation per (receiver bot, user), and
-- one bridge per conversation — the private chat plus the thread/topic
-- opened in each host. bridge_id comes from the reserved inbox range
-- (db/inbox.py: INBOX_BRIDGE_ID_FLOOR). lang is the writer's client
-- language, kept so a closing notice can still be worded for them.
-- last_message_ts drives the 30-day silence sweep. title is the writer's
-- display name as the thread and topic are named after it, and status is
-- 'user' | 'staff' | 'closed' — which side spoke last, i.e. which of the
-- 🟩/🟨/⬛ marks the title currently carries. Both are stored rather than
-- derived so a rename is only issued when the mark actually changes:
-- Discord rate-limits thread renames hard (two per ten minutes).
CREATE TABLE IF NOT EXISTS inbox_conversations (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bridge_id INTEGER NOT NULL,
    lang TEXT,
    created_at INTEGER,
    last_message_ts INTEGER,
    PRIMARY KEY (bot_id, user_id)
);

-- inbox_topics: the Telegram topic one writer owns in one host group, kept
-- across closings. A closed conversation leaves its topic behind closed
-- rather than deleted, and the writer's next message reopens *that* topic
-- instead of littering the group with a second one; the row is what makes
-- the reopening possible after the conversation record is gone. Discord is
-- deliberately not recorded here — an archived thread is cheap to leave
-- alone and a new one reads better in a channel list.
CREATE TABLE IF NOT EXISTS inbox_topics (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    host_chat_id TEXT NOT NULL,
    topic_chat_id TEXT NOT NULL,
    created_at INTEGER,
    PRIMARY KEY (bot_id, user_id, host_chat_id)
);

-- inbox_staff: per-conversation anonymization indexes — ord 0 renders as
-- 'Staff A', 1 as 'Staff B', … The index is reserved on a staff member's
-- first message while anonymization is on, and never reused within the
-- conversation: someone who keeps writing stays the same letter, and turning
-- anonymization off and on again does not reshuffle who is who.
CREATE TABLE IF NOT EXISTS inbox_staff (
    bridge_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (bridge_id, platform, user_id)
);

-- setup_deadlines: the seven-day setup deadline (setup_deadline.py). One row
-- per community the bot was added to AFTER the rule came into force —
-- everything it was already sitting in has no row and is never examined.
-- joined_at is what the deadline counts from; on Discord it is only a
-- record, since Guild.me.joined_at is authoritative there and survives a
-- restart, while Telegram offers nothing of the kind and the my_chat_member
-- update that adds the bot is the only moment the time can be learnt.
-- settled_at is set the first time the community is found configured and is
-- what makes the check one-shot: a community that was set up once is never
-- left afterwards, however its configuration changes later.
CREATE TABLE IF NOT EXISTS setup_deadlines (
    platform TEXT NOT NULL,
    server_id TEXT NOT NULL,
    joined_at INTEGER,
    settled_at INTEGER,
    PRIMARY KEY (platform, server_id)
);

-- inbox_bans: users barred from writing to one receiver bot (/inboxban).
-- Scoped to the bot, not to the whole bridge network — unlike shadow_bans,
-- which is bot-wide. A ban closes the open conversation and drops everything
-- the user sends that bot afterwards.
CREATE TABLE IF NOT EXISTS inbox_bans (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    banned_by TEXT,
    banned_at INTEGER,
    PRIMARY KEY (bot_id, user_id)
);
"""

_COLUMN_ADDITIONS = [
    ("messages", "origin_sender_id", "TEXT"),
    ("messages", "origin_sender_name", "TEXT"),
    ("messages", "reply_to_message_id", "INTEGER"),
    ("messages", "forward_type", "TEXT"),
    ("messages", "forward_name", "TEXT"),
    ("pending_consents", "first_message_id", "TEXT"),
    ("pending_consents", "first_message_payload", "TEXT"),
    ("message_copies", "kind", "TEXT DEFAULT 'main'"),
    ("chat_settings", "allow_bots", "INTEGER DEFAULT 0"),
    ("chat_settings", "webhooks", "INTEGER DEFAULT 0"),
    ("wiki_feed_settings", "webhook", "TEXT NOT NULL DEFAULT 'own'"),
    ("deadtopic_chats", "days", "INTEGER DEFAULT 6"),
    ("inbox_conversations", "title", "TEXT"),
    ("inbox_conversations", "status", "TEXT DEFAULT 'user'"),
    ("inbox_hosts", "hide_header", "INTEGER DEFAULT 0"),
]

def create_all(cur, conn):
    """Create every missing table and add every missing column.

    Idempotent and additive by design (see the module docstring): running it
    against a database from any older version brings the schema up to date
    without touching existing data. Called from db.init() on every start."""
    cur.executescript(SCHEMA_SQL)
    conn.commit()

    for table, column, ddl in _COLUMN_ADDITIONS:
        cols = [r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.commit()
