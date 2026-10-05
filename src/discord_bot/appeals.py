"""The Purgatorium ban-appeal system: /appeal, the verdict buttons, consul
anonymization, the DM↔thread relay hooks, and the daily maintenance pass.

How it hangs together: a banned user lands on the Purgatorium server, runs
/appeal, and gets a public thread bridged to their DM through an ordinary
bridge with an id from the reserved range (db/appeals.py). Consuls (CONSULS
role holders) write in the thread and reach the appellant anonymized as
'Consul A/B/…' or under a /setname alias; the appellant's DMs come back into
the thread. Verdicts are two buttons on the pinned message; a pardon kicks
the user from Purgatorium and publishes their id to APPEAL_PARDON_CHANNELS
for Confederate Guard to unban everywhere, a condemnation bans them there for good.

Storage is db/appeals.py; the inbound event routing that calls the two
handle_appeal_* functions is discord_bot/events.py.
"""
import asyncio
import logging
import re

import discord
from discord import app_commands

import db
import sponsors
from message_relay import clean_display_name
from utils import (
    DEFAULT_LANG, SUPPORTED_LANGS, get_chat_lang, is_admin, localized,
    rate_limit_ok,
)

from discord_bot.client import bot, _post_user_id_to_channels
from discord_bot.mentions import resolve_discord_user
from discord_bot.relay import _relay_verified_discord_message

logger = logging.getLogger("bridge.discord")

def _locale_to_lang(locale):
    """Map a Discord interaction locale (e.g. 'ru', 'en-US') to a supported
    bot language. The appeal system can't know which server banned the user
    (that is Confederate Guard's database), so the user's own client language is the
    best signal for how to talk to them."""
    code = str(locale or "").lower()[:2]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG

def _appeal_thread_chat_id(thread_id, guild_id=None):
    """The bot-wide chat key of an appeal thread ('purgatorium_guild:thread')."""
    settings = db.appeal_settings(guild_id)
    return f"{settings['guild_id']}:{thread_id}"

def _appeal_thread_lang(thread_id):
    """The language an appeal thread runs in (its chat-key setting, falling
    back to the default) — used for everything consuls see."""
    appeal = db.get_appeal_by_thread(thread_id)
    return get_chat_lang(_appeal_thread_chat_id(
        thread_id, appeal["guild_id"] if appeal else None)) or DEFAULT_LANG

def _consul_label(thread_id, consul_user_id):
    """How a server member writing in an appeal thread is signed in the copy the
    appellant receives: their `/setname` alias when they have one, otherwise the
    stable anonymized 'Consul A', 'Consul B', …

    Neither form is localized. An alias is a fixed string by design, and the
    anonymous signature is deliberately built from the English wording and the
    Latin alphabet whatever the appellant reads the rest of the bridge in: it is
    a label identifying one person across a thread, not a sentence, and a
    consul who appears as 'Consul B' to one appellant and 'Консул Б' to another
    is harder to talk about than one who is 'Consul B' to everybody.

    The per-thread index is reserved in either case: a consul who later drops
    their alias must fall back to the same letter they would have had, instead
    of reading to the appellant as one more person joining the thread."""
    idx = db.get_consul_ord(str(thread_id), consul_user_id)
    appeal = db.get_appeal_by_thread(thread_id)
    alias = db.get_consul_name(consul_user_id)
    if appeal and appeal["guild_id"] is not None:
        alias = db.get_appeal_consul_alias(consul_user_id, appeal["guild_id"])
    if alias:
        return alias
    letters = localized("appeal_consul_letters", DEFAULT_LANG)
    letter = letters[idx] if isinstance(letters, str) and idx < len(letters) else str(idx + 1)
    return localized("appeal_consul_name", DEFAULT_LANG, letter=letter)

CONSUL_NAME_MAX_LEN = 32
_CONSUL_PING_RE = re.compile(r"@everyone|@here|<@[!&]?\d+>")
_CONSUL_MARKUP_RE = re.compile(r"[*_~`|\\<>]")

def _normalize_consul_name(name):
    """Comparison form of an alias: case-folded, inner whitespace collapsed, so
    that 'Ivan' and 'ivan ' are one and the same name."""
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()

