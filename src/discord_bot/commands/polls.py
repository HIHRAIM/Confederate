"""Bridge-wide anonymous polls: the /poll command, the vote buttons, and the
publishing/results/teardown helpers shared with the Telegram side.

A poll lives once in the database and is posted as an interactive message
into every chat of the bridge; votes from either platform land in the same
poll_votes rows. Results are posted by main.py's poll_loop when the poll
expires; deleting any of the poll messages closes the poll everywhere
(relay.process_discord_message_delete calls close_and_delete_poll).
"""
import json
import logging
import time

import discord
from discord import app_commands

import db
from message_relay import clip_text, DISCORD_MSG_LIMIT
from utils import get_chat_lang, localized

from discord_bot.client import bot
from discord_bot.relay import RELAY_ALLOWED_MENTIONS, _esc_md

logger = logging.getLogger("bridge.discord")

POLL_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def _poll_emoji(idx):
    """Number emoji for an option index, plain '11.' past the tenth."""
    return POLL_NUMBER_EMOJI[idx] if idx < len(POLL_NUMBER_EMOJI) else f"{idx + 1}."

def _poll_start_text_discord(question, options, ends_at, lang):
    """The Discord rendering of a poll: question, numbered options, relative
    end time as native <t:…:R> markup."""
    lines = [f"📊 **{question}**", f"-# {localized('poll_anonymous', lang)}", ""]
    for i, opt in enumerate(options):
        lines.append(f"{_poll_emoji(i)} {opt}")
    lines.append("")
    lines.append(localized("poll_ends", lang, ends=f"<t:{ends_at}:R>"))
    return "\n".join(lines)

def _poll_relay_header(origin_platform, place, nick, target_platform):
    """First line shown when a poll is relayed to other chats — like a forwarded
    message header (normal text). Markdown in the names is escaped on Discord."""
    messenger = "Discord" if origin_platform == "discord" else "Telegram"
    if target_platform == "discord":
        return f"[{_esc_md(messenger)} | {_esc_md(place or '')}] {_esc_md(nick or '')}"
    return f"[{messenger} | {place or ''}] {nick or ''}"

def _format_poll_results(question, options, counts, total, lang):
    """The results text: per-option counts with percentages and the total."""
    lines = [localized("poll_results_header", lang, question=question), ""]
    for i, opt in enumerate(options):
        c = counts[i]
        pct = round(c / total * 100) if total else 0
        lines.append(f"{_poll_emoji(i)} {opt} — {c} ({pct}%)")
    lines.append("")
    lines.append(localized("poll_total_votes", lang, total=total))
    return "\n".join(lines)

class PollButton(discord.ui.Button):
    """One vote button per option. Stable custom_id 'poll:<id>:<idx>' so the
    button works across restarts (the views are re-added in setup_hook)."""

    def __init__(self, poll_id, idx, option):
        """Label is the clipped option text, emoji its number."""
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=clip_text(option, 80) or str(idx + 1),
            emoji=(POLL_NUMBER_EMOJI[idx] if idx < len(POLL_NUMBER_EMOJI) else None),
            custom_id=f"poll:{poll_id}:{idx}",
        )
        self.poll_id = poll_id
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        """Record the click as a vote."""
        await handle_discord_poll_vote(interaction, self.poll_id, self.idx)

class PollView(discord.ui.View):
    """The button row of one poll message. Persistent (timeout=None)."""

    def __init__(self, poll_id, options):
        """One PollButton per option."""
        super().__init__(timeout=None)
        for idx, opt in enumerate(options):
            self.add_item(PollButton(poll_id, idx, opt))

async def handle_discord_poll_vote(interaction: discord.Interaction, poll_id, idx):
    """Register a Discord vote: the poll must still be open and the voter
    verified (the vote crosses community lines like a message would).
    Re-voting simply replaces the previous choice."""
    lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}") or "en"
    poll = db.get_poll(poll_id)
    if not poll or poll["closed"] or (poll["ends_at"] and poll["ends_at"] <= int(time.time())):
        await interaction.response.send_message(localized("poll_closed", lang), ephemeral=True)
        return
    user_id = str(interaction.user.id)
    if not db.is_user_verified("discord", user_id, str(interaction.guild_id)):
        await interaction.response.send_message(localized("poll_not_verified", lang), ephemeral=True)
        return
    db.record_poll_vote(poll_id, "discord", user_id, idx)
    await interaction.response.send_message(localized("poll_vote_recorded", lang), ephemeral=True)

async def publish_poll(poll_id, bridge_id, question, options, ends_at, *,
                       origin_chat_id, origin_platform, origin_place, origin_nick,
                       skip_chat_id=None):
    """Post the interactive poll message to every chat in the bridge. The origin
    chat gets no header; every other chat is prefixed with a forwarded-message
    header showing the origin platform, community and creator. `skip_chat_id` is
    skipped entirely (used when the origin Discord message is the command response)."""
    try:
        from telegram_bot import bot as tg_bot, build_poll_keyboard, poll_start_text_telegram
    except Exception:
        tg_bot = None

    for chat in db.get_bridge_chats(bridge_id):
        if skip_chat_id and chat["chat_id"] == skip_chat_id:
            continue
        lang = get_chat_lang(chat["chat_id"]) or "en"
        is_origin = chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id
        header = None if is_origin else _poll_relay_header(origin_platform, origin_place, origin_nick, chat["platform"])

        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if channel is None:
                continue
            body = _poll_start_text_discord(question, options, ends_at, lang)
            content = body if header is None else f"{header}\n{body}"
            try:
                msg = await channel.send(
                    content, view=PollView(poll_id, options),
                    allowed_mentions=RELAY_ALLOWED_MENTIONS,
                )
                db.add_poll_message(poll_id, "discord", chat["chat_id"], msg.id)
            except Exception as e:
                logger.warning("poll post to discord %s failed: %s", chat["chat_id"], e)
        elif chat["platform"] == "telegram" and tg_bot is not None:
            try:
                tg_chat_id, thread = chat["chat_id"].split(":")
                body = poll_start_text_telegram(question, options, ends_at, lang)
                text = body if header is None else f"{header}\n{body}"
                sent = await tg_bot.send_message(
                    int(tg_chat_id), text,
                    message_thread_id=int(thread) or None,
                    reply_markup=build_poll_keyboard(poll_id, options),
                )
                db.add_poll_message(poll_id, "telegram", chat["chat_id"], sent.message_id)
            except Exception as e:
                logger.warning("poll post to telegram %s failed: %s", chat["chat_id"], e)

