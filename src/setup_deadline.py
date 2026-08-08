"""The seven-day setup deadline: a community nobody attached to a bridge is
left again.

The bot is invited far more often than it is put to use, and a bot sitting in
a server that never bridged anything is a standing permission grant paying for
nothing. Seven days after being added, a community that no Bot Admin has done
anything with is left, and the service chats are told. Nothing is said in the
community itself — the settings the rule asks for are Bot Admin settings, so
the people who could act on a warning are the operators, and they are exactly
who the service chats reach.

Three things keep the rule from ever touching a community it should not:

* **Grandfathering.** The deadline counts from the join, and a join from
  before the rule came into force (`db.rule_since`) is out of reach. Every
  community the bot was already in the day this shipped therefore stays,
  whatever state it is in.
* **One-shot.** The first sweep that finds a community configured settles it
  (`db.mark_settled`) and never looks at it again. Detaching every bridge
  years later does not put the bot out of the door.
* **Config exemption.** A community holding any chat named in `config.py` —
  service, backup and support chats, GALLERY, the verification channels, the
  appeal channels, Purgatorium — is the operator's own infrastructure and is
  never left, however it is set up.

On Discord the deadline is measured from `Guild.me.joined_at`, Discord's own
record of when the bot arrived, the same way the Purgatorium auto-kick reads
`Member.joined_at`: a restart, a lost event or an empty cache cannot make the
bot think it has been somewhere longer than it has. Telegram has no such
field, so there the row written by `my_chat_member` is the only clock, and a
group with no row is simply never examined.
"""
import logging
import time

import config
import db

logger = logging.getLogger("bridge.setup_deadline")

SETUP_GRACE_SECONDS = 7 * 24 * 3600

def _parse_chat_key(raw_key):
    """Split a config chat entry into ``(community_id, chat_id)``.

    The entries are hand-written and come in two shapes — 'guild:channel' /
    'group:topic', or a bare id, which names a chat whose community is not
    written down. Unparsable entries yield (None, None): a typo in the config
    must not decide whether the bot stays in a server."""
    key = str(raw_key).strip()
    if not key:
        return None, None
    if ":" in key:
        left, right = key.split(":", 1)
        try:
            return int(left), int(right)
        except ValueError:
            return None, None
    try:
        return None, int(key)
    except ValueError:
        return None, None

def _collect(*sources):
    """Flatten config entries — plain ids, sets of them, and the
    {'discord': {...}, 'telegram': {...}} mappings — into one set of raw
    keys."""
    out = set()
    for source in sources:
        if source is None:
            continue
        if isinstance(source, dict):
            for value in source.values():
                out |= _collect(value)
        elif isinstance(source, (set, frozenset, list, tuple)):
            for value in source:
                out |= _collect(value)
        else:
            out.add(source)
    return out

def _protected_ids(platform):
    """The community ids and chat ids of `platform` that config.py names.

    Read through getattr rather than a from-import so that a deployment whose
    config.py predates one of these settings still starts."""
    def cfg(name):
        return getattr(config, name, None)

    if platform == "discord":
        raw = _collect(
            (cfg("SERVICE_CHATS") or {}).get("discord"),
            (cfg("BACKUP_CHATS") or {}).get("discord"),
            (cfg("SUPPORT_CHATS") or {}).get("discord"),
            (cfg("APPEAL_PARDON_CHANNELS") or {}).get("discord"),
            (cfg("APPEAL_BANINFO_CHANNELS") or {}).get("discord"),
            cfg("GALLERY"), cfg("VERIFIED"), cfg("UNVERIFIED"),
            cfg("APPEAL_CHANNEL_ID"),
        )
        communities = {cfg("PURGATORIUM_GUILD_ID")} - {None}
    else:
        raw = _collect(
            (cfg("SERVICE_CHATS") or {}).get("telegram"),
            (cfg("BACKUP_CHATS") or {}).get("telegram"),
            (cfg("SUPPORT_CHATS") or {}).get("telegram"),
        )
        communities = set()

    chats = set()
    for key in raw:
        community_id, chat_id = _parse_chat_key(key)
        if community_id is not None:
            communities.add(community_id)
        if chat_id is not None:
            chats.add(chat_id)
    return communities, chats

