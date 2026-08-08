"""User-facing commands: consent (/verify, /unverify), moderation of users
(/shadow-ban), identity lookup (whois — context menu and slash), /privacy,
cross-community pings (/mention) and /help.

The consent flow's other half — prompting unverified senders automatically —
lives in discord_bot/events.py: on_message; the two share the same pattern
of clearing every pending prompt on one click.
"""
import logging

import discord
from discord import app_commands

import db
from message_relay import clip_text
from utils import (
    get_chat_lang, is_admin, localized, localized_consent_body,
    localized_consent_button, localized_consent_title, localized_help,
    localized_whois, rate_limit_ok,
)

from discord_bot.client import bot, announce_unverified_user, announce_verified_user
from discord_bot.feeds import relay_avatar_url
from discord_bot.mentions import resolve_discord_user
from discord_bot.relay import _relay_pending_discord_first_message, send_bridge_mention

logger = logging.getLogger("bridge.discord")

@bot.tree.command(name="verify", description="confirm consent to message forwarding")
async def verify_slash(interaction: discord.Interaction):
    """Ask for one's own consent prompt without having to post a message
    first. Replaces any prompt already standing in this community; the button
    logic mirrors the on_message consent flow (clear all pendings, announce,
    relay held first messages)."""
    prefix = str(interaction.guild_id)
    user_id_str = str(interaction.user.id)
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"

    if not rate_limit_ok(("verify-cmd", "discord", user_id_str), limit=2, window_seconds=60):
        await interaction.response.send_message(localized("too_many_requests", lang), ephemeral=True)
        return

    if db.is_user_verified("discord", user_id_str, "*"):
        await interaction.response.send_message(localized("verify_already", lang), ephemeral=True)
        return

    prev = db.get_pending_consent("discord", prefix, user_id_str)
    if prev:
        try:
            gid, cid = prev["chat_key"].split(":")
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    msg = await ch.fetch_message(int(prev["bot_message_id"]))
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass
        db.remove_pending_consent("discord", prefix, user_id_str)

    from discord import ui, ButtonStyle

    class _VerifyView(ui.View):
        """Consent button, same contract as the on_message prompt's view."""

        def __init__(self, prefix, user_id):
            """Remember whose consent this button collects."""
            super().__init__(timeout=None)
            self.prefix = str(prefix)
            self.user_id = str(user_id)

        @ui.button(label=localized_consent_button(lang), style=ButtonStyle.primary)
        async def accept(self, interaction2: discord.Interaction, button: ui.Button):
            """Record consent, clear all pending prompts, announce to
            Confederate Guard, relay the held first messages."""
            if str(interaction2.user.id) != str(self.user_id):
                await interaction2.response.send_message(localized("verify_button_not_yours", lang), ephemeral=True)
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
                await interaction2.message.delete()
            except Exception:
                pass
            await interaction2.response.send_message(localized("verify_thanks", lang), ephemeral=True)
            await announce_verified_user(self.user_id, interaction2.guild_id)
            for p in all_pendings:
                await _relay_pending_discord_first_message(p)

    mention = f"<@{interaction.user.id}>"
    consent_text = f"{mention}\n**{localized_consent_title(lang)}**\n\n{localized_consent_body(lang)}"
    sent = await interaction.channel.send(consent_text, view=_VerifyView(prefix, user_id_str))
    db.add_pending_consent(
        "discord",
        prefix,
        user_id_str,
        str(sent.id),
        f"{interaction.guild_id}:{interaction.channel_id}"
    )
    await interaction.response.send_message(localized("verify_message_sent", lang), ephemeral=True)

