"""The sponsorship commands on Telegram, mirroring the Discord ones.

Both spellings of every name are accepted (`/sponsor_claim` and
`/sponsor-claim`), as everywhere else on this side. The rules live in
src/sponsors.py and the status text is rendered by the Discord module's
`sponsor_status_text`, so the two platforms cannot drift into saying
different things about the same subscription.

`/add_discord` is the Telegram half of the linking handshake; the other half
is `/add-telegram` on Discord and neither works alone.
"""
from aiogram.filters import Command
from aiogram.types import Message

import db
import sponsors
from config import SPONSOR_URL
from utils import get_chat_lang, is_admin, is_chat_admin, localized

from telegram_bot.client import is_telegram_native_admin, router

def _chat_key(message):
    """This bot's chat key for the group topic a command was run in."""
    return f"{message.chat.id}:{message.message_thread_id or 0}"

async def _is_community_admin(message):
    """Whether the caller may speak for this Telegram group.

    Telegram's own administrator status counts, for the reason it does on
    Discord: claiming a community happens before this bot has any grant of
    its own there."""
    user_id = message.from_user.id
    if is_admin("telegram", user_id):
        return True
    if await is_telegram_native_admin(message.chat.id, user_id):
        return True
    return is_chat_admin("telegram", _chat_key(message), user_id)

def _argument(message):
    """Everything after the command word, stripped, or ''."""
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

@router.message(Command("add_discord", "add-discord"))
async def add_discord_cmd(message: Message):
    """Start linking your Discord account: name your Discord username here,
    then run `/add-telegram` there within thirty minutes naming your Telegram
    @username.

    A Telegram account with no @username cannot take part: the Discord half
    of the handshake has nothing to name it by. The link is what lends this
    account the sponsor status bought on Discord, and the pair then shares
    one set of rights, not one each."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if not message.from_user.username:
        await message.reply(localized("link_need_username", lang))
        return
    discord_nick = _argument(message).lstrip("@")
    if not discord_nick:
        await message.reply(localized("link_usage_telegram", lang))
        return

    result, _discord_id, _telegram_id = db.register_link_attempt(
        "telegram", message.from_user.id, message.from_user.username, discord_nick)
    key = "link_success" if result == "linked" else "link_pending_telegram"
    await message.reply(localized(key, lang, nick=discord_nick))

@router.message(Command("unlink_accounts", "unlink-accounts"))
async def unlink_accounts_cmd(message: Message):
    """Break the Discord <-> Telegram link, from the Telegram side.

    Immediate: this account's sponsor rights came through the link and go
    with it. The Discord account keeps its subscription, its communities and
    its bridges."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    row = db.unlink_accounts("telegram", message.from_user.id)
    await message.reply(localized("unlink_done" if row else "unlink_none", lang))

@router.message(Command("sponsor"))
async def sponsor_cmd(message: Message):
    """Show what your subscription currently entitles you to."""
    from discord_bot.commands.sponsors import sponsor_status_text

    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    overview = sponsors.sponsor_overview("telegram", message.from_user.id)
    if overview is None:
        await message.reply(localized("sponsor_none", lang))
        return
    await message.reply(sponsor_status_text(overview, lang))

@router.message(Command("sponsor_claim", "sponsor-claim"))
async def sponsor_claim_cmd(message: Message):
    """Take one of your community slots for this group.

    A group is one community however many topics it has: the slot is charged
    against the group's own id, with no topic number attached to it."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.reply(localized("sponsor_claim_not_a_community", lang))
        return
    if not await _is_community_admin(message):
        await message.reply(localized("sponsor_claim_not_admin", lang))
        return

    server_id = str(message.chat.id)
    status, extra = sponsors.community_slot_status(
        "telegram", message.from_user.id, server_id)
    if status != "ok":
        await message.reply(localized(f"sponsor_claim_{status}", lang, **extra))
        return

    db.claim_community("telegram", server_id, extra["discord_id"])
    await message.reply(localized("sponsor_claim_ok", lang, used=extra["used"] + 1,
                                  limit=extra["limit"]))

@router.message(Command("sponsor_release", "sponsor-release"))
async def sponsor_release_cmd(message: Message):
    """Give a community slot back — this group, or the community whose id is
    named as the argument.

    Nothing in the community is undone; only the sponsor's standing rights
    there end, and the next claim has to wait out the rotation limit."""
    lang = get_chat_lang(_chat_key(message))
    if not message.from_user:
        return
    discord_id = db.canonical_sponsor_id("telegram", message.from_user.id)
    if not discord_id:
        await message.reply(localized("sponsor_none", lang))
        return

    target = _argument(message) or str(message.chat.id)
    released = False
    for platform in ("discord", "telegram"):
        if db.release_community(platform, target, discord_id):
            released = True
    if not released:
        await message.reply(localized("sponsor_release_none", lang))
        return

    db.touch_sponsor_slot_change(discord_id)
    remaining = len(db.get_sponsor_communities(discord_id))
    await message.reply(localized(
        "sponsor_release_ok", lang, server_id=target, used=remaining,
        days=sponsors.SPONSOR_SLOT_CHANGE_SECONDS // 86400))

@router.message(Command("sponsor_subscribe", "sponsor-subscribe"))
async def sponsor_subscribe_cmd(message: Message):
    """Explain both tiers and how to get one, including the step everybody
    trips over: connecting Patreon to Discord, without which no role is
    granted and the bot can see nothing at all."""
    lang = get_chat_lang(_chat_key(message))
    tier1 = sponsors.sponsor_limits(1)
    tier2 = sponsors.sponsor_limits(2)
    await message.reply(localized(
        "sponsor_subscribe", lang, url=SPONSOR_URL,
        communities1=tier1["communities"], wikis1=tier1["wikis"],
        socials1=tier1["socials"], communities2=tier2["communities"],
        wikis2=tier2["wikis"], socials2=tier2["socials"]))
