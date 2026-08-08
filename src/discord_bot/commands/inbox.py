"""Slash commands of the inbox system: registering receiver bots, naming the
chats they report into, anonymizing staff, closing conversations and banning
people from one bot.

The Telegram twins live in telegram_bot/commands/inbox.py and behave
identically; everything they share — permission checks, token registration,
conversation lookup — is in inbox.py, so the two command layers stay thin
translations of the same operations into their platform's shape.

Who may do what: a Bot Admin registers the first token, and from then on the
bot answers to them *and* to whoever handed that token in (inbox.py:
can_manage_inbox_bot). `/close` is looser on purpose — closing a
conversation is routine support work, so the chat's own admins may do it too.
"""
import discord
from discord import app_commands

import db
from utils import get_chat_lang, is_admin, is_chat_admin, localized

from discord_bot.client import bot

def _lang(interaction: discord.Interaction):
    """The language to answer this interaction in."""
    return get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"

def _host_key(interaction: discord.Interaction):
    """The key a host row is written under: the *channel*, never a thread.

    A conversation opens as a thread of the host, and a thread cannot hold
    threads of its own — so someone running /setinboxchat inside one means
    the channel around it. Resolving that here is friendlier than refusing,
    and it keeps /reminboxchat working from anywhere in the channel."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return f"{interaction.guild_id}:{channel.parent_id}"
    return f"{interaction.guild_id}:{interaction.channel_id}"

def _can_open_threads(interaction: discord.Interaction, host_key):
    """Whether the bot may actually open threads in the channel it is being
    pointed at.

    Checked when the host is configured, not left to surface later as a
    conversation that silently will not open. A forum channel needs the same
    right under a different name, and both need somewhere to post the pinned
    first message."""
    channel = interaction.guild.get_channel(int(host_key.split(":")[1])) if interaction.guild else None
    if channel is None:
        return False
    perms = channel.permissions_for(interaction.guild.me)
    if isinstance(channel, discord.ForumChannel):
        return perms.create_public_threads and perms.send_messages_in_threads
    return perms.create_public_threads and perms.send_messages_in_threads and perms.send_messages

def _resolve_managed_bot(interaction: discord.Interaction, identifier):
    """Find the receiver bot a command is about and check the caller may
    manage it.

    Returns ``(bot_row, error_key)``. With no identifier the current chat is
    asked instead (inbox.py: inbox_bot_for_chat), which is what makes the
    commands usable from inside a conversation thread without typing ids."""
    from inbox import can_manage_inbox_bot, inbox_bot_for_chat

    if identifier and identifier.strip():
        bot_row = db.find_inbox_bot(identifier)
    else:
        bot_row = (inbox_bot_for_chat(f"{interaction.guild_id}:{interaction.channel_id}")
                   or inbox_bot_for_chat(_host_key(interaction)))
    if bot_row is None:
        return None, "inbox_unknown_bot"
    if not can_manage_inbox_bot(bot_row, "discord", interaction.user.id):
        return None, "no_permission"
    return bot_row, None

@bot.tree.command(name="setinbox", description="register a Telegram bot whose DMs this bot bridges (bot admins)")
@app_commands.describe(token="the receiver bot's token — pass it again later to rotate it")
async def setinbox(interaction: discord.Interaction, token: str):
    """Register a receiver bot, or replace the token of one already known.

    Bot Admins register; afterwards the bot's own registrant may rotate its
    token too. The answer is ephemeral and never repeats the token, which is
    validated against Telegram, encrypted and only then written down. Both
    permission cases are decided inside register_inbox_bot, which is the
    first place that knows whose token this is."""
    from inbox import inbox_bot_place_name, register_inbox_bot

    lang = _lang(interaction)
    await interaction.response.defer(ephemeral=True)
    bot_row, error_key, was_update = await register_inbox_bot(
        token.strip(), "discord", interaction.user.id
    )
    if error_key and bot_row is None:
        await interaction.followup.send(localized(error_key, lang), ephemeral=True)
        return

    name = inbox_bot_place_name(bot_row)
    if error_key:
        await interaction.followup.send(localized(error_key, lang, bot=name), ephemeral=True)
        return
    await interaction.followup.send(
        localized("setinbox_updated" if was_update else "setinbox_added", lang, bot=name),
        ephemeral=True,
    )

@bot.tree.command(name="reminbox", description="unregister a receiver bot and close its conversations")
@app_commands.describe(receiver_bot="the receiver bot's id or @username")
async def reminbox(interaction: discord.Interaction, receiver_bot: str = None):
    """Take a receiver bot out of service entirely: its open conversations are
    closed, its polling stops and its rows go."""
    from inbox import inbox_bot_place_name, unregister_inbox_bot

    lang = _lang(interaction)
    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    name = inbox_bot_place_name(bot_row)
    await unregister_inbox_bot(bot_row)
    await interaction.followup.send(localized("reminbox_done", lang, bot=name), ephemeral=True)

@bot.tree.command(name="setinboxchat", description="open this channel to a receiver bot's conversations")
@app_commands.describe(receiver_bot="the receiver bot's id or @username")
async def setinboxchat(interaction: discord.Interaction, receiver_bot: str):
    """Make this channel one of the places a receiver bot reports into: every
    person writing to it gets a thread here.

    Naming a second chat — here or on Telegram — is how a conversation grows
    past two chats: one incoming private chat then reaches a Discord thread
    and a Telegram topic at once, all in one bridge."""
    from inbox import inbox_bot_place_name

    lang = _lang(interaction)
    host_key = _host_key(interaction)
    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    name = inbox_bot_place_name(bot_row)
    if db.get_inbox_host(bot_row["bot_id"], host_key):
        await interaction.response.send_message(
            localized("setinboxchat_already", lang, bot=name), ephemeral=True
        )
        return

    if not _can_open_threads(interaction, host_key):
        await interaction.response.send_message(
            localized("setinboxchat_no_thread_rights", lang), ephemeral=True
        )
        return

    db.add_inbox_host(bot_row["bot_id"], "discord", host_key, interaction.user.id)
    await interaction.response.send_message(
        localized("setinboxchat_done", lang, bot=name), ephemeral=True
    )

@bot.tree.command(name="reminboxchat", description="stop opening a receiver bot's conversations in this channel")
@app_commands.describe(receiver_bot="the receiver bot's id or @username — optional when this chat hosts one")
async def reminboxchat(interaction: discord.Interaction, receiver_bot: str = None):
    """Stop this channel from hosting new conversations. Threads already open
    keep working until their conversation closes — they are ordinary bridge
    chats by then, and cutting them off would strand whoever is mid-sentence."""
    from inbox import inbox_bot_place_name

    lang = _lang(interaction)
    host_key = _host_key(interaction)
    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    name = inbox_bot_place_name(bot_row)
    if not db.remove_inbox_host(bot_row["bot_id"], host_key):
        await interaction.response.send_message(
            localized("reminboxchat_not_host", lang, bot=name), ephemeral=True
        )
        return
    await interaction.response.send_message(
        localized("reminboxchat_done", lang, bot=name), ephemeral=True
    )

@bot.tree.command(name="inboxanon", description="hide staff names from the people writing to a receiver bot")
@app_commands.describe(
    state="enable or disable",
    receiver_bot="the receiver bot's id or @username — optional when this chat names one",
)
@app_commands.choices(state=[
    app_commands.Choice(name="enable", value="enable"),
    app_commands.Choice(name="disable", value="disable"),
])
async def inboxanon(interaction: discord.Interaction, state: str, receiver_bot: str = None):
    """Turn staff anonymization on or off for one receiver bot.

    With it on, everyone answering from a host chat is signed 'Staff A',
    'Staff B', … instead of by name — a letter each, stable for as long as
    the conversation lasts. It applies from the next message on; copies
    already delivered are not rewritten."""
    from inbox import inbox_bot_place_name

    lang = _lang(interaction)
    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    enabled = state.strip().lower() == "enable"
    db.set_inbox_anonymize(bot_row["bot_id"], enabled)
    await interaction.response.send_message(
        localized(
            "inboxanon_enabled" if enabled else "inboxanon_disabled", lang,
            bot=inbox_bot_place_name(bot_row),
        ),
        ephemeral=True,
    )

@bot.tree.command(name="inboxlist", description="list the registered receiver bots")
async def inboxlist(interaction: discord.Interaction):
    """The registered receiver bots with their state, host chats and open
    conversations. Bot Admins see all of them; anyone else sees the ones they
    registered themselves."""
    from inbox import inbox_bot_instance, inbox_bot_place_name

    lang = _lang(interaction)
    rows = db.get_inbox_bots()
    if not is_admin("discord", interaction.user.id):
        rows = [r for r in rows
                if r["owner_platform"] == "discord" and str(r["owner_id"]) == str(interaction.user.id)]
    if not rows:
        await interaction.response.send_message(localized("inboxlist_empty", lang), ephemeral=True)
        return

    lines = []
    for row in rows:
        hosts = db.get_inbox_hosts(row["bot_id"])
        host_names = ", ".join(_host_display(h) for h in hosts) or localized("inboxlist_no_hosts", lang)
        lines.append(localized(
            "inboxlist_entry", lang,
            bot=inbox_bot_place_name(row),
            id=row["bot_id"],
            state=localized(
                "inboxlist_state_online" if inbox_bot_instance(row["bot_id"]) else "inboxlist_state_offline",
                lang,
            ),
            anon=localized("inboxlist_anon_on" if row["anonymize"] else "inboxlist_anon_off", lang),
            hosts=host_names,
            conversations=len(db.get_inbox_conversations_of_bot(row["bot_id"])),
        ))

    embed = discord.Embed(
        title=localized("inboxlist_title", lang),
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

def _host_display(host_row):
    """A host chat as one short line for /inboxlist: the channel mention on
    Discord, the raw key on Telegram (where there is nothing to mention)."""
    if host_row["platform"] == "discord":
        try:
            return f"<#{host_row['chat_id'].split(':')[1]}>"
        except Exception:
            return host_row["chat_id"]
    return f"Telegram {host_row['chat_id']}"

@bot.tree.command(name="close", description="close the inbox conversation of this thread")
async def close_conversation(interaction: discord.Interaction):
    """Close the conversation this thread belongs to: its title goes ⬛, both
    sides are told, the thread is archived and locked, the topic on the other
    platform is closed, and the bridge goes.

    Named without the `inbox` prefix the configuration commands carry, because
    this one is not configuration: it is run inside a conversation, by the
    people answering it, as part of the day's work."""
    from inbox import can_manage_inbox_bot, close_inbox_conversation, inbox_conversation_of_chat

    lang = _lang(interaction)
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    conv = inbox_conversation_of_chat(chat_key)
    if conv is None:
        await interaction.response.send_message(
            localized("close_not_conversation", lang), ephemeral=True
        )
        return

    bot_row = db.get_inbox_bot(conv["bot_id"])
    if not (can_manage_inbox_bot(bot_row, "discord", interaction.user.id)
            or is_chat_admin("discord", chat_key, interaction.user.id)):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    await interaction.response.send_message(localized("close_done", lang), ephemeral=True)
    await close_inbox_conversation(conv)