@bot.tree.command(name="unverify", description="revoke a user's verification")
async def unverify(interaction: discord.Interaction, target: str = None):
    """Withdraw forwarding consent — one's own with no argument, anyone's for
    bot admins. Deletes every Discord consent row of the user (consent is
    platform-wide) and announces the change for Confederate Guard."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if target is None or not target.strip():
        uid = interaction.user.id
    else:
        if not is_admin("discord", interaction.user.id):
            await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
            return
        if target.startswith("<@") or not target.isdigit() or "#" in target:
            uid = await resolve_discord_user(interaction.guild, target)
            if uid is None:
                await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
                return
        else:
            uid = int(target)

    db.cur.execute("DELETE FROM verified_users WHERE platform='discord' AND user_id=?", (str(uid),))
    db.conn.commit()
    await interaction.response.send_message(localized("unverify_done", lang, user_id=uid), ephemeral=True)
    await announce_unverified_user(uid)

@bot.tree.command(name="shadow-ban", description="hide user's messages from relay")
async def shadow_ban(interaction: discord.Interaction, target: str):
    """Shadow-ban a user (their messages are silently deleted instead of
    relayed). Bridge Admins of this chat's bridge and Bot Admins."""
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)
    allowed = False
    if is_admin("discord", interaction.user.id):
        allowed = True
    else:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row:
            bridge_admins = db.get_bridge_admins(row["bridge_id"])
            if str(interaction.user.id) in bridge_admins:
                allowed = True
    if not allowed:
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    uid = None
    if target.startswith("<@") or not target.isdigit() or "#" in target:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
    else:
        uid = int(target)

    db.add_shadow_ban("discord", uid)
    await interaction.response.send_message(localized("shadowban_done", lang, user_id=uid), ephemeral=True)

def _private_whois_embed(lang, nickname, avatar_url):
    """Everything `whois` may show about a sender who turned it off in
    `/privacy`: the nickname their messages already carry, and their avatar."""
    embed = discord.Embed(title=localized_whois("title", lang), color=discord.Color.blurple())
    embed.add_field(name=localized_whois("field_nickname", lang), value=nickname or "—", inline=False)
    embed.set_footer(text=localized_whois("private", lang))
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed

