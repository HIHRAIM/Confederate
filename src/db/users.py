"""Per-user state: forwarding consents (verification), pending consent
prompts, /privacy switches and shadow bans.

Consent is the legal heart of the relay: a user's messages are copied to
other communities only after they pressed the consent button, and the prompt
flow (pending_consents) holds their first message until they do. Not this
module's zone: the consent UI itself (commands/user.py on both platforms).
"""
import time

from db import conn, cur

def add_verified_user(platform, user_id, prefix, days_valid=365):
    """Record a forwarding consent, valid `days_valid` days. `prefix` is the
    community where it was given — kept for the record, though the consent
    counts platform-wide (see is_user_verified)."""
    now = int(time.time())
    expires = now + days_valid * 86400
    cur.execute(
        "INSERT OR REPLACE INTO verified_users (platform, user_id, prefix, verified_at, expires_at) VALUES (?,?,?,?,?)",
        (platform, str(user_id), str(prefix), now, expires)
    )
    conn.commit()

def is_user_verified(platform, user_id, prefix=None):
    """True when the user has an unexpired consent on this platform. Consent
    to forwarding is one per platform, so `prefix` (the chat/server where it
    was given) does not affect the check — the parameter stays only for
    signature compatibility."""
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
    """Withdraw one recorded consent (exact platform+user+prefix row). The
    /unverify commands instead delete all of a user's platform rows inline."""
    cur.execute(
        "DELETE FROM verified_users WHERE platform=? AND user_id=? AND prefix=?",
        (platform, str(user_id), str(prefix))
    )
    conn.commit()

def cleanup_expired_verified():
    """Drop consents past their expiry — after this the user is prompted
    again on their next message. Runs from pending_cleanup_loop."""
    now = int(time.time())
    cur.execute(
        "DELETE FROM verified_users WHERE expires_at<?",
        (now,)
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
    """Record an outstanding consent prompt. `bot_message_id` is the prompt
    message (deleted when answered or expired); `first_message_id` the user's
    held-back first message, and `first_message_payload` its serialized relay
    payload on Telegram — a Telegram message object can't be re-fetched later,
    so everything needed to relay it after consent is captured now."""
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
    """The outstanding prompt for this user in this community, or None. While
    one exists, the user's further messages are deleted instead of relayed."""
    return cur.execute(
        "SELECT * FROM pending_consents WHERE platform=? AND prefix=? AND user_id=?",
        (platform, str(prefix), str(user_id))
    ).fetchone()

def remove_pending_consent(platform, prefix, user_id):
    """Drop one outstanding prompt record (the prompt message is deleted by
    the caller, which is the side that has the chat handle)."""
    cur.execute(
        "DELETE FROM pending_consents WHERE platform=? AND prefix=? AND user_id=?",
        (platform, str(prefix), str(user_id))
    )
    conn.commit()

def get_all_pending_consents_for_user(platform, user_id):
    """Every community's outstanding prompt for the user — one consent click
    answers all of them at once, so the handler needs the whole list."""
    return cur.execute(
        "SELECT * FROM pending_consents WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchall()

def get_expired_pending_consents(older_than_seconds=24*3600):
    """Prompts older than the cutoff, for pending_cleanup_loop to delete
    together with their prompt messages and held first messages."""
    cutoff = int(time.time()) - older_than_seconds
    return cur.execute(
        "SELECT * FROM pending_consents WHERE created_at<?",
        (cutoff,)
    ).fetchall()

def delete_pending(platform, prefix, user_id):
    """Legacy alias of remove_pending_consent, kept for old call sites."""
    remove_pending_consent(platform, prefix, user_id)

def add_shadow_ban(platform, user_id):
    """Shadow-ban a user: every inbound handler silently deletes their
    messages instead of relaying (/shadow-ban, bridge admins and up)."""
    cur.execute(
        "INSERT OR IGNORE INTO shadow_bans (platform, user_id) VALUES (?,?)",
        (platform, str(user_id))
    )
    conn.commit()

def remove_shadow_ban(platform, user_id):
    """Lift a shadow ban. No command exposes this today — it exists for the
    control panel and manual intervention."""
    cur.execute(
        "DELETE FROM shadow_bans WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    )
    conn.commit()

def is_shadow_banned(platform, user_id):
    """Whether the user is shadow-banned on this platform — checked before
    every relay, including appeal DMs and threads."""
    row = cur.execute(
        "SELECT 1 FROM shadow_bans WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone()
    return row is not None

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
    """One /privacy switch. The flag name is interpolated into the SQL, hence
    the whitelist check — never call this with user-supplied flag names."""
    if flag not in PRIVACY_FLAGS:
        raise ValueError(f"unknown privacy flag: {flag}")
    row = cur.execute(
        f"SELECT {flag} FROM user_privacy WHERE platform=? AND user_id=?",
        (platform, str(user_id))
    ).fetchone()
    return bool(row and row[flag])

def set_privacy_flag(platform, user_id, flag, enabled: bool):
    """Set one /privacy switch, creating the row on first use (same
    whitelist caveat as get_privacy_flag)."""
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
