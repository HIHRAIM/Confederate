"""Every @bot.event handler: the inbound side of the Discord half.

on_message is the front door of the whole relay — its filter ladder decides
what gets bridged at all (see its docstring). The edit/delete events come in
both cached and raw flavours because discord.py only fires the cached ones
for messages it has seen since start-up; the raw ones cover everything else.
The actual propagation work lives in discord_bot/relay.py — handlers here
only route.
"""
import json
import logging
import time

import discord

import db
from config import GUARD_BOT_ID
from utils import (
    get_chat_lang, localized, localized_consent_body, localized_consent_button,
    localized_consent_title, rate_limit_ok, send_service_event,
)

from discord_bot.client import bot, announce_verified_user
from discord_bot.appeals import (
    _cleanup_appeal_records, handle_appeal_dm_message, handle_appeal_thread_message,
)
from discord_bot.mentions import _discord_system_event_key, replace_mentions
from discord_bot.relay import (
    _relay_pending_discord_first_message, _relay_verified_discord_message,
    is_own_relay_webhook_message, process_appeal_dm_edit,
    process_discord_message_delete, process_discord_message_edit,
)

logger = logging.getLogger("bridge.discord")

@bot.event
async def on_message(message: discord.Message):
    """The relay's front door. The ladder, in order: DMs go to the appeal
    system; news-chat reactions and dead-chat bookkeeping run for everyone;
    bot senders pass only under their special rules (own messages and own
    webhook copies never, Confederate Guard's appeal-thread posts get pinned,
    /allow-bots chats relay foreign bots); appeal threads and inbox
    conversation threads take their detours; shadow-banned users are silently
    deleted; unverified users get the consent prompt and their message is
    held; finally the rate limit, and the relay itself."""
    if message.guild is None:
        try:
            await handle_appeal_dm_message(message)
        except Exception as e:
            logger.warning("appeal DM relay failed (user=%s): %s", message.author.id, e)
        return

    chat_id = f"{message.guild.id}:{message.channel.id}"

    row_news = db.cur.execute(
        "SELECT emojis FROM news_chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

    if row_news:
        emojis = json.loads(row_news["emojis"])
        for e in emojis:
            try:
                await message.add_reaction(e)
            except Exception:
                pass

    db.cur.execute(
        "UPDATE dead_chats SET last_message_ts=? WHERE chat_id=?",
        (int(time.time()), chat_id)
    )
    db.conn.commit()

    if message.author.bot:
        if message.author == bot.user:
            return
        if is_own_relay_webhook_message(message):
            return
        if message.author.id == GUARD_BOT_ID and db.get_appeal_by_thread(str(message.channel.id)):
            try:
                await message.pin()
            except Exception:
                pass
            return
        if db.get_allow_bots(chat_id) and not db.is_relay_copy("discord", chat_id, str(message.id)):
            row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if row:
                if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
                    logger.warning("Rate limit: dropping relay from discord bot %s in %s", message.author.id, chat_id)
                    return
                await _relay_verified_discord_message(message, row["bridge_id"], is_bot_sender=True)
        return

    db.cur.execute(
        "UPDATE deadtopic_chats SET last_message_ts=? WHERE chat_id=?",
        (int(time.time()), chat_id)
    )
    db.conn.commit()

    system_event_key = _discord_system_event_key(message)

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        return

    bridge_id = row["bridge_id"]

    appeal_row = db.get_appeal_by_thread(str(message.channel.id))
    if appeal_row:
        if not _discord_system_event_key(message):
            try:
                await handle_appeal_thread_message(message, appeal_row, bridge_id)
            except Exception as e:
                logger.warning("appeal thread relay failed (thread=%s): %s", message.channel.id, e)
        return

    if db.is_inbox_bridge(bridge_id):
        if not _discord_system_event_key(message):
            try:
                from inbox import handle_inbox_host_discord_message
                await handle_inbox_host_discord_message(message, bridge_id)
            except Exception as e:
                logger.warning("inbox thread relay failed (thread=%s): %s", message.channel.id, e)
        return

    db.cur.execute(
        "UPDATE bridge_rules SET message_counter = COALESCE(message_counter, 0) + 1 WHERE bridge_id=?",
        (bridge_id,)
    )
    db.conn.commit()

    prefix = str(message.guild.id)
    user_id_str = str(message.author.id)

    if db.is_shadow_banned("discord", user_id_str):
        try:
            await message.delete()
        except Exception as e:
            logger.warning(
                "Failed to delete shadow-banned message (user=%s, chat=%s): %s",
                user_id_str, chat_id, e
            )
        return

    if system_event_key and not db.is_user_verified("discord", user_id_str, prefix):
        await _relay_verified_discord_message(message, bridge_id, system_event_key)
        return

    if not db.is_user_verified("discord", user_id_str, prefix):
        pend = db.get_pending_consent("discord", prefix, user_id_str)
        if pend:
            try:
                await message.delete()
            except Exception:
                pass
            return
        else:
            lang = get_chat_lang(chat_id) or "en"
            from discord import ui, ButtonStyle

            class _VerifyView(ui.View):
                """Consent button under the prompt. Not persistent: an expired
                prompt is simply re-issued on the user's next message."""

                def __init__(self, prefix, user_id):
                    """Remember whose consent this button collects."""
                    super().__init__(timeout=None)
                    self.prefix = str(prefix)
                    self.user_id = str(user_id)

                @ui.button(label=localized_consent_button(lang), style=ButtonStyle.primary)
                async def accept(self, interaction: discord.Interaction, button: ui.Button):
                    """Record consent, clear every community's pending prompt,
                    announce to Confederate Guard, then relay the held first messages."""
                    if str(interaction.user.id) != str(self.user_id):
                        await interaction.response.send_message(localized("verify_button_not_yours", lang), ephemeral=True)
                        return
                    db.add_verified_user("discord", self.user_id, self.prefix, days_valid=365)
                    all_pendings = db.get_all_pending_consents_for_user("discord", self.user_id)
                    for p in all_pendings:
                        db.remove_pending_consent("discord", p["prefix"], self.user_id)
                        p_bot_msg_id = p["bot_message_id"]
                        if p_bot_msg_id:
                            try:
                                p_guild_id, p_channel_id = p["chat_key"].split(":")
                                p_ch = bot.get_channel(int(p_channel_id))
                                if p_ch:
                                    try:
                                        p_msg = await p_ch.fetch_message(int(p_bot_msg_id))
                                        await p_msg.delete()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass
                    await interaction.response.send_message(localized("verify_thanks", lang), ephemeral=True)
                    await announce_verified_user(self.user_id, interaction.guild_id)
                    for p in all_pendings:
                        await _relay_pending_discord_first_message(p)

            try:
                mention = f"<@{message.author.id}>"
                consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
                sent = await message.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
                bot_msg_id = str(sent.id)
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    bot_msg_id,
                    chat_key,
                    first_message_id=str(message.id)
                )
            except Exception:
                chat_key = f"{message.guild.id}:{message.channel.id}"
                db.add_pending_consent(
                    "discord",
                    prefix,
                    user_id_str,
                    "",
                    chat_key,
                    first_message_id=str(message.id)
                )
            return

    if not rate_limit_ok(("relay", "discord", user_id_str), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping relay from discord user %s in %s", user_id_str, chat_id)
        return

    await _relay_verified_discord_message(message, bridge_id, system_event_key)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Cached-message edit: guild edits propagate to the copies, DM edits go
    to the appeal-thread copy. Bot edits are ignored (copies are the bot's
    own messages — reacting to them would loop)."""
    if after.author.bot:
        return
    if after.guild is None:
        await process_appeal_dm_edit(after.author.id, after.id,
                                     after.author.display_name or str(after.author),
                                     after.content or "")
        return
    await process_discord_message_edit(
        guild=after.guild,
        channel=after.channel,
        message_id=after.id,
        author_display_name=after.author.display_name or str(after.author),
        text=replace_mentions(after, after.content or ""),
    )

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """Raw edit event — covers messages not in the client cache (sent before
    start-up). Re-fetches the message when possible for the resolved author
    name and mention-free text; otherwise falls back to the raw payload
    fields. DM payloads route to the appeal edit path."""
    data = payload.data or {}
    if str(data.get("author", {}).get("bot", "")).lower() == "true":
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            channel = None

    if not guild and channel and getattr(channel, "guild", None):
        guild = channel.guild

    if not guild or not channel:
        author_data = data.get("author") or {}
        author_id = author_data.get("id")
        if isinstance(channel, discord.DMChannel) and author_id:
            name = author_data.get("global_name") or author_data.get("username") or "Unknown"
            try:
                await process_appeal_dm_edit(int(author_id), payload.message_id, name, data.get("content") or "")
            except Exception as e:
                logger.warning("appeal DM edit relay failed (user=%s): %s", author_id, e)
        return

    author_display_name = None
    content = data.get("content")
    try:
        msg = await channel.fetch_message(payload.message_id)
        if msg.author.bot:
            return
        author_display_name = msg.author.display_name or str(msg.author)
        content = msg.content
        content = replace_mentions(msg, content or "")
    except Exception:
        pass

    if author_display_name is None:
        author_data = data.get("author") or {}
        author_display_name = author_data.get("global_name") or author_data.get("username") or "Unknown"
        content = content or ""

    await process_discord_message_edit(
        guild=guild,
        channel=channel,
        message_id=payload.message_id,
        author_display_name=author_display_name,
        text=content,
    )

@bot.event
async def on_message_delete(message: discord.Message):
    """Cached-message delete → the shared delete propagation."""
    await process_discord_message_delete(
        guild_id=message.guild.id if message.guild else None,
        channel_id=message.channel.id if message.channel else None,
        message_id=message.id,
    )

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """Raw delete event — same propagation, for messages outside the cache.
    Both events firing for one deletion is fine: the second pass finds no
    rows and does nothing."""
    await process_discord_message_delete(
        guild_id=payload.guild_id,
        channel_id=payload.channel_id,
        message_id=payload.message_id,
    )

@bot.event
async def on_guild_join(guild: discord.Guild):
    """The bot was added to a server: record the join and tell the service
    chats, since a Bot Admin now has seven days to attach something of it to
    a bridge (setup_deadline.py).

    The row is a record, not the clock — the sweep reads Discord's own
    `Guild.me.joined_at`, so a missed event costs nothing but this notice."""
    db.record_join("discord", guild.id)
    await send_service_event(
        "joined_chat",
        platform="Discord",
        chat=guild.name or str(guild.id),
        chat_id=guild.id,
    )

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """The bot was kicked from (or left) a server: drop that server's per-chat
    configuration. Deliberately does NOT touch the chats table — the daily
    reachability sweep handles bridge detachment with its grace period, so a
    kick-and-reinvite within a day costs the server nothing.

    The setup-deadline row does go, so that a later re-invitation is a fresh
    seven days rather than a settlement inherited from the last time."""
    db.forget_deadline("discord", guild.id)

    db.cur.execute(
        """
        DELETE FROM chat_admins
        WHERE platform='discord' AND chat_id LIKE ?
        """,
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        """
        DELETE FROM dead_chats
        WHERE chat_id LIKE ?
        """,
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        "DELETE FROM news_chats WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        "DELETE FROM chat_settings WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.cur.execute(
        "DELETE FROM deadtopic_chats WHERE chat_id LIKE ?",
        (f"{guild.id}:%",)
    )

    db.conn.commit()

@bot.event
async def on_thread_delete(thread):
    """Deleting an appeal thread abandons the appeal: its records and bridge
    attachments go with it."""
    row = db.get_appeal_by_thread(str(thread.id))
    if row:
        _cleanup_appeal_records(row)
