"""Chat- and bridge-configuration commands: language (/lang, /locallang),
relay switches (/allow-bots, /allow-files, /webhooks, /verify-list), the
rules reminder (/remindrules) and the channel-keeping utilities (/deadchat,
/newschat, /deadtopic).

The loops these commands configure run in discord_bot/client.py; the
storage is db/settings.py (and the dead_chats/news_chats/deadtopic_chats/
bridge_rules tables).
"""
import json
import time

import discord
from discord import app_commands

import db
from utils import (
    SUPPORTED_LANGS, get_chat_lang, is_admin, is_chat_admin, localized,
    localized_deadtopic, set_chat_lang,
)

from discord_bot.client import bot

@bot.tree.command(name="deadchat", description="ping a role when chat is inactive (Discord only)")
async def deadchat(
    interaction: discord.Interaction,
    role_id: str,
    hours: int | None = None
):
    """Configure the dead-chat ping: after `hours` of silence the bot pings
    `role_id` in this channel (and repeats every further `hours`). 'disable'
    as the role turns it off. Chat admins and up."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            localized("no_permission", lang),
            ephemeral=True
        )
        return

    if role_id.lower() == "disable":
        db.cur.execute(
            "DELETE FROM dead_chats WHERE chat_id=?",
            (chat_id,)
        )
        db.conn.commit()
        await interaction.response.send_message(
            localized("deadchat_disabled", lang),
            ephemeral=True
        )
        return

    if not role_id.isdigit():
        await interaction.response.send_message(
            localized("deadchat_invalid_role", lang),
            ephemeral=True
        )
        return

    if hours is None or hours <= 0:
        await interaction.response.send_message(
            localized("deadchat_invalid_hours", lang),
            ephemeral=True
        )
        return

    db.cur.execute(
        """
        INSERT OR REPLACE INTO dead_chats
        (chat_id, role_id, hours, last_message_ts)
        VALUES (?,?,?,?)
        """,
        (
            chat_id,
            role_id,
            hours,
            int(time.time())
        )
    )
    db.conn.commit()

    await interaction.response.send_message(
        localized("deadchat_set", lang, role_id=role_id, hours=hours),
        ephemeral=True
    )

@bot.tree.command(name="newschat", description="auto-react to messages in a news channel (Discord only)")
async def newschat(
    interaction: discord.Interaction,
    action: str,
    emoji: str | None = None
):
    """/newschat add <emoji> — react with the emoji (unicode or <:name:id>)
    on every message in this channel; /newschat disable — stop. The emoji is
    validated by actually reacting to a throwaway message: that is the only
    reliable test that the bot can use it here."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"

    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(
            localized("no_permission", lang),
            ephemeral=True
        )
        return

    if action.lower() == "disable":
        db.cur.execute(
            "DELETE FROM news_chats WHERE chat_id=?",
            (chat_id,)
        )
        db.conn.commit()

        await interaction.response.send_message(
            localized("newschat_disabled", lang),
            ephemeral=True
        )
        return

    if action.lower() == "add":
        if emoji is None or emoji.strip() == "":
            await interaction.response.send_message(
                localized("newschat_specify_emoji", lang),
                ephemeral=True
            )
            return

        emoji_str = emoji.strip()

        try:
            test_msg = await interaction.channel.send("​")
            await test_msg.add_reaction(emoji_str)
            await test_msg.delete()
        except Exception:
            await interaction.response.send_message(
                localized("newschat_bad_emoji", lang),
                ephemeral=True
            )
            return

        row = db.cur.execute(
            "SELECT emojis FROM news_chats WHERE chat_id=?",
            (chat_id,)
        ).fetchone()

        emojis = json.loads(row["emojis"]) if row and row["emojis"] else []

        if emoji_str not in emojis:
            emojis.append(emoji_str)

        db.cur.execute(
            "INSERT OR REPLACE INTO news_chats (chat_id, emojis) VALUES (?,?)",
            (chat_id, json.dumps(emojis))
        )
        db.conn.commit()

        await interaction.response.send_message(
            localized("newschat_added", lang, emoji=emoji_str),
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        localized("newschat_usage", lang),
        ephemeral=True
    )

