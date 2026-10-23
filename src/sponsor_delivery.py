"""Drain durable sponsor copies in owner order under current UTC budgets.

Each outgoing copy reserves one unit immediately before sending, including
gallery follow-ups. Failed attempts release their reservation and retry after
a minute; a crash after remote delivery but before recording can duplicate a
copy because the platforms offer no shared transactional delivery API.
"""
import asyncio
import json
import logging
import time

import db
import sponsors

logger = logging.getLogger("bridge.sponsor_delivery")
_delivery_lock = asyncio.Lock()

def pack_payload(payload):
    """Convert Discord embeds to JSON while preserving rendered message text."""
    saved = dict(payload)
    if saved.get("embed") is not None:
        saved["embed"] = saved["embed"].to_dict()
    if saved.get("embeds") is not None:
        saved["embeds"] = [embed.to_dict() for embed in saved["embeds"]]
    return saved

def update_pending_bodies(message_id, renderer):
    """An edited origin replaces bodies of copies still waiting for capacity.

    The renderer returns fresh body fields for this target. Forward lines are
    restored here; reply references are resolved when the copy is delivered.
    """
    from message_relay import _forward_line, escape_html
    from utils import get_chat_lang

    origin = db.cur.execute("SELECT forward_type,forward_name FROM messages WHERE id=?", (message_id,)).fetchone()
    if origin is None:
        return
    rows = db.cur.execute("SELECT * FROM sponsor_relay_queue WHERE message_id=? AND kind='main'", (message_id,)).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        fresh = renderer(row)
        forward = _forward_line(origin["forward_type"], origin["forward_name"], get_chat_lang(row["chat_id"]))
        if forward:
            for bodies in (fresh, fresh.get("_file_fallback", {})):
                for key in ("body_plain", "body_discord", "body_telegram_html"):
                    if bodies.get(key) is not None:
                        prefix = escape_html(forward) if key == "body_telegram_html" else forward
                        bodies[key] = prefix + "\n" + bodies[key]
        payload.update(pack_payload(fresh))
        db.cur.execute("UPDATE sponsor_relay_queue SET payload=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), row["id"]))
    db.conn.commit()

async def deliver(chat, payload):
    """Route persisted content through the existing platform deliverers."""
    from discord_bot.relay import deliver_discord_relay, deliver_telegram_relay

    if chat["platform"] == "discord":
        import discord
        keys = ("header", "body_discord", "reply_line", "reply_link_line",
                "reply_to_platform_message_id", "sender_name", "place_name",
                "messenger_name", "avatar_url", "is_bot_sender", "prefer_plain", "webhook_name")
        kwargs = {key: payload[key] for key in keys if key in payload}
        if payload.get("embed"):
            kwargs["embed"] = discord.Embed.from_dict(payload["embed"])
        if payload.get("embeds"):
            kwargs["embeds"] = [discord.Embed.from_dict(item) for item in payload["embeds"]]
        return await deliver_discord_relay(chat, **kwargs)
    if chat["platform"] == "inbox":
        from inbox import deliver_inbox_relay
        keys = ("header", "body_plain", "body_telegram_html", "reply_line",
                "reply_to_platform_message_id", "sender_name")
        return await deliver_inbox_relay(chat, **{key: payload[key] for key in keys})
    keys = ("header", "body_plain", "body_telegram_html", "reply_line",
            "reply_to_platform_message_id")
    return await deliver_telegram_relay(chat, **{key: payload[key] for key in keys})

async def drain_sponsor_copies(max_copies=200):
    """One bounded pass; one blocked sponsor never holds up another."""
    from utils import is_admin
    from message_relay import _resolve_reply_for_chat, _webhook_reply_link_line, load_external_reply
    from utils import get_chat_lang

    async with _delivery_lock:
        owners = db.cur.execute(
            "SELECT discord_id FROM sponsor_relay_queue GROUP BY discord_id ORDER BY MIN(id)"
        ).fetchall()
        attempts = 0
        for owner_row in owners:
            owner = owner_row[0]
            while attempts < max_copies:
                row = db.cur.execute(
                    "SELECT q.*,m.reply_to_message_id,m.reply_external FROM sponsor_relay_queue q"
                    " LEFT JOIN messages m ON m.id=q.message_id WHERE q.discord_id=? AND q.retry_at<=?"
                    " AND NOT EXISTS (SELECT 1 FROM sponsor_relay_queue earlier WHERE"
                    " earlier.discord_id=q.discord_id AND earlier.platform=q.platform"
                    " AND earlier.chat_id=q.chat_id AND earlier.id<q.id)"
                    " ORDER BY q.id LIMIT 1", (owner, int(time.time())),
                ).fetchone()
                if row is None:
                    break
                now = int(time.time())
                chat = db.cur.execute(
                    "SELECT * FROM chats WHERE platform=? AND chat_id=? AND bridge_id=?",
                    (row["platform"], row["chat_id"], row["bridge_id"]),
                ).fetchone()
                origin = db.cur.execute("SELECT id FROM messages WHERE id=?", (row["message_id"],)).fetchone()
                if chat is None or origin is None or sponsors.sponsor_relay_owner(row["bridge_id"]) != owner:
                    db.cur.execute("DELETE FROM sponsor_relay_queue WHERE id=?", (row["id"],))
                    db.conn.commit()
                    continue
                payload = json.loads(row["payload"])
                if payload.get("_file_consent_required") and not db.chat_file_consent(
                        row["platform"], row["chat_id"], row["bridge_id"]):
                    if row["kind"] == "file":
                        db.cur.execute("DELETE FROM sponsor_relay_queue WHERE id=?", (row["id"],))
                        db.conn.commit()
                        continue
                    payload.update(payload["_file_fallback"])
                reservation = None
                tier = sponsors.sponsor_quota_tier("discord", owner)
                if not is_admin("discord", owner):
                    if tier == 0:
                        break
                    caps = sponsors.sponsor_limits(tier)
                    day = time.strftime("%Y-%m-%d", time.gmtime(now))
                    if not db.spend_sponsor_relay(owner, day, now, 1, caps["relay_minute"], caps["relay_day"]):
                        break
                    reservation = (day, db.sponsor_relay_usage(owner, day)["minute_started"])
                attempts += 1
                if row["kind"] == "main":
                    line, reference = _resolve_reply_for_chat(
                        chat, get_chat_lang(row["chat_id"]), row["reply_to_message_id"],
                        load_external_reply(row["reply_external"]))
                    payload["reply_line"] = line
                    payload["reply_to_platform_message_id"] = reference
                    if chat["platform"] == "discord" and reference and db.get_webhooks_enabled(chat["chat_id"]):
                        payload["reply_link_line"] = _webhook_reply_link_line(
                            chat, get_chat_lang(row["chat_id"]), row["reply_to_message_id"], reference)
                try:
                    sent = await deliver(chat, payload)
                except Exception:
                    logger.warning("queued relay delivery failed for copy %s", row["id"])
                    sent = None
                if not sent:
                    if reservation is not None:
                        with db._db_lock:
                            db.cur.execute(
                                "UPDATE sponsor_relay_usage SET day_units=MAX(0,day_units-1),"
                                " minute_units=CASE WHEN minute_started=? THEN MAX(0,minute_units-1)"
                                " ELSE minute_units END WHERE discord_id=? AND day=?",
                                (reservation[1], owner, reservation[0]),
                            )
                    db.cur.execute("UPDATE sponsor_relay_queue SET retry_at=? WHERE id=?", (now + 60, row["id"]))
                    db.conn.commit()
                    continue
                with db._db_lock:
                    if row["kind"] == "main":
                        from message_relay import record_copy
                        record_copy(row["message_id"], chat, sent)
                    else:
                        db.cur.execute(
                            "INSERT INTO message_copies (message_id,platform,chat_id,message_id_platform,kind)"
                            " VALUES (?,?,?,?,?)", (row["message_id"], row["platform"], row["chat_id"], sent, row["kind"]),
                        )
                    db.cur.execute("DELETE FROM sponsor_relay_queue WHERE id=?", (row["id"],))
                    db.conn.commit()

async def sponsor_delivery_loop():
    """Resume after startup and check released capacity every five seconds."""
    from discord_bot.client import bot
    await bot.wait_until_ready()
    while True:
        try:
            await drain_sponsor_copies()
        except Exception:
            logger.exception("sponsor delivery pass failed")
        await asyncio.sleep(5)
