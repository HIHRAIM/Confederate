"""Entry point: start both halves of the bridge and the loops that belong to
neither of them.

Run it from this directory — the database and .env are opened by relative
path: ``python main.py``.

What lives here is the cross-platform machinery: the service-chat reporter
every loop uses to complain, the followed-source poller (scheduling, backoff
and throttling; the readers themselves are in sources/), the poll closer, the
consent/verification cleanup and the daily reachability sweep. Loops that only
concern Discord — status, backups, dead chats and topics, appeal maintenance —
are started by the client itself in discord_bot/client.py.

Import order matters at the bottom of the imports: `db.init()` runs before
either bot package is imported further, so the schema exists before anything
queries it.
"""
import asyncio
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bridge.main")

import db
from config import DISCORD_TOKEN
from discord_bot import bot as discord_bot
from telegram_bot import main as tg_main
from utils import get_chat_lang, localized_service_event
from config import SERVICE_CHATS

db.init()
db.cleanup_old_messages(days=30)

def _normalize_service_chat_key(platform, raw_key):
    """Parse a config.SERVICE_CHATS entry into ``(prefix, target)``.

    The entries are written by hand and come in two shapes: 'group:topic' /
    'guild:channel', or a bare id — which means a group with no topic on
    Telegram and a channel of unknown guild on Discord. Returns (None, None)
    for anything unparsable rather than raising: a typo in the config must
    not stop the bot from starting."""
    key = str(raw_key).strip()
    if not key:
        return None, None

    if ":" in key:
        left, right = key.split(":", 1)
        try:
            return int(left), int(right)
        except Exception:
            return None, None

    try:
        single = int(key)
    except Exception:
        return None, None

    if platform == "telegram":
        return single, 0
    if platform == "discord":
        return None, single
    return None, None

async def send_service_event(event_key, **kwargs):
    """Report an operational event to the service chats of both platforms,
    each in its own language. Every send is guarded — the service chats are
    where failures are reported, so a failure there must stay a log line."""
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    for chat_key in SERVICE_CHATS.get("telegram", set()):
        try:
            chat_id, thread = _normalize_service_chat_key("telegram", chat_key)
            if chat_id is None:
                continue
            lang = get_chat_lang(f"{chat_id}:{thread}")
            text = localized_service_event(event_key, lang, **kwargs)
            await tg.send_message(
                int(chat_id),
                text,
                message_thread_id=int(thread) or None
            )
        except Exception as e:
            logger.warning("send_service_event failed for telegram %s: %s", chat_key, e)

    for chat_key in SERVICE_CHATS.get("discord", set()):
        try:
            guild_id, channel_id = _normalize_service_chat_key("discord", chat_key)
            if channel_id is None:
                continue
            ch = dc.get_channel(channel_id)
            if not ch:
                try:
                    ch = await dc.fetch_channel(channel_id)
                except Exception:
                    ch = None
            effective_guild_id = guild_id
            if effective_guild_id is None and ch and getattr(ch, "guild", None):
                effective_guild_id = ch.guild.id
            lang_key = f"{effective_guild_id}:{channel_id}" if effective_guild_id is not None else str(channel_id)
            lang = get_chat_lang(lang_key)
            text = localized_service_event(event_key, lang, **kwargs)
            if ch:
                await ch.send(text)
        except Exception as e:
            logger.warning("send_service_event failed for discord %s: %s", chat_key, e)

async def rules_loop():
    """Legacy rules poster, kept alongside DiscordBot.bridge_rules_loop.

    It reads bridge_rules.hours as HOURS while the command that writes the
    column stores MINUTES, so with the intervals the commands produce its
    time check effectively never passes and the client-side loop is what
    actually posts. Left running because it costs one query a minute and
    removing it is a behaviour change, not a refactor — see the findings list
    in the stage-1 report."""
    import time
    from discord_bot import bot as dc
    from telegram_bot import bot as tg

    while True:
        now = int(time.time())
        rows = db.cur.execute("SELECT * FROM bridge_rules").fetchall()

        for r in rows:
            try:
                if now - r["last_post_ts"] < r["hours"] * 3600:
                    continue

                if r["messages"] is not None and r["message_counter"] < r["messages"]:
                    continue

                db.cur.execute(
                    """
                    UPDATE bridge_rules
                    SET last_post_ts=?, message_counter=0
                    WHERE bridge_id=?
                    """,
                    (now, r["bridge_id"])
                )
                db.conn.commit()

                chats = db.cur.execute(
                    "SELECT * FROM chats WHERE bridge_id=?",
                    (r["bridge_id"],)
                ).fetchall()

                for c in chats:
                    try:
                        if c["platform"] == "discord":
                            channel_id = int(c["chat_id"].split(":")[1])
                            channel = dc.get_channel(channel_id)
                            if not channel:
                                try:
                                    channel = await dc.fetch_channel(channel_id)
                                except Exception:
                                    channel = None
                            if channel:
                                await channel.send(r["content"])

                        elif c["platform"] == "telegram":
                            chat_id, thread = c["chat_id"].split(":")
                            await tg.send_message(
                                int(chat_id),
                                r["content"],
                                message_thread_id=int(thread) or None
                            )
                    except Exception as e:
                        await send_service_event("daily_loop_error", error=f"RULES SEND ERROR ({r['bridge_id']}->{c['chat_id']}): {e}")
            except Exception as e:
                await send_service_event("daily_loop_error", error=f"RULES LOOP ERROR: {e}")

        await asyncio.sleep(60)