def _reserved_consul_labels():
    """Every anonymized signature the bot itself can produce, in all supported
    languages — an alias must not be able to impersonate one of them (nor a
    consul in another language: appellants read different locales)."""
    reserved = set()
    for lang in SUPPORTED_LANGS:
        letters = localized("appeal_consul_letters", lang)
        marks = list(letters) if isinstance(letters, str) else []
        marks += [str(n) for n in range(1, 100)]
        for mark in marks:
            reserved.add(_normalize_consul_name(localized("appeal_consul_name", lang, letter=mark)))
    return reserved

def _clean_consul_name(raw):
    """Validate a `/setname` alias.

    Returns ``(display, error_key)``. An empty ``display`` with no error means a
    reset. Pings and the characters that could forge a relay header are removed
    rather than escaped: the alias is re-escaped downstream by the relay's own
    header builder, so storing an escaped form would show the backslashes."""
    text = _CONSUL_PING_RE.sub("", str(raw or ""))
    text = _CONSUL_MARKUP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "", None
    if len(text) > CONSUL_NAME_MAX_LEN:
        return None, "setname_too_long"
    if _normalize_consul_name(text) in _reserved_consul_labels():
        return None, "setname_reserved"
    return text, None

async def _is_consul_user(user_id, guild_id=None):
    """Whether a user may hold a consul alias: a CONSULS role holder on
    Purgatorium, or a bot admin. Unlike `_can_judge_appeals`, which inspects the
    member who ran a command, this resolves an arbitrary user id against
    Purgatorium's member list."""
    if is_admin("discord", user_id):
        return True
    settings = db.appeal_settings(guild_id)
    if settings is None:
        return False
    guild = bot.get_guild(int(settings["guild_id"]))
    if guild is None:
        return False
    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            return False
    return any(role.id in settings["consul_roles"]
               for role in getattr(member, "roles", None) or [])

APPEAL_AUTO_KICK_SECONDS = 7 * 86400
APPEAL_CLEANUP_SECONDS = 7 * 86400

def _cleanup_appeal_records(appeal_row):
    """Drop an appeal's rows and detach its bridge chats (the bridge row itself
    disappears with its last chat via remove_chat_from_bridge)."""
    if not appeal_row:
        return
    settings = db.appeal_settings(appeal_row["guild_id"])
    db.remove_chat_from_bridge(f"{settings['guild_id']}:{appeal_row['thread_id']}")
    db.remove_chat_from_bridge(f"dm:{appeal_row['user_id']}")
    db.delete_appeal(appeal_row["user_id"])

def _can_judge_appeals(member):
    """Consuls hold one of the CONSULS roles on Purgatorium; global bot admins also count."""
    if is_admin("discord", member.id):
        return True
    guild = getattr(member, "guild", None)
    settings = db.appeal_settings(guild.id) if guild else None
    return bool(settings and any(role.id in settings["consul_roles"]
                                 for role in getattr(member, "roles", None) or []))

class AppealVerdictButton(discord.ui.Button):
    """One of the two verdict buttons ('pardon' / 'condemn') on the pinned
    appeal message."""

    def __init__(self, action, user_id, lang):
        """Stable custom_id 'appeal:<action>:<user_id>' — required for the
        view to keep working across restarts."""
        super().__init__(
            label=localized(f"appeal_btn_{action}", lang),
            style=discord.ButtonStyle.success if action == "pardon" else discord.ButtonStyle.danger,
            custom_id=f"appeal:{action}:{user_id}",
        )
        self.action = action
        self.target_uid = int(user_id)

    async def callback(self, interaction: discord.Interaction):
        """Route the click into the shared permission/confirmation flow."""
        await handle_appeal_verdict_click(interaction, self.action, self.target_uid)

class AppealVerdictView(discord.ui.View):
    """Verdict buttons pinned in an appeal thread. Persistent: stable custom_ids,
    re-registered in setup_hook for every open appeal after a restart."""

    def __init__(self, user_id, lang):
        """Build the pardon+condemn pair for one appellant."""
        super().__init__(timeout=None)
        self.add_item(AppealVerdictButton("pardon", user_id, lang))
        self.add_item(AppealVerdictButton("condemn", user_id, lang))

class _AppealConfirmView(discord.ui.View):
    """One-button ephemeral confirmation shown before a verdict is executed."""

    def __init__(self, action, target_uid, pinned_message_id, lang):
        """Wire the confirm button straight to execute_appeal_verdict."""
        super().__init__(timeout=60)
        button = discord.ui.Button(
            label=localized("appeal_confirm_btn", lang),
            style=discord.ButtonStyle.danger,
        )

        async def _confirm(interaction: discord.Interaction):
            """Execute the verdict the consul just confirmed."""
            await execute_appeal_verdict(interaction, action, target_uid, pinned_message_id)

        button.callback = _confirm
        self.add_item(button)