@bot.tree.command(name="deadtopic", description="send a phantom message every 6 days of inactivity to keep the topic alive")
async def deadtopic(
    interaction: discord.Interaction,
    action: str,
):
    """/deadtopic enable|disable — keep this chat from being archived for
    inactivity by send-and-deleting a phantom message every 6 quiet days
    (the loop in client.py). Bridge Admins and Bot Admins only."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    user_id = interaction.user.id

    allowed = is_admin("discord", user_id)
    if not allowed:
        row = db.cur.execute(
            "SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if row:
            bridge_admins = db.get_bridge_admins(row["bridge_id"])
            if str(user_id) in bridge_admins:
                allowed = True

    lang = get_chat_lang(chat_id) or "en"
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    action = action.strip().lower()

    if action == "disable":
        db.cur.execute("DELETE FROM deadtopic_chats WHERE chat_id=?", (chat_id,))
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("disabled", lang), ephemeral=True
        )
        return

    if action == "enable":
        now_ts = int(time.time())
        db.cur.execute(
            """
            INSERT INTO deadtopic_chats (chat_id, last_message_ts, bot_last_sent_ts)
            VALUES (?, ?, NULL)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_message_ts=excluded.last_message_ts,
                bot_last_sent_ts=NULL
            """,
            (chat_id, now_ts)
        )
        db.conn.commit()
        await interaction.response.send_message(
            localized_deadtopic("enabled", lang), ephemeral=True
        )
        return

    await interaction.response.send_message(
        localized_deadtopic("usage", lang),
        ephemeral=True
    )

@bot.tree.command(name="remindrules", description="periodically post rules to all bridge chats (e.g.: 2h, 30m)")
async def remindrules(
    interaction: discord.Interaction,
    hours_or_disable: str,
    messages: int | None = None,
    message_id: str | None = None,
    text: str | None = None,
):
    """Configure the bridge-wide rules reminder: an interval ('2h'/'30m'/bare
    hours; stored in minutes despite the column name), an optional minimum
    message count between posts, and the content — either `text` or a
    `message_id` to copy from this channel. last_post_ts is backdated one
    interval so the first post comes soon rather than a full interval away.
    'disable' removes the reminder."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_id, interaction.user.id)
    ):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    if not row:
        await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
        return

    bridge_id = row["bridge_id"]

    if hours_or_disable.strip().lower() == "disable":
        db.cur.execute("DELETE FROM bridge_rules WHERE bridge_id=?", (bridge_id,))
        db.conn.commit()
        await interaction.response.send_message(localized("remindrules_disabled", lang), ephemeral=True)
        return

    raw = hours_or_disable.strip().lower()
    try:
        if raw.endswith("h"):
            interval_minutes = int(raw[:-1]) * 60
        elif raw.endswith("m"):
            interval_minutes = int(raw[:-1])
        else:
            interval_minutes = int(raw) * 60
        if interval_minutes <= 0:
            raise ValueError
    except ValueError:
        await interaction.response.send_message(
            localized("remindrules_usage_discord", lang),
            ephemeral=True,
        )
        return

    content = (text or "").strip()
    source_message_id = ""

    if not content and message_id:
        try:
            ref_msg = await interaction.channel.fetch_message(int(message_id))
            content = (getattr(ref_msg, "content", "") or "").strip()
            source_message_id = str(ref_msg.id)
        except Exception:
            await interaction.response.send_message(localized("remindrules_fetch_failed", lang), ephemeral=True)
            return

    if not content:
        await interaction.response.send_message(
            localized("remindrules_no_content", lang),
            ephemeral=True,
        )
        return

    db.cur.execute(
        """
        INSERT OR REPLACE INTO bridge_rules
        (bridge_id, content, format, origin_platform, origin_chat_id,
         origin_message_id, hours, messages, last_post_ts, message_counter)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            bridge_id,
            content,
            "discord",
            "discord",
            chat_id,
            source_message_id,
            interval_minutes,
            messages,
            int(time.time()) - (interval_minutes * 60),
            0
        )
    )
    db.conn.commit()

    human = f"{interval_minutes // 60}h {interval_minutes % 60}m".replace("0h ", "").replace(" 0m", "").strip()
    await interaction.response.send_message(
        localized("remindrules_saved", lang, interval=human),
        ephemeral=True
    )

@bot.tree.command(name="locallang", description="set bot language for this channel/thread (ru, uk, pl, en, es, pt)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
async def locallang_command(interaction: discord.Interaction, code: str):
    """Set the bot language for this one channel/thread (overrides the
    server-wide /lang). Chat admins and up."""
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    if not (
        is_admin("discord", interaction.user.id)
        or is_chat_admin("discord", chat_key, interaction.user.id)
    ):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    code = code.strip().lower()
    try:
        set_chat_lang(chat_key, code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set", code, code=code), ephemeral=True)

@bot.tree.command(name="lang", description="set the default bot language for the whole server (bridge admins)")
@app_commands.describe(code="Language code (ru, uk, pl, en, es, pt)")
async def lang_command(interaction: discord.Interaction, code: str):
    """Set the server-wide default language (stored under the bare guild id;
    /locallang beats it per channel). Bridge Admins and Bot Admins only."""
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    allowed = is_admin("discord", interaction.user.id)
    if not allowed:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row and str(interaction.user.id) in db.get_bridge_admins(row["bridge_id"]):
            allowed = True
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    code = code.strip().lower()
    try:
        set_chat_lang(str(interaction.guild_id), code)
    except Exception:
        await interaction.response.send_message(
            localized("loc_unknown_lang", lang, lang=code, supported=", ".join(sorted(SUPPORTED_LANGS))),
            ephemeral=True
        )
        return

    await interaction.response.send_message(localized("lang_set_server", code, code=code), ephemeral=True)

@bot.tree.command(name="allow-bots", description="allow or block relay of bot messages")
async def allow_bots_command(interaction: discord.Interaction, action: str):
    """Toggle relaying of other bots' messages for this chat (default off —
    see db/settings.py: get_allow_bots). Chat admins and up."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_allow_bots(chat_id, True)
        await interaction.response.send_message(localized("allow_bots_enabled", lang), ephemeral=True)
    elif action == "disable":
        db.set_allow_bots(chat_id, False)
        await interaction.response.send_message(localized("allow_bots_disabled", lang), ephemeral=True)
    else:
        await interaction.response.send_message(localized("allow_bots_usage", lang), ephemeral=True)