def _can_set_header(interaction: discord.Interaction, chat_key):
    """Bot Admins, and the Bridge Admins of the chat the command was run in.

    Deliberately narrower than `is_chat_admin`, which would also let a Local
    Admin through: this changes how every relayed message in the community's
    conversations reads, which is the bridge's business rather than the
    control panel's."""
    if is_admin("discord", interaction.user.id):
        return True
    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if row and str(interaction.user.id) in db.get_bridge_admins(row["bridge_id"]):
        return True
    return db.is_server_bridge_admin("discord", str(interaction.guild_id), interaction.user.id)

@bot.tree.command(name="close-header", description="hide the relay header in this server's inbox conversations")
@app_commands.describe(
    state="hide or show",
    receiver_bot="the receiver bot's id or @username — optional inside a conversation thread",
)
@app_commands.choices(state=[
    app_commands.Choice(name="hide", value="hide"),
    app_commands.Choice(name="show", value="show"),
])
async def close_header(interaction: discord.Interaction, state: str, receiver_bot: str = None):
    """Drop the ``[Telegram | DM] Name:`` line from the copies a receiver
    bot's conversations deliver into this server.

    A conversation thread is one person talking to one team, so the header
    repeats what the thread already says. Scoped to this server and this
    receiver bot: another community hosting the same bot keeps its own
    answer. Only the staff side is affected — the writer's own copies carry
    just a name either way.

    Bot Admins and Bridge Admins. Run it in a conversation thread or anywhere
    in the host channel; it takes effect from the next message on."""
    from inbox import inbox_bot_place_name

    lang = _lang(interaction)
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    if not _can_set_header(interaction, chat_key):
        await interaction.response.send_message(localized("no_permission", lang), ephemeral=True)
        return

    bot_row = None
    if receiver_bot and receiver_bot.strip():
        bot_row = db.find_inbox_bot(receiver_bot)
    else:
        from inbox import inbox_bot_for_chat
        bot_row = inbox_bot_for_chat(chat_key) or inbox_bot_for_chat(_host_key(interaction))
    if bot_row is None:
        await interaction.response.send_message(localized("inbox_unknown_bot", lang), ephemeral=True)
        return

    host = db.get_inbox_host_of_community(bot_row["bot_id"], "discord", chat_key)
    if host is None:
        await interaction.response.send_message(
            localized("close_header_not_host", lang, bot=inbox_bot_place_name(bot_row)),
            ephemeral=True,
        )
        return

    hidden = state.strip().lower() == "hide"
    db.set_inbox_header_hidden(bot_row["bot_id"], host["chat_id"], hidden)
    await interaction.response.send_message(
        localized(
            "close_header_hidden" if hidden else "close_header_shown", lang,
            bot=inbox_bot_place_name(bot_row),
        ),
        ephemeral=True,
    )

