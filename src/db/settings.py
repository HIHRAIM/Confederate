"""Chat, community and bot-wide switches: language, bot-message relay,
webhook scopes, file-reupload consents, the Confederate Guard verify-list toggle,
and localization-suggestion tickets.

The pattern to notice: several switches exist in widening scopes (chat →
bridge → server), and the read side checks all of them, never caching —
a chat may have joined its bridge a minute ago.
"""
import time

from db import conn, cur
from db.bridges import chat_server_id

def set_chat_lang(chat_id, lang_code):
    """Store a language for a chat key — an exact 'prefix:chat' key from
    /locallang, or a bare community prefix from /lang. Validation of the code
    happens in utils.set_chat_lang; this just writes."""
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

def get_allow_bots(chat_id):
    """Whether this chat relays other bots' messages (/allow-bots). Off by
    default: bot chatter is noise in most bridges and a loop risk."""
    row = cur.execute(
        "SELECT allow_bots FROM chat_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    return bool(row and row["allow_bots"])

def set_allow_bots(chat_id, enabled: bool):
    """Toggle relaying of bot-authored messages for one chat."""
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
    """Write the legacy per-channel webhook switch. Only `/webhooks disable`
    still writes it (to False), so an old per-channel enable cannot silently
    override a server-wide disable."""
    cur.execute(
        "INSERT INTO chat_settings (chat_id, webhooks) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET webhooks=excluded.webhooks",
        (chat_id, 1 if enabled else 0)
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
    """Whether the whole server has webhook copies on."""
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
    """Whether the server's chats in this one bridge have webhook copies on."""
    return cur.execute(
        "SELECT 1 FROM bridge_webhooks WHERE server_id=? AND bridge_id=?",
        (str(server_id), int(bridge_id))
    ).fetchone() is not None

def set_server_file_consent(platform, server_id, enabled: bool, enabled_by=None):
    """Server/group-wide consent to the GALLERY file re-upload (`/allow-files`).

    The community's standing answer, and the default every one of its chats
    carries into every bridge — the ones it is in now and the ones it joins
    afterwards. Nothing materializes it per chat or per bridge, so a bridge
    built tomorrow reads the same row this writes today."""
    if enabled:
        cur.execute(
            "INSERT INTO server_file_consents (platform, server_id, enabled_by, enabled_at, left_at)"
            " VALUES (?,?,?,strftime('%s','now'),NULL)"
            " ON CONFLICT(platform, server_id) DO UPDATE SET"
            " enabled_by=excluded.enabled_by, enabled_at=excluded.enabled_at, left_at=NULL",
            (platform, str(server_id), str(enabled_by) if enabled_by is not None else None)
        )
    else:
        cur.execute(
            "DELETE FROM server_file_consents WHERE platform=? AND server_id=?",
            (platform, str(server_id))
        )
    conn.commit()

def get_server_file_consent(platform, server_id) -> bool:
    """Whether the server/group consented to the GALLERY re-upload."""
    return cur.execute(
        "SELECT 1 FROM server_file_consents WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    ).fetchone() is not None

def mark_server_departed(platform, server_id):
    """Start the seven-day countdown on a community's file consent: the bot
    has just been removed from it.

    The consent is not dropped on the spot. A kick, a re-invitation and a
    misclicked "leave" all look the same from here, and making a community
    re-answer a privacy question because the bot was out for an afternoon is
    both rude and pointless. The row keeps working while it waits — the bot
    is not in the community, so nothing of it is being relayed anyway."""
    cur.execute(
        "UPDATE server_file_consents SET left_at=strftime('%s','now')"
        " WHERE platform=? AND server_id=? AND left_at IS NULL",
        (platform, str(server_id))
    )
    conn.commit()

def clear_server_departure(platform, server_id):
    """The bot is back in the community before the seven days ran out: the
    consent stops counting down and goes on as if it had never left."""
    cur.execute(
        "UPDATE server_file_consents SET left_at=NULL"
        " WHERE platform=? AND server_id=?",
        (platform, str(server_id))
    )
    conn.commit()

FILE_CONSENT_DEPARTURE_GRACE = 7 * 24 * 3600

def cleanup_departed_file_consents():
    """Drop the consents of communities the bot left more than seven days ago.

    Called from the cleanup loop in main.py. A community that invites the bot
    back after that answers `/allow-files` again — a standing permission to
    put its files on a public CDN should not outlive the bot's presence by
    more than the grace period."""
    cur.execute(
        "DELETE FROM server_file_consents"
        " WHERE left_at IS NOT NULL AND strftime('%s','now') - left_at >= ?",
        (FILE_CONSENT_DEPARTURE_GRACE,)
    )
    conn.commit()

def set_bridge_file_consent(bridge_id, enabled: bool, enabled_by=None):
    """Bridge-wide consent to the GALLERY file re-upload (`/allow-files local`).
    Covers every side of the bridge — whatever their own communities answered,
    and including chats attached later."""
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
    """Whether the bridge as a whole consented to the GALLERY re-upload."""
    return cur.execute(
        "SELECT 1 FROM bridge_file_consents WHERE bridge_id=?",
        (int(bridge_id),)
    ).fetchone() is not None

def chat_file_consent(platform, chat_id, bridge_id=None) -> bool:
    """Whether this one chat is covered by an `/allow-files` consent.

    The single primitive the whole mechanic is decided by, and it is a
    per-side question: the bridge-wide consent of `bridge_id` covers every
    side at once, otherwise the chat's own community answers for it. Never
    cached — a chat may have joined its bridge a minute ago, and the answer
    for a community that never said anything must not be frozen the moment
    the bridge was built.

    A chat that belongs to no community — an appeal DM, a receiver bot's
    private chat — has nobody to ask and is covered only by the bridge-wide
    consent."""
    if bridge_id is not None and get_bridge_file_consent(bridge_id):
        return True
    server_id = chat_server_id(platform, chat_id)
    if not server_id:
        return False
    return get_server_file_consent(platform, server_id)

def file_reupload_allowed(bridge_id, origin_platform, origin_chat_id) -> bool:
    """Whether an incoming message's files should be re-uploaded to GALLERY at
    all — the question asked once, before the download.

    Two conditions, and they are about different people. The chat the files
    came from must be covered, because the mechanic takes its files out of
    Telegram and puts them on a public CDN. And at least one *other* chat of
    the bridge must be covered too, or the upload would serve nobody: every
    target would get the "[N files from Telegram]" marker anyway.

    Which targets actually receive the links is decided per chat afterwards,
    by `chat_file_consent` inside message_relay.relay_message. One community
    that never answered no longer silences the whole bridge."""
    from db.bridges import get_bridge_chats
    if not chat_file_consent(origin_platform, origin_chat_id, bridge_id):
        return False

    for c in get_bridge_chats(bridge_id):
        if c["platform"] == origin_platform and c["chat_id"] == origin_chat_id:
            continue
        if chat_file_consent(c["platform"], c["chat_id"], bridge_id):
            return True
    return False

def set_verify_list_enabled(enabled):
    """Toggle publishing of (un)verified user IDs to the VERIFIED/UNVERIFIED
    channels (/verify-list) — the Confederate Guard sync described in ARCHITECTURE.md."""
    cur.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('verify_list_enabled', ?)",
        ("1" if enabled else "0",)
    )
    conn.commit()

def is_verify_list_enabled():
    """Whether (un)verified user IDs are published to the VERIFIED/UNVERIFIED
    channels for Confederate Guard to mirror. Enabled by default."""
    row = cur.execute(
        "SELECT value FROM bot_settings WHERE key='verify_list_enabled'"
    ).fetchone()
    return row is None or row["value"] == "1"

def add_loc_suggestion(code, platform, user_id, username, lang, rkey, suggestion, ui_lang):
    """File a /loc-suggest ticket under its short hex code. ui_lang remembers
    the suggester's interface language so the eventual /loc-reply can answer
    in it."""
    cur.execute(
        "INSERT OR REPLACE INTO loc_suggestions "
        "(code, platform, user_id, username, lang, rkey, suggestion, ui_lang, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (code, platform, str(user_id), username, lang, rkey, suggestion, ui_lang, int(time.time()))
    )
    conn.commit()

def get_loc_suggestion(code):
    """The ticket behind a /loc-reply code, or None."""
    return cur.execute(
        "SELECT * FROM loc_suggestions WHERE code=?",
        (code,)
    ).fetchone()

def delete_loc_suggestion(code):
    """Close a ticket once the reply reached its author."""
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