@bot.tree.command(name="allow-files", description="allow re-uploading Telegram files to Discord and sharing their links")
@app_commands.describe(action="enable or disable", scope="local — this bridge only (default: this whole server)")
async def allow_files_command(interaction: discord.Interaction, action: str, scope: str = None):
    """Grant or withdraw the GALLERY file-reupload consent, server-wide or
    (with scope 'local') for this chat's bridge. The consent semantics —
    why every chat must be covered — are documented on
    db.bridge_file_relay_enabled."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("allow_files_no_permission", lang), ephemeral=True)
        return

    action = action.strip().lower()
    scope = (scope or "").strip().lower()
    if action not in ("enable", "disable") or scope not in ("", "local"):
        await interaction.response.send_message(localized("allow_files_usage", lang), ephemeral=True)
        return

    enabled = action == "enable"
    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
            return
        db.set_bridge_file_consent(row["bridge_id"], enabled, enabled_by=interaction.user.id)
        key = "allow_files_bridge_enabled" if enabled else "allow_files_bridge_disabled"
    else:
        db.set_server_file_consent("discord", str(interaction.guild_id), enabled, enabled_by=interaction.user.id)
        key = "allow_files_enabled" if enabled else "allow_files_disabled"

    await interaction.response.send_message(localized(key, lang), ephemeral=True)

@bot.tree.command(name="verify-list", description="publish IDs of (un)verified users for Confederate Guard (bot admins)")
@app_commands.describe(action="enable or disable")
async def verify_list_cmd(interaction: discord.Interaction, action: str):
    """Toggle the Confederate Guard verification sync (publishing user ids to the
    VERIFIED/UNVERIFIED channels). Bot Admins only; on by default."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    action = action.strip().lower()
    if action == "enable":
        db.set_verify_list_enabled(True)
        await interaction.response.send_message(localized("verify_list_enabled", lang), ephemeral=True)
    elif action == "disable":
        db.set_verify_list_enabled(False)
        await interaction.response.send_message(localized("verify_list_disabled", lang), ephemeral=True)
    else:
        await interaction.response.send_message(localized("verify_list_usage", lang), ephemeral=True)

@bot.tree.command(name="webhooks", description="show relayed messages as webhooks (sender avatar and name)")
@app_commands.describe(action="enable or disable",
                       scope="`local` for this bridge only; omit for the whole server")
async def webhooks_command(interaction: discord.Interaction, action: str, scope: str | None = None):
    """Webhook-style relay copies, in the same two scopes as `/allow-files`:
    the whole server by default, or the server's chats in one bridge with
    `scope: local`. Both cover chats that join later."""
    chat_id = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_id)
    if not (is_admin("discord", interaction.user.id) or is_chat_admin("discord", chat_id, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    action = action.strip().lower()
    if action not in ("enable", "disable"):
        await interaction.response.send_message(localized("webhooks_usage", lang), ephemeral=True)
        return
    scope = (scope or "").strip().lower()
    if scope not in ("", "local"):
        await interaction.response.send_message(localized("webhooks_usage", lang), ephemeral=True)
        return

    enabled = action == "enable"
    server_id = str(interaction.guild_id)

    if scope == "local":
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
            return
        db.set_bridge_webhooks(server_id, row["bridge_id"], enabled,
                               enabled_by=interaction.user.id)
        key = "webhooks_bridge_enabled" if enabled else "webhooks_bridge_disabled"
    else:
        db.set_server_webhooks(server_id, enabled, enabled_by=interaction.user.id)
        key = "webhooks_enabled" if enabled else "webhooks_disabled"

    if not enabled:
        db.set_webhooks_enabled(chat_id, False)

    await interaction.response.send_message(localized(key, lang), ephemeral=True)