async def _whois_lookup(interaction: discord.Interaction, target_message: discord.Message | None, replied_id: str | None = None):
    """The whois worker: map a relayed copy back to its origin sender and
    show what the origin platform knows about them. Requesters must be
    verified themselves (symmetry: you may look up only if you may be looked
    up); senders who set hide_whois get the reduced embed.

    Inside an inbox conversation only one person may be looked up: whoever is
    writing to the receiver bot. Staff answering them are off limits — they
    may be reading under a 'Staff A' label, and either way the thread is a
    workplace rather than a bridged community whose members agreed to be
    identifiable to each other."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"

    requester_id = str(interaction.user.id)
    if not rate_limit_ok(("whois", "discord", requester_id), limit=5, window_seconds=60):
        await interaction.response.send_message(localized("too_many_requests", lang), ephemeral=True)
        return

    if not (
        is_admin("discord", interaction.user.id)
        or db.is_user_verified("discord", requester_id, str(interaction.guild_id))
    ):
        await interaction.response.send_message(
            localized_whois("not_verified", lang), ephemeral=True
        )
        return

    def _find_origin_row(message_id_platform: str | None):
        """The message_copies row of a copy in this chat, or None."""
        if not message_id_platform:
            return None
        return db.cur.execute(
            "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
            ("discord", chat_key, str(message_id_platform))
        ).fetchone()

    row = None
    if target_message:
        row = _find_origin_row(str(target_message.id))
    elif replied_id:
        row = _find_origin_row(replied_id)

    if not row:
        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await interaction.response.send_message(localized_whois("origin_missing", lang), ephemeral=True)
        return

    origin_platform = msg_row["origin_platform"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    if db.is_inbox_bridge(msg_row["bridge_id"]) and origin_platform != "inbox":
        await interaction.response.send_message(
            localized_whois("inbox_writer_only", lang), ephemeral=True
        )
        return

    try:
        if origin_platform == "discord":
            guild_id, _ = msg_row["origin_chat_id"].split(":")
            guild = bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None

            user_obj = None
            try:
                user_obj = await bot.fetch_user(int(origin_sender_id))
            except Exception:
                user_obj = getattr(member, "user", None)

            nick = member.display_name if member else "—"
            user_name = "—"
            if user_obj:
                user_name = f"{user_obj.name}#{user_obj.discriminator}"
            elif member:
                user_name = f"{member.name}#{member.discriminator}"

            avatar_url = None
            if user_obj and getattr(user_obj, "display_avatar", None):
                avatar_url = str(user_obj.display_avatar.url)

            if db.get_privacy_flag("discord", origin_sender_id, "hide_whois"):
                await interaction.response.send_message(
                    embed=_private_whois_embed(lang, nick, avatar_url), ephemeral=True
                )
                return

            mode_key = str(getattr(member, "status", "offline"))
            if mode_key not in ("online", "idle", "dnd", "offline", "invisible"):
                mode_key = "offline"
            if mode_key == "invisible":
                mode_key = "offline"
            mode = localized_whois(f"mode_{mode_key}", lang)

            custom_status = "—"
            if member:
                try:
                    custom = discord.utils.find(lambda a: isinstance(a, discord.CustomActivity), member.activities or [])
                    if custom and getattr(custom, "name", None):
                        custom_status = custom.name
                except Exception:
                    custom_status = "—"

            banner_url = None
            created_at = "—"
            if user_obj:
                banner_url = str(user_obj.banner.url) if getattr(user_obj, "banner", None) else None
                if getattr(user_obj, "created_at", None):
                    created_at = discord.utils.format_dt(user_obj.created_at, style="F")

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick or "—", inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=user_name or "—", inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_status", lang), value=custom_status, inline=False)
            embed.add_field(name=localized_whois("field_mode", lang), value=mode, inline=False)
            embed.add_field(name=localized_whois("field_registered", lang), value=created_at, inline=False)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            if banner_url:
                embed.set_image(url=banner_url)

            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif origin_platform == "telegram":
            from telegram_bot import bot as tg_bot
            prefix = msg_row["origin_chat_id"].split(":", 1)[0]
            try:
                member = await tg_bot.get_chat_member(int(prefix), int(origin_sender_id))
                u = member.user
                nick = u.full_name or (u.first_name or "—")
                username = f"@{u.username}" if u.username else "—"
                full_user = await tg_bot.get_chat(int(origin_sender_id))
                bio = getattr(full_user, "bio", None) or "—"
            except Exception:
                nick, username, bio = "—", "—", "—"

            if db.get_privacy_flag("telegram", origin_sender_id, "hide_whois"):
                await interaction.response.send_message(
                    embed=_private_whois_embed(lang, nick, None), ephemeral=True
                )
                return

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick, inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=username, inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_bio", lang), value=bio if len(bio) < 1000 else (bio[:997] + "..."), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif origin_platform == "inbox":
            from inbox import inbox_whois_profile
            nick, username, bio = await inbox_whois_profile(
                msg_row["origin_chat_id"], origin_sender_id
            )

            if db.get_privacy_flag("telegram", origin_sender_id, "hide_whois"):
                await interaction.response.send_message(
                    embed=_private_whois_embed(lang, nick, None), ephemeral=True
                )
                return

            embed = discord.Embed(
                title=localized_whois("title", lang),
                color=discord.Color.blurple()
            )
            embed.add_field(name=localized_whois("field_nickname", lang), value=nick, inline=False)
            embed.add_field(name=localized_whois("field_username", lang), value=username, inline=False)
            embed.add_field(name=localized_whois("field_id", lang), value=str(origin_sender_id), inline=False)
            embed.add_field(name=localized_whois("field_bio", lang), value=bio if len(bio) < 1000 else (bio[:997] + "..."), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(localized_whois("origin_not_found", lang), ephemeral=True)
    except Exception as e:
        logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
        await interaction.response.send_message(
            localized_whois("fetch_error", lang, error=type(e).__name__), ephemeral=True
        )

@bot.tree.context_menu(name="whois")
async def whois_context_menu(interaction: discord.Interaction, message: discord.Message):
    """Context menu (правая кнопка → Apps → whois): показывает автора пересланного сообщения."""
    await _whois_lookup(interaction, target_message=message)

@bot.tree.command(name="whois", description="info about the message author (reply to a bot relay message)")
async def whois_command(interaction: discord.Interaction):
    """
    Slash-команда /whois. Поскольку Discord не передаёт контекст reply для slash-команд,
    используй лучше context menu: ПКМ на сообщении → Apps → whois.
    """
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    await interaction.response.send_message(
        localized_whois("use_context_menu", lang), ephemeral=True
    )

def _privacy_embed(user_id, lang):
    """The `/privacy` menu: one line per switch, with what it does and whether
    the user has it on."""
    flags = db.get_user_privacy("discord", user_id)
    lines = [localized("privacy_header", lang), ""]
    for flag in db.PRIVACY_FLAGS:
        state = localized("privacy_state_on" if flags[flag] else "privacy_state_off", lang)
        mark = "🔒" if flags[flag] else "🔓"
        lines.append(f"{mark} {localized(f'privacy_opt_{flag}', lang)} — **{state}**")
    return discord.Embed(
        title=localized("privacy_title", lang),
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )

class PrivacyToggleButton(discord.ui.Button):
    """One toggle per privacy flag; pressing it flips the flag and redraws
    the menu."""

    def __init__(self, flag, lang, enabled):
        """Style reflects the current state (green = on)."""
        super().__init__(
            label=clip_text(localized(f"privacy_btn_{flag}", lang), 80),
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
        )
        self.flag = flag
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        """Flip the flag (owner only) and refresh embed + buttons."""
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message(
                localized("privacy_not_yours", self.lang), ephemeral=True
            )
            return
        db.toggle_privacy_flag("discord", interaction.user.id, self.flag)
        await interaction.response.edit_message(
            embed=_privacy_embed(interaction.user.id, self.lang),
            view=PrivacyView(interaction.user.id, self.lang),
        )

class PrivacyView(discord.ui.View):
    """The /privacy button row. 10-minute timeout: the menu is ephemeral and
    per-user, nothing needs to survive a restart."""

    def __init__(self, user_id, lang):
        """One toggle button per flag, in PRIVACY_FLAGS order."""
        super().__init__(timeout=600)
        self.user_id = user_id
        flags = db.get_user_privacy("discord", user_id)
        for flag in db.PRIVACY_FLAGS:
            self.add_item(PrivacyToggleButton(flag, lang, flags[flag]))

@bot.tree.command(name="privacy", description="configure what the bot may share about you")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def privacy_cmd(interaction: discord.Interaction):
    """Show the caller their /privacy switches (flag meanings documented on
    the user_privacy table in db/schema.py)."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    await interaction.response.send_message(
        embed=_privacy_embed(interaction.user.id, lang),
        view=PrivacyView(interaction.user.id, lang),
        ephemeral=True,
    )