async def pending_cleanup_loop():
    """
    Удаляет устаревшие pending_consents (старше 24ч): удаляет бот-сообщение и строку в БД.
    Также очищает verified_users, у которых истёк срок.
    """
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    while True:
        try:
            rows = db.get_expired_pending_consents(older_than_seconds=24*3600)
            for p in rows:
                platform = p["platform"]
                bot_msg_id = p["bot_message_id"]
                chat_key = p["chat_key"]
                prefix = p["prefix"]
                user_id = p["user_id"]
                first_msg_id = p["first_message_id"] if "first_message_id" in p.keys() else None

                if platform == "telegram":
                    chat_id_str, th = chat_key.split(":")
                    try:
                        if bot_msg_id:
                            await tg.delete_message(int(chat_id_str), int(bot_msg_id))
                    except Exception:
                        pass
                    try:
                        if first_msg_id and first_msg_id not in (None, "None", ""):
                            await tg.delete_message(int(chat_id_str), int(first_msg_id))
                    except Exception:
                        pass

                elif platform == "discord":
                    try:
                        guild_id, channel_id = chat_key.split(":")
                        ch = dc.get_channel(int(channel_id))
                        if ch:
                            if bot_msg_id:
                                try:
                                    msg = await ch.fetch_message(int(bot_msg_id))
                                    await msg.delete()
                                except Exception:
                                    pass
                            if first_msg_id and first_msg_id not in (None, "None", ""):
                                try:
                                    first_msg = await ch.fetch_message(int(first_msg_id))
                                    await first_msg.delete()
                                except Exception:
                                    pass
                    except Exception:
                        pass

                db.remove_pending_consent(platform, prefix, user_id)

            db.cleanup_expired_verified()
            db.cleanup_old_loc_suggestions()
            db.cleanup_old_polls()
            db.cleanup_wiki_relay_records()

        except Exception as e:
            try:
                await send_service_event("daily_loop_error", error=f"pending_cleanup_loop error: {e}")
            except Exception:
                pass

        await asyncio.sleep(60)

FEED_TICK_SECONDS = 30
FEED_MAX_POSTS_PER_CYCLE = 5

FEED_POLL_INTERVALS = {"telegram": 60, "bluesky": 120, "youtube": 5 * 60,
                       "wiki": 90, "wikidisc": 180}
FEED_DEFAULT_INTERVAL = 10 * 60
FEED_FETCHES_PER_KIND_PER_TICK = 1

FEED_THROTTLE_BACKOFF = 15 * 60
FEED_BACKOFF_START = 15 * 60
FEED_BACKOFF_MAX = 4 * 3600

_feed_backoff = {}
_feed_last_fetch = {}

def _feed_note_failure(key, throttled=False):
    """Put a source aside for a while after a failed fetch.

    A source that is broken or gone is backed off further on every failure, up
    to hours. Being throttled is different: the far end is working and will take
    us back shortly, so the wait stays flat — an escalating one would keep the
    feed silent long after the throttle lifted. A throttle is counted per host
    rather than per source, since that is how the far end counts it: asking it
    about a different account of the same site would only prolong the refusal."""
    if throttled:
        deadline = time.time() + FEED_THROTTLE_BACKOFF
        _feed_backoff[key] = (deadline, 0)
        _feed_backoff[(key[0], None)] = (deadline, 0)
        return FEED_THROTTLE_BACKOFF
    _, strikes = _feed_backoff.get(key, (0, 0))
    strikes += 1
    delay = min(FEED_BACKOFF_START * 2 ** (strikes - 1), FEED_BACKOFF_MAX)
    _feed_backoff[key] = (time.time() + delay, strikes)
    return delay

