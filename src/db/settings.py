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
    """Whether the server/group consented to the GALLERY re-upload."""
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
    """Whether the bridge as a whole consented to the GALLERY re-upload."""
    return cur.execute(
        "SELECT 1 FROM bridge_file_consents WHERE bridge_id=?",
        (int(bridge_id),)
    ).fetchone() is not None

def bridge_file_relay_enabled(bridge_id) -> bool:
    """Whether Telegram files may be re-uploaded to GALLERY for this bridge.

    Every chat of the bridge must be covered — by the bridge-wide consent or by
    its own server/group consent — because the mechanic both takes files out of
    one chat and posts public CDN links into all the others. Never cached: a
    chat may have joined the bridge a minute ago."""
    from db.bridges import get_bridge_chats
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