async def post_poll_results(poll_id):
    """Post the final counts into every chat of the bridge, each as a reply to
    that chat's own poll message where it still exists (so readers can see
    what was voted on). Called by main.py's poll_loop on expiry."""
    poll = db.get_poll(poll_id)
    if not poll:
        return
    options = json.loads(poll["options"])
    counts = db.get_poll_results(poll_id, len(options))
    total = sum(counts)
    starts = {(m["platform"], m["chat_id"]): m["message_id"] for m in db.get_poll_messages(poll_id)}
    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None

    for chat in db.get_bridge_chats(poll["bridge_id"]):
        lang = get_chat_lang(chat["chat_id"]) or "en"
        text = _format_poll_results(poll["question"], options, counts, total, lang)
        start_mid = starts.get((chat["platform"], chat["chat_id"]))
        if chat["platform"] == "discord":
            channel_id = int(chat["chat_id"].split(":")[1])
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if channel is None:
                continue
            send_kwargs = {"allowed_mentions": RELAY_ALLOWED_MENTIONS}
            if start_mid:
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=int(start_mid), channel_id=channel_id, fail_if_not_exists=False
                )
            try:
                await channel.send(clip_text(text, DISCORD_MSG_LIMIT), **send_kwargs)
            except Exception:
                pass
        elif chat["platform"] == "telegram" and tg_bot is not None:
            tg_chat_id, thread = chat["chat_id"].split(":")
            kw = dict(chat_id=int(tg_chat_id), message_thread_id=int(thread) or None, text=text)
            if start_mid:
                kw["reply_to_message_id"] = int(start_mid)
            try:
                await tg_bot.send_message(**kw)
            except Exception:
                kw.pop("reply_to_message_id", None)
                try:
                    await tg_bot.send_message(**kw)
                except Exception:
                    pass

async def close_and_delete_poll(poll_id):
    """Close a poll and delete its message in every chat (triggered when a copy is deleted)."""
    poll = db.get_poll(poll_id)
    if not poll:
        return
    db.close_poll(poll_id)
    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    for m in db.get_poll_messages(poll_id):
        try:
            if m["platform"] == "discord":
                channel_id = int(m["chat_id"].split(":")[1])
                ch = bot.get_channel(channel_id)
                if ch is None:
                    ch = await bot.fetch_channel(channel_id)
                msg = await ch.fetch_message(int(m["message_id"]))
                await msg.delete()
            elif m["platform"] == "telegram" and tg_bot is not None:
                tg_chat_id, _ = m["chat_id"].split(":")
                await tg_bot.delete_message(int(tg_chat_id), int(m["message_id"]))
        except Exception:
            pass
    db.delete_poll(poll_id)

@bot.tree.command(name="poll", description="anonymous poll across all bridge chats")
@app_commands.describe(
    text="Poll question",
    duration="Duration: 1h, 2d, … (max 30 days)",
    option1="Option 1", option2="Option 2",
    option3="Option 3 (optional)", option4="Option 4 (optional)", option5="Option 5 (optional)",
)
async def poll_cmd(interaction: discord.Interaction, text: str, duration: str,
                   option1: str, option2: str, option3: str = None,
                   option4: str = None, option5: str = None):
    """Create a bridge-wide poll. The command response itself is the origin
    chat's poll message (hence skip_chat_id when publishing to the rest).
    Anyone may create one — voting, not creating, is the gated action."""
    chat_key = f"{interaction.guild_id}:{interaction.channel_id}"
    lang = get_chat_lang(chat_key) or "en"

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await interaction.response.send_message(localized("poll_not_in_bridge", lang), ephemeral=True)
        return
    bridge_id = row["bridge_id"]

    from utils import parse_poll_duration
    try:
        seconds = parse_poll_duration(duration)
    except ValueError:
        await interaction.response.send_message(localized("poll_duration_invalid", lang), ephemeral=True)
        return

    options = [o.strip() for o in (option1, option2, option3, option4, option5) if o and o.strip()]
    if len(options) < 2:
        await interaction.response.send_message(localized("poll_too_few", lang), ephemeral=True)
        return

    ends_at = int(time.time()) + seconds
    poll_id = db.create_poll(bridge_id, text.strip(), json.dumps(options, ensure_ascii=False), ends_at)

    await interaction.response.send_message(
        _poll_start_text_discord(text.strip(), options, ends_at, lang),
        view=PollView(poll_id, options),
        allowed_mentions=RELAY_ALLOWED_MENTIONS,
    )
    try:
        origin_msg = await interaction.original_response()
        db.add_poll_message(poll_id, "discord", chat_key, origin_msg.id)
    except Exception:
        pass

    place = interaction.guild.name if interaction.guild else "Discord"
    nick = interaction.user.display_name
    await publish_poll(
        poll_id, bridge_id, text.strip(), options, ends_at,
        origin_chat_id=chat_key, origin_platform="discord",
        origin_place=place, origin_nick=nick, skip_chat_id=chat_key,
    )