def _feeds_due(now):
    """The sources to ask on this tick: at most one per kind, the one waiting
    longest, and only if its kind's interval has passed since it was last read.

    Spreading the sources over the ticks instead of walking them all at once is
    what keeps a bridge with many sources of one kind from arriving at that
    kind's host as a burst."""
    candidates = {}
    for feed in db.get_all_feeds():
        if feed["live"]:
            continue
        key = (feed["kind"], feed["source"])
        until, _ = _feed_backoff.get(key, (0, 0))
        kind_until, _ = _feed_backoff.get((feed["kind"], None), (0, 0))
        if max(until, kind_until) > now:
            continue
        interval = FEED_POLL_INTERVALS.get(feed["kind"], FEED_DEFAULT_INTERVAL)
        if now - _feed_last_fetch.get(key, 0) < interval:
            continue
        candidates.setdefault(key, []).append(feed)

    due = []
    for kind in {k for k, _ in candidates}:
        of_kind = sorted((k for k in candidates if k[0] == kind),
                         key=lambda k: _feed_last_fetch.get(k, 0))
        for key in of_kind[:FEED_FETCHES_PER_KIND_PER_TICK]:
            due.append((key, candidates[key]))
    return due

async def feed_loop():
    """Poll every followed source and relay what is new.

    Covers the Bluesky accounts of `/setbskyfeed`, the YouTube channels of
    `/setytfeed`, the wikis of `/setwikifeed` and the Telegram channels of
    `/settgfeed` that the bot is not a member of — a channel it *is* in delivers
    its posts through `channel_post` and never appears here. A source is read
    once however many chats follow it, and each attachment advances its own
    `last_post_id`. A failing source is put aside for a while and reported to the
    service chats at most once an hour: an outage at the far end must turn into
    neither a flood of messages nor a flood of requests.

    A wiki hands its whole batch to `relay_wiki_posts` instead of being
    walked post by post: it is the one source that merges several changes
    into one message and filters per chat, so the cap on how many messages a
    poll may produce lives there rather than in the slice taken here."""
    import aiohttp
    from discord_bot import (
        bot as dc, feed_module, feed_stale_since, relay_feed_post, warm_feed_avatars,
    )
    from utils import rate_limit_ok, FeedError

    await dc.wait_until_ready()
    await warm_feed_avatars()
    while True:
        try:
            due = _feeds_due(time.time())
            if due:
                async with aiohttp.ClientSession() as session:
                    for key, rows in due:
                        kind, source = key
                        _feed_last_fetch[key] = time.time()
                        try:
                            title, posts = await feed_module(kind).fetch_posts(
                                source, session=session)
                        except FeedError as e:
                            delay = _feed_note_failure(
                                key, throttled=getattr(e, "throttled", False))
                            logger.warning("feed fetch failed (%s %s): %s — next try in %d min",
                                           kind, source, e, delay // 60)
                            if rate_limit_ok(("feed-error", kind, source), limit=1, window_seconds=3600):
                                await send_service_event("feed_error", account=source,
                                                         error=str(e))
                            continue
                        _feed_backoff.pop(key, None)

                        stale_since = feed_stale_since(posts, kind)
                        if stale_since and rate_limit_ok(("feed-stale", kind, source),
                                                         limit=1, window_seconds=86400):
                            logger.warning("feed %s %s has not moved since %s",
                                           kind, source, stale_since)
                            await send_service_event("feed_stale", account=source,
                                                     date=stale_since)

                        for feed in rows:
                            try:
                                last_id = feed["last_post_id"]
                                fresh = [p for p in posts
                                         if last_id is None or int(p["id"]) > int(last_id)]
                                if not fresh:
                                    if title and title != feed["title"]:
                                        db.set_feed_last_post(kind, source, feed["chat_id"],
                                                              last_id or "0", title=title)
                                    continue
                                if kind in ("wiki", "wikidisc"):
                                    from discord_bot.wiki import relay_wiki_posts
                                    await relay_wiki_posts(feed, fresh)
                                else:
                                    for post in fresh[-FEED_MAX_POSTS_PER_CYCLE:]:
                                        await relay_feed_post(feed, post)
                                db.set_feed_last_post(kind, source, feed["chat_id"],
                                                      fresh[-1]["id"], title=title)
                            except Exception as e:
                                logger.warning("feed relay failed (%s %s -> %s): %s",
                                               kind, source, feed["chat_id"], e)
        except Exception as e:
            try:
                await send_service_event("daily_loop_error", error=f"feed_loop error: {e}")
            except Exception:
                pass

        await asyncio.sleep(FEED_TICK_SECONDS)

async def poll_loop():
    """Posts results for expired polls to every bridge chat, then closes them."""
    from discord_bot import post_poll_results
    while True:
        try:
            for poll in db.get_expired_open_polls():
                try:
                    await post_poll_results(poll["id"])
                finally:
                    db.close_poll(poll["id"])
        except Exception as e:
            try:
                await send_service_event("daily_loop_error", error=f"poll_loop error: {e}")
            except Exception:
                pass
        await asyncio.sleep(30)

async def daily_check_loop():
    """
    Сразу при старте проверяет все чаты, затем спит 24 часа.
    Сообщения отправляет в service-чаты из config.SERVICE_CHATS.
    """
    from telegram_bot import bot as tg
    from discord_bot import bot as dc

    await dc.wait_until_ready()
    while True:
        try:
            rows = db.cur.execute("SELECT * FROM chats").fetchall()
            for r in rows:
                platform = r["platform"]
                chat_key = r["chat_id"]
                if platform == "discord" and chat_key.startswith("dm:"):
                    db.clear_chat_inaccessible(chat_key)
                    continue
                inaccessible = False
                if platform == "telegram":
                    try:
                        prefix = chat_key.split(":",1)[0]
                        try:
                            ch = await tg.get_chat(int(prefix))
                        except Exception:
                            await send_service_event("daily_missing_tg_chat", chat_key=chat_key)
                            inaccessible = True
                            ch = None
                        try:
                            if ch is not None:
                                me = await tg.get_me()
                                mem = await tg.get_chat_member(int(prefix), me.id)
                                can_delete = getattr(mem, "can_delete_messages", False)
                                if not can_delete:
                                    await send_service_event("daily_no_tg_delete_perm", chat_key=chat_key)
                        except Exception:
                            await send_service_event("daily_tg_perm_check_error", chat_key=chat_key, error="unknown")
                    except Exception:
                        continue

                elif platform == "discord":
                    try:
                        guild_id, channel_id = chat_key.split(":")
                        ch = dc.get_channel(int(channel_id))
                        if not ch:
                            try:
                                ch = await dc.fetch_channel(int(channel_id))
                            except Exception:
                                ch = None
                        if not ch:
                            await send_service_event("daily_missing_dc_channel", chat_key=chat_key)
                            inaccessible = True
                        else:
                            guild = getattr(ch, "guild", None)
                            me = guild.me if guild else None
                            if me is None and guild is not None:
                                try:
                                    me = await guild.fetch_member(dc.user.id)
                                except Exception:
                                    me = None
                            if me is not None:
                                perms = ch.permissions_for(me)
                                if not perms.manage_messages:
                                    await send_service_event("daily_no_dc_manage_perm", chat_key=chat_key)
                    except Exception:
                        continue

                if inaccessible:
                    fail_row = db.mark_chat_inaccessible(platform, chat_key)
                    if fail_row:
                        first_failed_ts = int(fail_row["first_failed_ts"])
                        if int(time.time()) - first_failed_ts >= 24 * 3600:
                            result = db.remove_chat_from_bridge(chat_key)
                            if result:
                                await send_service_event(
                                    "daily_auto_removed_chat",
                                    chat_key=chat_key,
                                    platform=platform.capitalize()
                                )
                                if result.get("bridge_deleted"):
                                    await send_service_event(
                                        "daily_auto_removed_bridge",
                                        bridge_id=result["bridge_id"]
                                    )
                    continue

                db.clear_chat_inaccessible(chat_key)
        except Exception as e:
            try:
                await send_service_event("daily_loop_error", error=str(e))
            except Exception:
                print("daily_check_loop exception:", e)

        await asyncio.sleep(24 * 3600)

async def main():
    """Start both bots and the cross-platform loops as one task group, then
    wait. The service chats are told once the bots have had five seconds to
    connect, and told again on the way out however the gather ends."""
    tasks = [
        asyncio.create_task(tg_main()),
        asyncio.create_task(discord_bot.start(DISCORD_TOKEN)),
        asyncio.create_task(rules_loop()),
        asyncio.create_task(pending_cleanup_loop()),
        asyncio.create_task(daily_check_loop()),
        asyncio.create_task(poll_loop()),
        asyncio.create_task(feed_loop()),
    ]

    await asyncio.sleep(5)
    await send_service_event("bot_started")

    try:
        await asyncio.gather(*tasks)
    finally:
        await send_service_event("bot_stopped")

if __name__ == "__main__":
    asyncio.run(main())