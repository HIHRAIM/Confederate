"""The Discord client: the DiscordBot class, its background loops, the one
`bot` instance, and the channel-based integration with Confederate Guard.

Everything else in the package imports `bot` from here, so this module must
not import its siblings at module level — sibling references inside the
class (poll views, appeal maintenance) are imported at the call site.
"""
import asyncio
import datetime
import io
import json
import logging
import time

import discord
from discord import app_commands

import db
from config import PURGATORIUM_GUILD_ID
from utils import get_next_status_text, get_chat_lang, localized_deadtopic, DEFAULT_LANG

logger = logging.getLogger("bridge.discord")

DEADTOPIC_DEFAULT_DAYS = 6

def thread_closed_by_hand(*, locked, archived, archived_at, window_minutes, last_activity):
    """Whether a Discord thread was closed by a person rather than by Discord.

    A lock is always a person's. An archive alone may be either: Discord
    archives a thread by itself once `window_minutes` have passed without a
    message, and a person may archive one at any moment. So an archive that
    came more than an hour before the window could have run out since the
    last message (`last_activity`) was a person's; anything later is taken
    for Discord's own. Both the keep-alive and the inbox go by it: neither
    may reopen a thread somebody closed on purpose."""
    if locked:
        return True
    if not archived or archived_at is None or last_activity is None:
        return False
    window = int(window_minutes or 10080) * 60
    return (archived_at - last_activity).total_seconds() < window - 3600

def thread_last_activity(thread):
    """When the last message in a thread was sent — read off the id of that
    message, so a phantom the keep-alive deleted still counts, as it does
    for Discord's own archive timer."""
    return discord.utils.snowflake_time(int(thread.last_message_id or thread.id))

def discord_thread_closed_by_hand(thread):
    """`thread_closed_by_hand` for a discord.Thread."""
    return thread_closed_by_hand(
        locked=bool(getattr(thread, "locked", False)),
        archived=bool(getattr(thread, "archived", False)),
        archived_at=getattr(thread, "archive_timestamp", None),
        window_minutes=getattr(thread, "auto_archive_duration", None),
        last_activity=thread_last_activity(thread),
    )

def bridged_thread_keepalive_due(thread, now):
    """Whether a bridged thread with no `/deadtopic` row of its own wants its
    phantom message tonight.

    One that Discord has already archived is restored. One still open gets
    the phantom when Discord would archive it before the next nightly pass —
    which, for the usual seven-day window, is after six quiet days, what
    `/deadtopic` itself does. A window shorter than three days cannot be
    beaten by a nightly pass at all, so such a thread is only restored once
    archived. A thread a person locked or archived is left as they left it."""
    if discord_thread_closed_by_hand(thread):
        return False
    if thread.archived:
        return True
    window = int(thread.auto_archive_duration or 10080) * 60
    if window < 3 * 86400:
        return False
    return (now - thread_last_activity(thread)).total_seconds() >= window - 86400