@bot.tree.command(name="inboxban", description="bar a user from writing to a receiver bot")
@app_commands.describe(
    user="the user's Telegram id or @username — omit inside their conversation thread",
    receiver_bot="the receiver bot's id or @username — omit inside a conversation thread",
)
async def inboxban(interaction: discord.Interaction, user: str = None, receiver_bot: str = None):
    """Ban someone from one receiver bot. Their conversation is closed and
    everything they send that bot afterwards is dropped.

    Run inside their conversation thread it needs no arguments at all — the
    thread names both the bot and the person. The ban is scoped to this bot
    alone; `/shadow-ban` is the bot-wide instrument."""
    from inbox import ban_inbox_user, inbox_conversation_of_chat, resolve_inbox_user

    lang = _lang(interaction)
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    conv = inbox_conversation_of_chat(chat_key)

    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if user and user.strip():
        target = await resolve_inbox_user(bot_row["bot_id"], user)
    elif conv is not None:
        target = conv["user_id"]
    else:
        target = None
    if target is None:
        await interaction.followup.send(localized("inboxban_usage", lang), ephemeral=True)
        return

    if db.is_inbox_banned(bot_row["bot_id"], target):
        await interaction.followup.send(localized("inboxban_already", lang), ephemeral=True)
        return

    await ban_inbox_user(bot_row, target, interaction.user.id)
    await interaction.followup.send(localized("inboxban_done", lang, user=target), ephemeral=True)