@bot.tree.command(name="mention", description="mention a user from another bridge community (1-hour cooldown per user)")
@app_commands.describe(target="User ID or username")
async def mention_cmd(interaction: discord.Interaction, target: str):
    """Ping a user in whichever bridge chat can reach them (send logic in
    relay.send_bridge_mention). The name is resolved in this guild first,
    then in the other Discord guilds of the bridge. Honors the target's
    block_mention privacy flag and a per-target hourly cooldown."""
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key)

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await interaction.response.send_message(localized("chat_not_in_bridge", lang), ephemeral=True)
        return
    bridge_id = row["bridge_id"]

    target = target.strip()
    if target.isdigit():
        uid = int(target)
    else:
        uid = await resolve_discord_user(interaction.guild, target)
        if uid is None:
            for c in db.get_bridge_chats(bridge_id):
                if c["platform"] != "discord" or c["chat_id"] == chat_key:
                    continue
                guild = bot.get_guild(int(c["chat_id"].split(":")[0]))
                if guild is None:
                    continue
                uid = await resolve_discord_user(guild, target)
                if uid is not None:
                    break
    if uid is None:
        await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
        return

    if db.get_privacy_flag("discord", uid, "block_mention"):
        await interaction.response.send_message(localized("mention_opted_out", lang), ephemeral=True)
        return

    if not rate_limit_ok(("mention-target", str(uid)), limit=1, window_seconds=3600):
        await interaction.response.send_message(localized("mention_cooldown", lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    ok = await send_bridge_mention(
        bridge_id, "discord", chat_key, uid,
        sender_name=interaction.user.display_name or str(interaction.user),
        place_name=interaction.guild.name if interaction.guild else "Discord",
        messenger_name="Discord",
        avatar_url=await relay_avatar_url(
            interaction.user.id, str(interaction.user.display_avatar.url)
        ),
    )
    if ok:
        await interaction.followup.send(localized("mention_sent", lang), ephemeral=True)
    else:
        await interaction.followup.send(localized("mention_no_discord", lang), ephemeral=True)

@bot.tree.command(name="help", description="show this command list")
async def help_command(interaction: discord.Interaction):
    """The command list, grouped by required role. Maintained by hand in the
    i18n help.* keys — a new command must be added there (all six languages)
    to appear here."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")

    everyone_lines = "\n".join([
        localized_help("cmd_bridge", lang),
        localized_help("cmd_wikifeeds", lang),
        localized_help("cmd_whois", lang),
        localized_help("cmd_verify", lang),
        localized_help("cmd_mention", lang),
        localized_help("cmd_poll", lang),
        localized_help("cmd_appeal", lang),
        localized_help("cmd_privacy", lang),
        localized_help("cmd_locale", lang),
        localized_help("cmd_loc_compare", lang),
        localized_help("cmd_loc_suggest", lang),
        localized_help("cmd_help", lang),
    ])

    admins_lines = "\n".join([
        localized_help("cmd_rfb", lang),
        localized_help("cmd_setadmin", lang),
        localized_help("cmd_lang", lang),
        localized_help("cmd_locallang", lang),
        localized_help("cmd_remindrules", lang),
        localized_help("cmd_shadowban", lang),
        localized_help("cmd_allow_bots", lang),
        localized_help("cmd_allow_files", lang),
        localized_help("cmd_webhooks", lang),
        localized_help("cmd_deadtopic", lang),
        localized_help("cmd_deadchat", lang),
        localized_help("cmd_newschat", lang),
        localized_help("cmd_close", lang),
        localized_help("cmd_close_header", lang),
    ])

    bot_admins_lines = "\n".join([
        localized_help("cmd_atb", lang),
        localized_help("cmd_setytfeed", lang),
        localized_help("cmd_remytfeed", lang),
        localized_help("cmd_setbskyfeed", lang),
        localized_help("cmd_rembskyfeed", lang),
        localized_help("cmd_settgfeed", lang),
        localized_help("cmd_remtgfeed", lang),
        localized_help("cmd_setwikifeed", lang),
        localized_help("cmd_remwikifeed", lang),
        localized_help("cmd_wikifeed_settings", lang),
        localized_help("cmd_remadmin", lang),
        localized_help("cmd_setlocaladmin", lang),
        localized_help("cmd_remlocaladmin", lang),
        localized_help("cmd_localizer_add", lang),
        localized_help("cmd_localizer_rem", lang),
        localized_help("cmd_unverify", lang),
        localized_help("cmd_setname", lang),
        localized_help("cmd_verify_list", lang),
        localized_help("cmd_list_chats", lang),
        localized_help("cmd_force_leave", lang),
        localized_help("cmd_backup", lang),
        localized_help("cmd_loc_reply", lang),
        localized_help("cmd_setinbox", lang),
        localized_help("cmd_reminbox", lang),
        localized_help("cmd_setinboxchat", lang),
        localized_help("cmd_reminboxchat", lang),
        localized_help("cmd_inboxanon", lang),
        localized_help("cmd_inboxlist", lang),
        localized_help("cmd_inboxban", lang),
        localized_help("cmd_inboxunban", lang),
    ])

    embed = discord.Embed(
        title=localized_help("title", lang),
        color=discord.Color.blurple()
    )
    embed.add_field(name=localized_help("section_everyone", lang), value=everyone_lines, inline=False)
    embed.add_field(name=localized_help("section_admins", lang), value=admins_lines, inline=False)
    embed.add_field(name=localized_help("section_bot_admins", lang), value=bot_admins_lines, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)