async def handle_appeal_verdict_click(interaction: discord.Interaction, action, target_uid):
    """First stage of a verdict click: permission check, staleness check, then
    an ephemeral confirmation — a verdict is irreversible, so no single
    misclick may execute one."""
    thread_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG
    if not db.appeal_settings(interaction.guild_id) or not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", thread_lang), ephemeral=True)
        return
    appeal = db.get_open_appeal(target_uid)
    if (not appeal or str(db.appeal_settings(appeal["guild_id"])["guild_id"]) != str(interaction.guild_id)
            or str(appeal["thread_id"]) != str(interaction.channel_id)):
        await interaction.response.send_message(localized("appeal_already_resolved", thread_lang), ephemeral=True)
        return
    key = "appeal_confirm_pardon" if action == "pardon" else "appeal_confirm_condemn"
    await interaction.response.send_message(
        localized(key, thread_lang),
        view=_AppealConfirmView(action, target_uid, interaction.message.id if interaction.message else None, thread_lang),
        ephemeral=True,
    )

async def execute_appeal_verdict(interaction: discord.Interaction, action, target_uid, pinned_message_id):
    """Carry out a confirmed verdict.

    Both checks are repeated (the confirmation may sit unclicked while another
    consul resolves the appeal) and resolve_appeal's row-count doubles as the
    race lock. Pardon: DM the user, publish their id to the pardon channels
    (Confederate Guard unbans them everywhere), kick from Purgatorium. Condemn: DM and
    permanent ban. Either way the thread gets a note, loses its buttons, and is
    archived+locked. Every side effect is separately fail-safed — a user with
    closed DMs must not block the verdict itself."""
    thread_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG
    if not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", thread_lang), ephemeral=True)
        return

    status = "pardoned" if action == "pardon" else "condemned"
    appeal = db.get_open_appeal(target_uid)
    if (not appeal or str(db.appeal_settings(appeal["guild_id"])["guild_id"]) != str(interaction.guild_id)
            or str(appeal["thread_id"]) != str(interaction.channel_id)
            or not db.resolve_appeal(target_uid, status, interaction.user.id)):
        await interaction.response.send_message(localized("appeal_already_resolved", thread_lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    user_lang = appeal["lang"] if appeal["lang"] in SUPPORTED_LANGS else DEFAULT_LANG
    settings = db.appeal_settings(appeal["guild_id"])
    purg = bot.get_guild(int(settings["guild_id"]))

    try:
        user = bot.get_user(target_uid) or await bot.fetch_user(target_uid)
        dm_key = "appeal_user_pardoned" if action == "pardon" else "appeal_user_condemned"
        await user.send(localized(dm_key, user_lang))
    except Exception:
        pass

    if action == "pardon":
        await _post_user_id_to_channels(settings["pardon_channels"], target_uid)
        if purg:
            try:
                await purg.kick(discord.Object(id=target_uid), reason="Appeal granted")
            except Exception:
                pass
    else:
        if purg:
            try:
                await purg.ban(
                    discord.Object(id=target_uid),
                    reason="Appeal denied by consuls",
                    delete_message_days=0,
                )
            except Exception:
                pass

    thread = interaction.channel
    note_key = "appeal_note_pardoned" if action == "pardon" else "appeal_note_condemned"
    try:
        await thread.send(localized(note_key, thread_lang))
    except Exception:
        pass

    if pinned_message_id:
        try:
            pinned = await thread.fetch_message(int(pinned_message_id))
            await pinned.edit(view=None)
        except Exception:
            pass

    try:
        await interaction.followup.send(localized("appeal_verdict_done", thread_lang), ephemeral=True)
    except Exception:
        pass

    try:
        await thread.edit(archived=True, locked=True)
    except Exception:
        pass

@bot.tree.command(name="appeal", description="file a ban appeal (on the appeal server)")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(server_id="Appeal server ID when you belong to several")
async def appeal_cmd(interaction: discord.Interaction, server_id: str = None):
    """Open an appeal: create the thread, wire thread+DM into a fresh appeal
    bridge, post the pinned verdict message, and tell the ban-info channels
    (Confederate Guard posts the user's ban record into the thread in response).

    Order matters: the DM check comes *after* the records are written but is
    allowed to abort everything — a user whose DMs are closed can't receive
    consul replies, so the appeal is rolled back and they're told to open
    them. Any previous (resolved) appeal of the same user is cleaned up first;
    filing an appeal also auto-verifies the user so their DMs relay without a
    consent prompt."""
    lang = _locale_to_lang(interaction.locale)
    uid = interaction.user.id

    existing_appeal = db.get_open_appeal(uid)
    if existing_appeal:
        old = db.appeal_settings(existing_appeal["guild_id"])
        old_guild = bot.get_guild(int(old["guild_id"]))
        await interaction.response.send_message(localized(
            "appeal_already_open_server", lang,
            server=old_guild.name if old_guild else old["guild_id"]), ephemeral=True)
        return

    eligible = []
    for settings in db.all_appeal_servers():
        guild = bot.get_guild(int(settings["guild_id"]))
        if guild and guild.get_member(uid):
            eligible.append((guild, settings))
    if not eligible:
        requested = (db.appeal_settings(server_id) if server_id and
                     server_id.strip().isdecimal() else None)
        await interaction.response.send_message(
            localized("appeal_not_member", lang,
                      invite=(requested or db.appeal_settings())["invite_url"]), ephemeral=True
        )
        return
    if len(eligible) > 1 and server_id is None:
        choices = "\n".join(f"{guild.name} ({guild.id})" for guild, _ in eligible)
        await interaction.response.send_message(localized(
            "appeal_choose_server", lang, servers=choices), ephemeral=True)
        return
    selected = next(((guild, settings) for guild, settings in eligible
                     if server_id is None or str(guild.id) == server_id.strip()), None)
    if selected is None:
        await interaction.response.send_message(localized("appeal_server_invalid", lang), ephemeral=True)
        return
    purg, settings = selected
    owner = settings["owner_id"]
    if not settings["accepting"] or (owner is not None and
            not is_admin("discord", owner) and sponsors.sponsor_quota_tier("discord", owner) == 0):
        await interaction.response.send_message(localized("appeal_server_frozen", lang), ephemeral=True)
        return
    if not settings["pardon_channels"] or not settings["baninfo_channels"]:
        await interaction.response.send_message(localized("appeal_server_incomplete", lang), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(int(settings["thread_channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(settings["thread_channel_id"]))
        except Exception:
            channel = None
    if channel is None:
        await interaction.followup.send(localized("appeal_failed", lang), ephemeral=True)
        return

    try:
        thread = await channel.create_thread(
            name=clean_display_name(interaction.user.display_name or interaction.user.name, max_len=90),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
    except Exception as e:
        logger.warning("appeal thread creation failed (user=%s): %s", uid, e)
        await interaction.followup.send(localized("appeal_failed", lang), ephemeral=True)
        return

    _cleanup_appeal_records(db.get_appeal(uid))

    bridge_id = db.next_appeal_bridge_id()
    db.attach_chat("discord", f"{purg.id}:{thread.id}", bridge_id)
    db.attach_chat("discord", f"dm:{uid}", bridge_id)
    db.create_appeal(uid, thread.id, bridge_id, lang, purg.id)

    db.add_verified_user("discord", uid, str(purg.id), days_valid=365)

    try:
        await interaction.user.send(localized("appeal_created_dm", lang))
    except Exception:
        _cleanup_appeal_records(db.get_appeal(uid))
        try:
            await thread.delete()
        except Exception:
            pass
        await interaction.followup.send(localized("appeal_dm_closed", lang), ephemeral=True)
        return

    thread_lang = _appeal_thread_lang(thread.id)
    try:
        pinned = await thread.send(
            localized(
                "appeal_thread_info", thread_lang,
                mention=f"<@{uid}>", username=str(interaction.user), id=uid,
            ),
            view=AppealVerdictView(uid, thread_lang),
        )
        try:
            await pinned.pin()
        except Exception:
            pass
    except Exception as e:
        logger.warning("appeal pinned message failed (user=%s): %s", uid, e)

    await _post_user_id_to_channels(
        settings["baninfo_channels"], f"{uid} {thread.id}"
    )

    await interaction.followup.send(localized("appeal_created", lang), ephemeral=True)

@bot.tree.command(name="setname", description="set the alias appellants see instead of 'Consul A' (consuls)")
@app_commands.describe(
    name=f"Alias shown to appellants, up to {CONSUL_NAME_MAX_LEN} characters. Leave empty to go back to 'Consul A'.",
    user="Bot Admins only: whose alias to change (ID or mention)",
)
async def setname_command(interaction: discord.Interaction, name: str = None, user: str = None):
    """Consul aliases. Always answers ephemerally — the command may well be run
    inside an appeal thread, and the appellant must see neither the call nor the
    answer."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or DEFAULT_LANG

    if not db.appeal_settings(interaction.guild_id) or not _can_judge_appeals(interaction.user):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    target_id = interaction.user.id
    if user is not None:
        if not is_admin("discord", interaction.user.id):
            await interaction.response.send_message(localized("setname_user_forbidden", lang), ephemeral=True)
            return
        try:
            resolved = await resolve_discord_user(interaction.guild, user)
        except Exception:
            resolved = None
        if resolved is None:
            await interaction.response.send_message(localized("could_not_resolve_user", lang), ephemeral=True)
            return
        target_id = resolved

    is_other = str(target_id) != str(interaction.user.id)
    display, error_key = _clean_consul_name(name)
    if error_key:
        await interaction.response.send_message(
            localized(error_key, lang, limit=CONSUL_NAME_MAX_LEN), ephemeral=True
        )
        return

    if not display:
        if db.appeal_owner(interaction.guild_id) is None:
            db.remove_consul_name(target_id)
        else:
            db.remove_appeal_consul_alias(interaction.guild_id, target_id)
        key = "setname_reset_other" if is_other else "setname_reset"
        await interaction.response.send_message(
            localized(key, lang, user=f"<@{target_id}>"), ephemeral=True
        )
        return

    if not await _is_consul_user(target_id, interaction.guild_id):
        await interaction.response.send_message(localized("setname_target_not_consul", lang), ephemeral=True)
        return

    normalized = _normalize_consul_name(display)
    owner = (db.find_consul_name_owner(normalized)
             if db.appeal_owner(interaction.guild_id) is None
             else db.find_appeal_alias_owner(interaction.guild_id, normalized))
    if owner is not None and str(owner) != str(target_id):
        await interaction.response.send_message(localized("setname_taken", lang), ephemeral=True)
        return

    if db.appeal_owner(interaction.guild_id) is None:
        db.set_consul_name(target_id, display, normalized, set_by=interaction.user.id)
    else:
        db.set_appeal_consul_alias(interaction.guild_id, target_id, display,
                                   normalized, interaction.user.id)
    key = "setname_done_other" if is_other else "setname_done"
    await interaction.response.send_message(
        localized(key, lang, name=display, user=f"<@{target_id}>"), ephemeral=True
    )

def _appeal_registry_control(user_id, guild_id):
    """A sponsor may change only their server; Bot Admins may change any."""
    if is_admin("discord", user_id):
        return True
    return db.appeal_owner(guild_id) == str(user_id)

@bot.tree.command(name="appeal-register", description="register this server for sponsor appeals")
@app_commands.describe(thread_channel="Channel ID for appeal threads", invite="Discord invitation URL")
async def appeal_register_cmd(interaction: discord.Interaction, thread_channel: str, invite: str):
    """Require server ownership, a paid tier and permission to create threads."""
    lang = _locale_to_lang(interaction.locale)
    guild = interaction.guild
    if guild is None or db.appeal_owner(guild.id) is None:
        await interaction.response.send_message(localized("appeal_register_guild", lang), ephemeral=True)
        return
    admin = is_admin("discord", interaction.user.id)
    perms = interaction.user.guild_permissions
    if not admin and not (perms.administrator or perms.manage_guild):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    if not admin and sponsors.sponsor_quota_tier("discord", interaction.user.id) == 0:
        await interaction.response.send_message(localized("sponsor_not_active", lang), ephemeral=True)
        return
    owner = db.appeal_owner(guild.id)
    other = db.appeal_server_for_owner(interaction.user.id)
    if ((owner is not False and owner != str(interaction.user.id) and not admin)
            or (not admin and other is not None
                and str(other["guild_id"]) != str(guild.id))):
        await interaction.response.send_message(localized("appeal_register_taken", lang), ephemeral=True)
        return
    if not thread_channel.isdecimal() or not invite.startswith((
            "https://discord.gg/", "https://discord.com/invite/",
            "https://discordapp.com/invite/")):
        await interaction.response.send_message(localized("appeal_register_invalid", lang), ephemeral=True)
        return
    channel = guild.get_channel(int(thread_channel))
    bot_member = guild.me
    if (not isinstance(channel, discord.TextChannel) or bot_member is None or
            not channel.permissions_for(bot_member).create_public_threads or
            not channel.permissions_for(bot_member).send_messages_in_threads):
        await interaction.response.send_message(localized("appeal_register_channel", lang), ephemeral=True)
        return
    if not db.register_appeal_server(guild.id, interaction.user.id,
                                     channel.id, invite.strip(), allow_existing=admin):
        await interaction.response.send_message(localized("appeal_register_taken", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized("appeal_register_done", lang), ephemeral=True)

@bot.tree.command(name="appeal-protocol", description="set Guard sync channels and consul roles")
@app_commands.describe(pardon="Pardon channel ID", baninfo="Ban summary channel ID",
                       consul_roles="Comma-separated role IDs")
async def appeal_protocol_cmd(interaction: discord.Interaction, pardon: str,
                              baninfo: str, consul_roles: str):
    """Keep the Guard protocol within the same appeal server."""
    lang = _locale_to_lang(interaction.locale)
    guild = interaction.guild
    if guild is None or not _appeal_registry_control(interaction.user.id, guild.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    settings = db.appeal_settings(guild.id)
    ids = [item.strip() for item in consul_roles.split(",") if item.strip()]
    if (settings is None or settings["owner_id"] is None or not pardon.isdecimal()
            or not baninfo.isdecimal() or pardon == baninfo or not ids
            or any(not item.isdecimal() or guild.get_role(int(item)) is None for item in ids)
            or guild.get_channel(int(pardon)) is None
            or guild.get_channel(int(baninfo)) is None):
        await interaction.response.send_message(localized("appeal_protocol_invalid", lang), ephemeral=True)
        return
    db.set_appeal_channels(guild.id, pardon, baninfo, ids)
    await interaction.response.send_message(localized("appeal_protocol_done", lang), ephemeral=True)

@bot.tree.command(name="appeal-pause", description="pause or resume new appeals here")
@app_commands.describe(paused="True pauses new appeals; open appeals continue")
async def appeal_pause_cmd(interaction: discord.Interaction, paused: bool):
    """Preserve existing threads and their verdict buttons across a pause."""
    lang = _locale_to_lang(interaction.locale)
    guild = interaction.guild
    if guild is None or not _appeal_registry_control(interaction.user.id, guild.id):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return
    settings = db.appeal_settings(guild.id)
    if settings is None or settings["owner_id"] is None:
        await interaction.response.send_message(localized("appeal_register_guild", lang), ephemeral=True)
        return
    db.set_appeal_accepting(guild.id, not paused)
    await interaction.response.send_message(localized(
        "appeal_pause_done" if paused else "appeal_resume_done", lang), ephemeral=True)

@bot.tree.command(name="appeal-settings", description="show this appeal server's settings")
async def appeal_settings_cmd(interaction: discord.Interaction):
    """Display the effective routing so both bots can be configured alike."""
    lang = _locale_to_lang(interaction.locale)
    guild = interaction.guild
    settings = db.appeal_settings(guild.id) if guild else None
    if settings is None:
        await interaction.response.send_message(localized("appeal_register_guild", lang), ephemeral=True)
        return
    await interaction.response.send_message(localized(
        "appeal_settings_show", lang, guild=settings["guild_id"],
        thread=settings["thread_channel_id"],
        pardon=", ".join(settings["pardon_channels"]) or "—",
        baninfo=", ".join(settings["baninfo_channels"]) or "—",
        roles=", ".join(str(x) for x in settings["consul_roles"]) or "—",
        active="yes" if settings["accepting"] else "no"), ephemeral=True)

async def _appeal_maintenance_pass(client):
    """Daily housekeeping of the appeal system.

    Silently kicks Purgatorium members who have been on the server for over
    7 days without ever filing an appeal (bots, bot admins and anyone holding
    a role — consuls/staff — are exempt; Discord's own joined_at makes this
    restart-proof), and garbage-collects appeals resolved more than 7 days
    ago together with their bridge attachments. Message-copy bookkeeping for
    appeals has its own seven-day rolling retention, including open cases.
    """
    for settings in db.all_appeal_servers():
        purg = client.get_guild(int(settings["guild_id"]))
        if purg is None:
            continue
        now = discord.utils.utcnow()
        for member in list(purg.members):
            if member.bot:
                continue
            if is_admin("discord", member.id):
                continue
            if len(member.roles) > 1:
                continue
            if member.joined_at is None:
                continue
            if (now - member.joined_at).total_seconds() < APPEAL_AUTO_KICK_SECONDS:
                continue
            if db.has_any_appeal(member.id, purg.id):
                continue
            try:
                await purg.kick(member, reason="No appeal filed within 7 days")
            except Exception:
                pass

    for row in db.get_resolved_appeals_older_than(APPEAL_CLEANUP_SECONDS):
        _cleanup_appeal_records(row)
    await confirm_pending_appeal_flood_bans()

async def confirm_pending_appeal_flood_bans():
    """Purge appellant message records only after a live Discord ban proves
    Guard processed the exact appeal-server request. A delayed Guard response
    can be picked up on the next maintenance pass or by the short retry task."""
    for row in db.pending_appeal_flood_bans():
        guild = bot.get_guild(int(row["guild_id"]))
        if guild is None:
            continue
        try:
            await guild.fetch_ban(discord.Object(id=int(row["user_id"])))
        except discord.NotFound:
            continue
        except Exception:
            continue
        db.confirm_appeal_flood_ban(row["user_id"], row["bridge_id"])

async def _confirm_flood_ban_later():
    """Give Guard time to consume the protocol message without holding the
    user's DM handler. The daily pass is the restart-safe fallback."""
    for _ in range(6):
        await asyncio.sleep(5)
        await confirm_pending_appeal_flood_bans()

async def _request_guard_flood_ban(appeal):
    """Send only IDs on the established, scoped ban-info sync channel."""
    settings = db.appeal_settings(appeal["guild_id"])
    if not settings:
        return
    await _post_user_id_to_channels(
        settings["baninfo_channels"],
        f"FLOOD {appeal['user_id']} {appeal['guild_id']}",
    )
    asyncio.create_task(_confirm_flood_ban_later())

async def handle_appeal_dm_message(message: discord.Message):
    """DM side of an appeal bridge: relay the appellant's direct messages into
    their appeal thread. DMs from users without an open appeal are ignored."""
    if message.author.bot:
        return
    appeal = db.get_open_appeal(message.author.id)
    if not appeal:
        return
    if db.is_shadow_banned("discord", str(message.author.id)):
        return
    count = db.count_appeal_message(appeal)
    if count == 20:
        try:
            await message.author.send(localized("appeal_flood_warning", appeal["lang"]))
        except Exception:
            pass
    if count >= 30:
        if rate_limit_ok(("appeal-flood-signal", str(message.author.id)),
                             limit=1, window_seconds=3600):
            await _request_guard_flood_ban(appeal)
        return
    if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping appeal DM from %s", message.author.id)
        return
    thread_lang = _appeal_thread_lang(appeal["thread_id"])
    await _relay_verified_discord_message(
        message, appeal["bridge_id"],
        origin_chat_id=f"dm:{message.author.id}",
        place_name=localized("appeal_dm_place", thread_lang),
    )

async def handle_appeal_thread_message(message: discord.Message, appeal_row, bridge_id):
    """Thread side of an appeal bridge: relay server members' messages to the
    appellant's DM, anonymized as 'Consul A/B/…'. No consent prompt — writing
    in an appeal thread is a moderation duty, not a bridged community chat.
    The appellant's own thread messages (they can see their thread if channel
    permissions allow) are not relayed back to their DM."""
    if appeal_row["status"] != "open":
        return
    if str(message.author.id) == str(appeal_row["user_id"]):
        return
    if db.is_shadow_banned("discord", str(message.author.id)):
        try:
            await message.delete()
        except Exception:
            pass
        return
    if _can_judge_appeals(message.author):
        db.reset_appeal_flood(appeal_row["user_id"])
    if not rate_limit_ok(("relay", "discord", str(message.author.id)), limit=20, window_seconds=60):
        logger.warning("Rate limit: dropping appeal thread message from %s", message.author.id)
        return
    await _relay_verified_discord_message(
        message, bridge_id,
        sender_name=_consul_label(message.channel.id, message.author.id),
        avatar_url="",
    )