@bot.tree.command(name="inboxunban", description="let a banned user write to a receiver bot again")
@app_commands.describe(
    user="the user's Telegram id or @username — omit inside their conversation thread",
    receiver_bot="the receiver bot's id or @username — omit inside a conversation thread",
)
async def inboxunban(interaction: discord.Interaction, user: str = None, receiver_bot: str = None):
    """Lift a ban. Nothing reopens by itself: the user's next message to the
    bot starts a new conversation."""
    from inbox import inbox_conversation_of_chat, resolve_inbox_user

    lang = _lang(interaction)
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    conv = inbox_conversation_of_chat(chat_key)

    bot_row, error_key = _resolve_managed_bot(interaction, receiver_bot)
    if error_key:
        await interaction.response.send_message(localized(error_key, lang), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if user and user.strip():
        target = await resolve_inbox_user(bot_row["bot_id"], user)
    elif conv is not None:
        target = conv["user_id"]
    else:
        target = None
    if target is None:
        await interaction.followup.send(localized("inboxunban_usage", lang), ephemeral=True)
        return

    if not db.remove_inbox_ban(bot_row["bot_id"], target):
        await interaction.followup.send(localized("inboxunban_not_banned", lang), ephemeral=True)
        return
    await interaction.followup.send(localized("inboxunban_done", lang, user=target), ephemeral=True)