class DiscordBot(discord.Client):
    """The Discord half of the bridge.

    Uses a plain Client + CommandTree rather than commands.Bot: every command
    is a slash command, there are no text prefixes. The privileged intents
    (message_content, members, presences) are all genuinely needed — relaying,
    member resolution and whois status respectively."""

    def __init__(self):
        """Build the client with the intents the relay needs."""
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        """Sync the command tree, start the client-side loops and re-register
        the persistent views (poll vote buttons, appeal verdict buttons) that
        must keep answering clicks after a restart."""
        from discord_bot.commands.polls import PollView
        from discord_bot.appeals import AppealVerdictView

        await self.tree.sync()
        self.loop.create_task(self.deadchat_loop())
        self.loop.create_task(self.status_loop())
        self.loop.create_task(self.bridge_rules_loop())
        self.loop.create_task(self.deadtopic_loop())
        self.loop.create_task(self.backup_loop())
        self.loop.create_task(self.appeal_maintenance_loop())
        try:
            for poll in db.get_open_polls():
                self.add_view(PollView(poll["id"], json.loads(poll["options"])))
        except Exception:
            pass
        try:
            for row in db.get_open_appeals():
                lang = get_chat_lang(f"{PURGATORIUM_GUILD_ID}:{row['thread_id']}") or DEFAULT_LANG
                self.add_view(AppealVerdictView(int(row["user_id"]), lang))
        except Exception:
            pass

    async def appeal_maintenance_loop(self):
        """Daily wrapper around discord_bot.appeals._appeal_maintenance_pass
        (Purgatorium auto-kicks and appeal garbage collection)."""
        from discord_bot.appeals import _appeal_maintenance_pass

        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await _appeal_maintenance_pass(self)
            except Exception as e:
                logger.warning("appeal maintenance failed: %s", e)
            await asyncio.sleep(24 * 3600)

    async def backup_loop(self):
        """Every 12 hours, send the encrypted database snapshot to the backup
        chats on both platforms. Sleeps first: a crash-looping bot must not
        spam backups on every restart."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(12 * 3600)
            await _send_db_backup_discord(self)
            await _send_db_backup_telegram()

    async def deadchat_loop(self):
        """Every 5 minutes, ping the configured role in /deadchat chats whose
        silence exceeded their threshold, and restart their clock."""
        await self.wait_until_ready()
        while not self.is_closed():
            now = int(time.time())
            rows = db.cur.execute("SELECT * FROM dead_chats").fetchall()

            for r in rows:
                timeout = r["hours"] * 3600
                if now - r["last_message_ts"] >= timeout:
                    try:
                        guild_id, channel_id = r["chat_id"].split(":")
                        channel = self.get_channel(int(channel_id))
                        if channel:
                            await channel.send(f"<@&{r['role_id']}>")
                    except Exception:
                        pass

                    db.cur.execute(
                        "UPDATE dead_chats SET last_message_ts=? WHERE chat_id=?",
                        (now, r["chat_id"])
                    )
                    db.conn.commit()

            await asyncio.sleep(300)

    async def status_loop(self):
        """Every minute, rotate the presence text to the next of the six
        languages, with live totals of members and communities across both
        platforms (Telegram counts come from the Bot API per group)."""
        await self.wait_until_ready()
        from telegram_bot import bot as tg_bot
        while not self.is_closed():
            try:
                discord_members = sum((g.member_count or 0) for g in self.guilds)
                telegram_members = 0
                for gid in db.get_telegram_group_ids():
                    try:
                        members_count = await tg_bot.get_chat_member_count(int(gid))
                        telegram_members += int(members_count or 0)
                    except Exception:
                        continue
                total_members = discord_members + telegram_members
                discord_servers = len(self.guilds)
                telegram_groups = db.get_telegram_group_count()
                total_servers = discord_servers + telegram_groups

                status_text = get_next_status_text(total_members, total_servers)

                await self.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=status_text
                    )
                )
            except Exception as e:
                print(f"Status update error: {e}")

            await asyncio.sleep(60)

    async def bridge_rules_loop(self):
        """Periodically posts bridge rules to ALL chats in the bridge (Discord + Telegram).

        A bridge posts when BOTH thresholds pass: the interval (the
        bridge_rules.hours column — which actually stores minutes) and the
        message count since the last post, when one is set."""
        await self.wait_until_ready()
        from telegram_bot import bot as tg_bot

        while not self.is_closed():
            try:
                now = int(time.time())
                rows = db.cur.execute("SELECT * FROM bridge_rules").fetchall()

                for r in rows:
                    interval_minutes = r["hours"]
                    if not interval_minutes or interval_minutes <= 0:
                        continue

                    interval_seconds = interval_minutes * 60
                    elapsed_since_post = now - (r["last_post_ts"] or 0)

                    time_due = elapsed_since_post >= interval_seconds

                    msg_count = r["messages"]
                    count_due = (msg_count is None) or (r["message_counter"] or 0) >= msg_count

                    if not (time_due and count_due):
                        continue

                    content = r["content"] or ""
                    if not content:
                        continue

                    bridge_id = r["bridge_id"]
                    chats = db.get_bridge_chats(bridge_id)

                    for chat in chats:
                        try:
                            if chat["platform"] == "discord":
                                channel_id = int(chat["chat_id"].split(":")[1])
                                channel = self.get_channel(channel_id)
                                if not channel:
                                    try:
                                        channel = await self.fetch_channel(channel_id)
                                    except Exception:
                                        continue
                                await channel.send(content)

                            elif chat["platform"] == "telegram":
                                chat_id_str, thread = chat["chat_id"].split(":")
                                await tg_bot.send_message(
                                    chat_id=int(chat_id_str),
                                    message_thread_id=int(thread) or None,
                                    text=content,
                                )
                        except Exception as e:
                            print(f"bridge_rules_loop: failed to send to {chat['chat_id']}: {e}")

                    db.cur.execute(
                        "UPDATE bridge_rules SET last_post_ts=?, message_counter=0 WHERE bridge_id=?",
                        (now, bridge_id)
                    )
                    db.conn.commit()

            except Exception as e:
                print(f"bridge_rules_loop error: {e}")

            await asyncio.sleep(60)

    async def deadtopic_loop(self):
        """Run `deadtopic_pass` at every 00:00 UTC. Sleeping until the next
        midnight — rather than a fixed interval — makes the schedule survive
        restarts."""
        await self.wait_until_ready()
        while not self.is_closed():
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            next_midnight = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_seconds = (next_midnight - now_utc).total_seconds()
            await asyncio.sleep(sleep_seconds)

            try:
                await self.deadtopic_pass()
            except Exception as e:
                print(f"deadtopic_loop error: {e}")

    async def _resolve_deadtopic_channel(self, chat_id):
        """The channel or thread behind a ``<guild>:<channel>`` key — from the
        cache, or asked for: an archived thread is no longer cached."""
        guild_part, _, channel_part = str(chat_id).partition(":")
        if not (guild_part.isdigit() and channel_part.isdigit()):
            return None
        channel = self.get_channel(int(channel_part))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_part))
            except Exception:
                return None
        return channel

    async def _send_phantom(self, chat_id, channel):
        """Send the phantom message and delete it at once. Sending into a
        thread Discord archived is what unarchives it."""
        lang = get_chat_lang(chat_id) or "en"
        sent = await channel.send(localized_deadtopic("phantom_message", lang))
        await asyncio.sleep(1)
        try:
            await sent.delete()
        except Exception:
            pass

    async def deadtopic_pass(self):
        """Keep threads and forum posts from staying archived for silence.

        Two kinds of chat are looked after. The ones with a `deadtopic_chats`
        row follow it: once the row's own window has passed since the last
        message (or the bot's own last phantom), a phantom message is sent
        and deleted right away — 6 days for the ones `/deadtopic enable`
        sets, 3 for inbox conversation threads, which register themselves
        and cannot afford to be archived while somebody waits for an answer.
        Every other Discord thread and forum post that belongs to a bridge
        is kept alive by default (`bridged_thread_keepalive_due`): restored
        when Discord has archived it, and given its phantom before Discord
        would. `/deadtopic disable` writes `disabled=1`, which takes a chat
        out of both.

        Neither kind ever reopens a thread a person locked or archived by
        hand (`discord_thread_closed_by_hand`) — keeping a conversation
        alive is not overruling the people who closed it."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_ts = int(time.time())
        today_str = now_utc.strftime("%Y-%m-%d")
        rows = db.cur.execute("SELECT * FROM deadtopic_chats").fetchall()

        for r in rows:
            try:
                chat_id = r["chat_id"]
                if r["disabled"]:
                    continue

                bot_last_ts = r["bot_last_sent_ts"] or 0
                if bot_last_ts > 0:
                    last_sent_day = datetime.datetime.fromtimestamp(
                        bot_last_ts, tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
                    if last_sent_day == today_str:
                        continue

                last_msg_ts = r["last_message_ts"] or 0
                ref_ts = max(last_msg_ts, bot_last_ts)

                if now_ts - ref_ts < (r["days"] or DEADTOPIC_DEFAULT_DAYS) * 86400:
                    continue

                channel = await self._resolve_deadtopic_channel(chat_id)
                if channel is None:
                    continue
                if isinstance(channel, discord.Thread) and discord_thread_closed_by_hand(channel):
                    continue

                await self._send_phantom(chat_id, channel)

                db.cur.execute(
                    "UPDATE deadtopic_chats SET bot_last_sent_ts=? WHERE chat_id=?",
                    (now_ts, chat_id)
                )
                db.conn.commit()

            except Exception as e:
                print(f"deadtopic_loop: error for {r['chat_id']}: {e}")

        configured = {r["chat_id"] for r in rows}
        bridged = db.cur.execute(
            "SELECT chat_id FROM chats WHERE platform='discord'"
        ).fetchall()
        for row in bridged:
            chat_id = row["chat_id"]
            if chat_id in configured:
                continue
            try:
                channel = await self._resolve_deadtopic_channel(chat_id)
                if not isinstance(channel, discord.Thread):
                    continue
                if bridged_thread_keepalive_due(channel, now_utc):
                    await self._send_phantom(chat_id, channel)
            except Exception as e:
                print(f"deadtopic_loop: error for {chat_id}: {e}")

bot = DiscordBot()

async def _send_db_backup_discord(client):
    """Send the encrypted bridge.db snapshot to every Discord backup channel
    (config.BACKUP_CHATS). Failures are logged, never raised — a broken backup
    channel must not take the loop down."""
    from config import BACKUP_CHATS
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
        return
    fname = encrypted_filename("bridge.db")
    for channel_id in BACKUP_CHATS.get("discord", set()):
        try:
            ch = client.get_channel(channel_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(channel_id)
                except Exception:
                    logger.warning("Periodic backup: cannot fetch Discord channel %s", channel_id)
                    continue
            if ch:
                await ch.send(file=discord.File(io.BytesIO(data), filename=fname))
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Discord channel %s: %s", channel_id, e)

async def _send_db_backup_telegram():
    """Send the encrypted bridge.db snapshot to every Telegram backup chat.
    Lives on the Discord side only because backup_loop does; the send itself
    goes through the aiogram bot."""
    from config import BACKUP_CHATS
    from telegram_bot import bot as tg_bot
    from backup_crypto import build_encrypted_backup, encrypted_filename
    try:
        data = build_encrypted_backup("bridge.db")
    except Exception as e:
        logger.warning("Periodic backup failed to build: %s", e)
        return
    fname = encrypted_filename("bridge.db")
    for chat_entry in BACKUP_CHATS.get("telegram", set()):
        try:
            chat_id_str, thread_str = chat_entry.split(":")
            from aiogram.types import BufferedInputFile
            doc = BufferedInputFile(data, filename=fname)
            await tg_bot.send_document(
                chat_id=int(chat_id_str),
                document=doc,
                message_thread_id=int(thread_str) or None,
            )
        except Exception as e:
            logger.warning("Periodic backup: failed to send to Telegram chat %s: %s", chat_entry, e)

async def _post_user_id_to_channels(channel_ids, payload):
    """Post a verification-sync payload to each of the given Discord channels.

    Used to publish verification state changes to the VERIFIED / UNVERIFIED
    channels so Confederate Guard can mirror them into its cross-server database.
    """
    for cid in channel_ids:
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        try:
            await channel.send(str(payload))
        except Exception as e:
            logger.warning("Failed to publish user id %s to channel %s: %s", payload, cid, e)

async def announce_verified_user(user_id, guild_id=None):
    """Publish a newly verified user's ID to the VERIFIED channel(s).

    The line is the user's ID, followed by the ID of the server where the
    consent was given when that is known. Confederate Guard needs the second
    ID to tell the server the user verified on (plain 'verified' notice) from
    the other servers it hands the role out on ('due to verification on another
    server'); without it, every server was told the verification had happened
    somewhere else. Guard reads the first ID and treats a missing second one as
    an unknown origin, so a bare ID stays a valid message.

    Only useful when running alongside Confederate Guard; can be turned off
    with /verify-list disable.
    """
    if not db.is_verify_list_enabled():
        return
    from config import VERIFIED
    payload = f"{user_id} {guild_id}" if guild_id is not None else str(user_id)
    await _post_user_id_to_channels(VERIFIED, payload)

async def announce_unverified_user(user_id):
    """Publish an unverified user's ID to the UNVERIFIED channel(s).

    Only useful when running alongside Confederate Guard; can be turned off
    with /verify-list disable.
    """
    if not db.is_verify_list_enabled():
        return
    from config import UNVERIFIED
    await _post_user_id_to_channels(UNVERIFIED, user_id)