def is_protected_guild(guild):
    """Whether this Discord server is named in config.py — by its own id, or
    by holding one of the channels the deployment is wired to."""
    communities, chats = _protected_ids("discord")
    if guild.id in communities:
        return True
    return any(channel.id in chats for channel in guild.channels)

def is_protected_telegram_chat(chat_id):
    """Whether this Telegram group is named in config.py. A group is reached
    by its own id: the config entries carry the group and the topic, and the
    group is what the bot would be leaving."""
    communities, chats = _protected_ids("telegram")
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    return chat_id in communities or chat_id in chats

async def _sweep_discord(client, now, since, events):
    """Leave every Discord server whose week ran out unconfigured, appending
    one report per departure."""
    for guild in list(client.guilds):
        try:
            me = guild.me
            if me is None:
                try:
                    me = await guild.fetch_member(client.user.id)
                except Exception:
                    me = None
            joined_at = getattr(me, "joined_at", None)
            if joined_at is None:
                continue
            joined_ts = joined_at.timestamp()
            if joined_ts <= since:
                continue
            row = db.get_deadline_row("discord", guild.id)
            if row and row["settled_at"]:
                continue
            if db.community_is_configured("discord", guild.id):
                db.mark_settled("discord", guild.id)
                continue
            if now - joined_ts < SETUP_GRACE_SECONDS:
                continue
            if is_protected_guild(guild):
                db.mark_settled("discord", guild.id)
                continue
            await guild.leave()
            db.forget_deadline("discord", guild.id)
            events.append(("left_unconfigured", {
                "platform": "Discord",
                "chat": guild.name or str(guild.id),
                "chat_id": guild.id,
                "days": SETUP_GRACE_SECONDS // 86400,
            }))
            logger.info("Left unconfigured Discord server %s (%s)", guild.name, guild.id)
        except Exception as e:
            logger.warning("setup deadline failed for Discord server %s: %s", guild.id, e)

async def _sweep_telegram(tg, now, since, events):
    """The same for the Telegram groups recorded by `my_chat_member`.

    A group is dropped from the table whichever way it leaves the rule —
    settled, exempt or left — so the table only ever holds groups still
    waiting. A failed `leave_chat` keeps its row and is retried tomorrow,
    unless the bot is plainly not there any more."""
    for row in db.get_pending_deadlines("telegram"):
        chat_id = row["server_id"]
        try:
            joined_ts = int(row["joined_at"] or 0)
            if joined_ts <= since:
                db.forget_deadline("telegram", chat_id)
                continue
            if db.community_is_configured("telegram", chat_id):
                db.mark_settled("telegram", chat_id)
                continue
            if now - joined_ts < SETUP_GRACE_SECONDS:
                continue
            if is_protected_telegram_chat(chat_id):
                db.mark_settled("telegram", chat_id)
                continue
            title = str(chat_id)
            try:
                chat = await tg.get_chat(int(chat_id))
                title = getattr(chat, "title", None) or title
            except Exception:
                pass
            await tg.leave_chat(int(chat_id))
            db.forget_deadline("telegram", chat_id)
            events.append(("left_unconfigured", {
                "platform": "Telegram",
                "chat": title,
                "chat_id": chat_id,
                "days": SETUP_GRACE_SECONDS // 86400,
            }))
            logger.info("Left unconfigured Telegram group %s", chat_id)
        except Exception as e:
            logger.warning("setup deadline failed for Telegram group %s: %s", chat_id, e)

async def setup_deadline_pass(client, tg):
    """One sweep over both platforms.

    Returns the service-chat reports to send — `(event_key, kwargs)` pairs —
    rather than sending them, so the loop in main.py owns the reporting and
    this module needs nothing from it."""
    now = time.time()
    since = db.rule_since()
    events = []
    await _sweep_discord(client, now, since, events)
    await _sweep_telegram(tg, now, since, events)
    return events
