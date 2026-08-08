"""User-facing Telegram commands: consent (/verify and its callback,
/unverify), /shadow-ban, /whois, /mention, /privacy (menu + callback) and
/help.

Telegram has no ephemeral replies, so the lookups here answer with a message
that deletes itself after a minute; the Discord twins use ephemeral
interactions for the same purpose.
"""
import asyncio
import logging

from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import db
from message_relay import escape_html
from utils import (
    DEFAULT_LANG, get_chat_lang, is_admin, localized, localized_consent_body,
    localized_consent_button, localized_consent_title, localized_help,
    localized_whois, rate_limit_ok,
)

from telegram_bot.client import (
    _telegram_html_mention, bot, get_telegram_avatar_url, resolve_telegram_user, router,
)

logger = logging.getLogger("bridge.telegram")

@router.callback_query(lambda c: c.data and c.data.startswith("verify:"))
async def handle_verify_callback(query: CallbackQuery):
    """
    Expected callback_data: verify:telegram|<prefix>|<user_id>
    Only the target user can confirm. On confirm — add verified and remove pending + bot message.

    One click answers every community's outstanding prompt for this user, and
    each held first message is replayed from its serialized payload.
    """
    from telegram_bot.relay import _relay_serialized_telegram_payload

    data = query.data
    if query.message:
        lang = get_chat_lang(f"{query.message.chat.id}:{query.message.message_thread_id or 0}")
    else:
        lang = DEFAULT_LANG
    try:
        _, payload = data.split(":", 1)
        parts = payload.split("|")
        platform = parts[0]
        prefix = parts[1]
        target_user_id = parts[2]
    except Exception:
        await query.answer(localized("verify_invalid_data", lang), show_alert=True)
        return

    if str(query.from_user.id) != str(target_user_id):
        await query.answer(localized("verify_button_not_yours", lang), show_alert=True)
        return

    if not db.get_pending_consent("telegram", prefix, target_user_id):
        await query.answer(localized("verify_invalid_data", lang), show_alert=True)
        return

    db.add_verified_user("telegram", target_user_id, prefix, days_valid=365)

    all_pendings = db.get_all_pending_consents_for_user("telegram", target_user_id)

    first_payloads = []
    for p in all_pendings:
        p_bot_msg_id = p["bot_message_id"]
        if p_bot_msg_id:
            try:
                p_chat_id_str, p_th = p["chat_key"].split(":")
                await bot.delete_message(chat_id=int(p_chat_id_str), message_id=int(p_bot_msg_id))
            except Exception:
                pass
        p_payload = p["first_message_payload"] if "first_message_payload" in p.keys() else None
        if p_payload:
            first_payloads.append(p_payload)
        db.remove_pending_consent("telegram", p["prefix"], target_user_id)

    for payload in first_payloads:
        await _relay_serialized_telegram_payload(payload)

    await query.answer(localized("verify_thanks", lang), show_alert=False)

@router.message(Command("verify"))
async def verify_cmd(message: Message):
    """Ask for one's own consent prompt without posting a message first;
    replaces any prompt already standing in this community."""
    thread = message.message_thread_id or 0
    prefix = str(message.chat.id)
    user_id = str(message.from_user.id)
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    if not rate_limit_ok(("verify-cmd", "telegram", user_id), limit=2, window_seconds=60):
        return

    prev = db.get_pending_consent("telegram", prefix, user_id)
    if prev:
        try:
            pid_chat, pid_thread = prev["chat_key"].split(":")
            await bot.delete_message(chat_id=int(pid_chat), message_id=int(prev["bot_message_id"]))
        except Exception:
            pass
        db.remove_pending_consent("telegram", prefix, user_id)

    mention = _telegram_html_mention(message.from_user)
    consent_text = (
        f"{mention},\n"
        f"<b>{escape_html(localized_consent_title(lang))}</b>\n\n"
        f"{escape_html(localized_consent_body(lang))}"
    )
    cbdata = f"verify:telegram|{prefix}|{user_id}"

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=localized_consent_button(lang), callback_data=cbdata)]])
    try:
        sent = await bot.send_message(chat_id=int(message.chat.id), message_thread_id=int(thread) or None,
                                      text=consent_text, reply_markup=markup, parse_mode="HTML")
        db.add_pending_consent("telegram", prefix, user_id, str(sent.message_id), chat_key)
    except Exception:
        await message.reply(localized("verify_send_failed", lang))

@router.message(Command("unverify"))
async def unverify_cmd(message: Message):
    """Withdraw forwarding consent — one's own with no argument, anyone's for
    bot admins. Removes every Telegram consent row of the user."""
    lang = get_chat_lang(f"{message.chat.id}:{message.message_thread_id or 0}")
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        uid = message.from_user.id
    else:
        if not is_admin("telegram", message.from_user.id):
            await message.reply(localized("no_permission", lang))
            return
        identifier = parts[1].strip()
        if identifier.startswith("@") or not identifier.isdigit():
            uid = await resolve_telegram_user(identifier)
            if uid is None:
                await message.reply(localized("could_not_resolve_user", lang))
                return
        else:
            uid = int(identifier)

    db.cur.execute("DELETE FROM verified_users WHERE platform='telegram' AND user_id=?", (str(uid),))
    db.conn.commit()
    await message.reply(localized("unverify_done", lang, user_id=uid))

@router.message(Command("shadow-ban"))
async def shadow_ban_cmd(message: Message):
    """Shadow-ban a user (their messages are silently deleted instead of
    relayed). Bridge Admins of this chat's bridge and Bot Admins."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.reply(localized("shadowban_usage", lang))
        return
    allowed = False
    if is_admin("telegram", message.from_user.id):
        allowed = True
    else:
        row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
        if row:
            bridge_id = row["bridge_id"]
            bridge_admins = db.get_bridge_admins(bridge_id)
            if str(message.from_user.id) in bridge_admins:
                allowed = True
    if not allowed:
        await message.reply(localized("no_permission", lang))
        return

    identifier = parts[1].strip()
    uid = None
    if identifier.startswith("@") or not identifier.isdigit():
        uid = await resolve_telegram_user(identifier)
        if uid is None:
            await message.reply(localized("could_not_resolve_user", lang))
            return
    else:
        uid = int(identifier)

    db.add_shadow_ban("telegram", uid)
    await message.reply(localized("shadowban_done", lang, user_id=uid))

@router.message(Command("whois"))
async def whois_cmd(message: Message):
    """Show who wrote the relayed message this command replies to.

    Requesters must be verified themselves; senders who set hide_whois get
    the reduced answer. Every reply self-deletes after a minute — profile
    details should not linger in a group's history.

    Inside an inbox conversation only the person writing to the receiver bot
    may be looked up; staff answering them are off limits (see the Discord
    twin's docstring for why)."""
    lang = get_chat_lang(f"{message.chat.id}:{message.message_thread_id or 0}")

    async def _reply_autodelete(text: str):
        """Reply, then remove the answer after a minute."""
        sent = await message.reply(text)
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    requester_id = str(message.from_user.id) if message.from_user else ""
    if not rate_limit_ok(("whois", "telegram", requester_id), limit=5, window_seconds=60):
        return

    if not (
        is_admin("telegram", message.from_user.id if message.from_user else 0)
        or db.is_user_verified("telegram", requester_id, str(message.chat.id))
    ):
        await _reply_autodelete(localized_whois("not_verified", lang))
        return

    reply = getattr(message, "reply_to_message", None)
    replied_id = str(
        getattr(reply, "message_id", "")
        or getattr(message, "reply_to_message_id", "")
        or ""
    )

    if not replied_id.strip():
        await _reply_autodelete(localized_whois("use_reply", lang))
        return

    chat_key = f"{message.chat.id}:{message.message_thread_id or 0}"

    row = db.cur.execute(
        "SELECT message_id FROM message_copies WHERE platform=? AND chat_id=? AND message_id_platform=? LIMIT 1",
        ("telegram", chat_key, replied_id)
    ).fetchone()

    if not row:
        await _reply_autodelete(localized_whois("origin_not_found", lang))
        return

    msg_row = db.cur.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
    if not msg_row:
        await _reply_autodelete(localized_whois("origin_missing", lang))
        return

    origin_platform = msg_row["origin_platform"]
    origin_chat_id = msg_row["origin_chat_id"]
    origin_sender_id = msg_row["origin_sender_id"] if "origin_sender_id" in msg_row.keys() else ""

    if db.is_inbox_bridge(msg_row["bridge_id"]) and origin_platform != "inbox":
        await _reply_autodelete(localized_whois("inbox_writer_only", lang))
        return

    if origin_platform == "inbox":
        from inbox import inbox_whois_profile
        nickname, uname, bio = await inbox_whois_profile(origin_chat_id, origin_sender_id)
        if db.get_privacy_flag("telegram", origin_sender_id, "hide_whois"):
            await _reply_autodelete(
                localized_whois("private_template", lang, nickname=nickname, avatar="—")
            )
            return
        await _reply_autodelete(
            localized_whois(
                "tg_template", lang,
                nickname=nickname, username=uname, id=origin_sender_id, bio=bio,
            )
        )
        return

    if origin_platform == "telegram":
        try:
            prefix = origin_chat_id.split(":",1)[0]
            member = await bot.get_chat_member(int(prefix), int(origin_sender_id))
            u = member.user
            uname = f"@{u.username}" if u.username else "—"
            full = u.full_name or (u.first_name or "")
            full_user = await bot.get_chat(int(origin_sender_id))
            bio = getattr(full_user, "bio", None) or "—"
            if db.get_privacy_flag("telegram", origin_sender_id, "hide_whois"):
                await _reply_autodelete(
                    localized_whois("private_template", lang, nickname=full or "—", avatar="—")
                )
                return
            await _reply_autodelete(
                localized_whois(
                    "tg_template",
                    lang,
                    nickname=full,
                    username=uname,
                    id=u.id,
                    bio=bio
                )
            )
        except Exception as e:
            logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
            await _reply_autodelete(localized_whois("fetch_error", lang, error=type(e).__name__))
        return

    if origin_platform == "discord":
        try:
            from discord_bot import bot as dc_bot
            import discord as _discord

            guild_id = origin_chat_id.split(":", 1)[0]
            guild = dc_bot.get_guild(int(guild_id))
            member = guild.get_member(int(origin_sender_id)) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(int(origin_sender_id))
                except Exception:
                    member = None

            try:
                user_obj = await dc_bot.fetch_user(int(origin_sender_id))
            except Exception:
                user_obj = getattr(member, "user", None)

            nick = member.display_name if member else "—"
            user_name = "—"
            if user_obj:
                user_name = f"{user_obj.name}#{user_obj.discriminator}"
            elif member:
                user_name = f"{member.name}#{member.discriminator}"

            if db.get_privacy_flag("discord", origin_sender_id, "hide_whois"):
                hidden_avatar = "—"
                if user_obj and getattr(user_obj, "display_avatar", None):
                    hidden_avatar = str(user_obj.display_avatar.url)
                await _reply_autodelete(
                    localized_whois("private_template", lang, nickname=nick or "—", avatar=hidden_avatar)
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
                    custom = _discord.utils.find(
                        lambda a: isinstance(a, _discord.CustomActivity),
                        member.activities or []
                    )
                    if custom and getattr(custom, "name", None):
                        custom_status = custom.name
                except Exception:
                    custom_status = "—"

            avatar_url = "—"
            banner_url = "—"
            created_at = "—"
            if user_obj:
                if getattr(user_obj, "display_avatar", None):
                    avatar_url = str(user_obj.display_avatar.url)
                if getattr(user_obj, "banner", None):
                    banner_url = str(user_obj.banner.url)
                if getattr(user_obj, "created_at", None):
                    created_at = user_obj.created_at.strftime("%Y-%m-%d %H:%M UTC")

            await _reply_autodelete(
                localized_whois(
                    "dc_template",
                    lang,
                    nickname=nick or "—",
                    username=user_name or "—",
                    id=origin_sender_id,
                    status=custom_status,
                    mode=mode,
                    registered=created_at,
                    avatar=avatar_url,
                    banner=banner_url,
                )
            )
        except Exception as e:
            logger.warning("whois lookup failed (chat=%s): %s", chat_key, e)
            await _reply_autodelete(localized_whois("fetch_error", lang, error=type(e).__name__))
        return

    await _reply_autodelete(localized_whois("origin_not_telegram", lang))

@router.message(Command("mention"))
async def mention_cmd(message: Message):
    """Ping a Discord user of this bridge from Telegram. The name is resolved
    against the bridge's Discord guilds; the target's block_mention flag and
    an hourly per-target cooldown are honored."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(localized("mention_usage", lang))
        return

    row = db.cur.execute("SELECT bridge_id FROM chats WHERE chat_id=?", (chat_key,)).fetchone()
    if not row:
        await message.reply(localized("chat_not_in_bridge", lang))
        return
    bridge_id = row["bridge_id"]

    from discord_bot import bot as dc_bot, resolve_discord_user, send_bridge_mention

    target = parts[1].strip()
    uid = None
    if target.isdigit():
        uid = int(target)
    else:
        for c in db.get_bridge_chats(bridge_id):
            if c["platform"] != "discord":
                continue
            guild = dc_bot.get_guild(int(c["chat_id"].split(":")[0]))
            if guild is None:
                continue
            uid = await resolve_discord_user(guild, target)
            if uid is not None:
                break
    if uid is None:
        await message.reply(localized("could_not_resolve_user", lang))
        return

    if db.get_privacy_flag("discord", uid, "block_mention"):
        await message.reply(localized("mention_opted_out", lang))
        return

    if not rate_limit_ok(("mention-target", str(uid)), limit=1, window_seconds=3600):
        await message.reply(localized("mention_cooldown", lang))
        return

    sender = message.from_user.full_name if message.from_user else "Unknown"
    ok = await send_bridge_mention(
        bridge_id, "telegram", chat_key, uid,
        sender_name=sender,
        place_name=message.chat.title or "Private chat",
        messenger_name="Telegram",
        avatar_url=(await get_telegram_avatar_url(message.from_user.id)) if message.from_user else None,
    )
    if ok:
        await message.reply(localized("mention_sent", lang))
    else:
        await message.reply(localized("mention_no_discord", lang))

def _privacy_text(user_id, lang):
    """The `/privacy` menu: one line per switch, with what it does and whether
    the user has it on."""
    flags = db.get_user_privacy("telegram", user_id)
    lines = [f"<b>{escape_html(localized('privacy_title', lang))}</b>", "",
             escape_html(localized("privacy_header", lang)), ""]
    for flag in db.PRIVACY_FLAGS:
        state = localized("privacy_state_on" if flags[flag] else "privacy_state_off", lang)
        mark = "🔒" if flags[flag] else "🔓"
        lines.append(f"{mark} {escape_html(localized(f'privacy_opt_{flag}', lang))} — "
                     f"<b>{escape_html(state)}</b>")
    return "\n".join(lines)

def _privacy_keyboard(user_id, lang):
    """One toggle button per privacy flag; the owner's id travels in the
    callback data so only they can press them."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    flags = db.get_user_privacy("telegram", user_id)
    rows = []
    for flag in db.PRIVACY_FLAGS:
        mark = "🔒" if flags[flag] else "🔓"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {localized(f'privacy_btn_{flag}', lang)}",
            callback_data=f"privacy:{user_id}:{flag}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(Command("privacy"))
async def privacy_cmd(message: Message):
    """Show the caller their /privacy switches (flag meanings are documented
    on the user_privacy table in db/schema.py)."""
    lang = get_chat_lang(f"{message.chat.id}:{message.message_thread_id or 0}")
    if not message.from_user:
        return
    user_id = message.from_user.id
    await message.reply(
        _privacy_text(user_id, lang),
        parse_mode="HTML",
        reply_markup=_privacy_keyboard(user_id, lang),
    )

@router.callback_query(lambda c: c.data and c.data.startswith("privacy:"))
async def handle_privacy_callback(query: CallbackQuery):
    """Flip one privacy switch and redraw the menu. Only the user the menu
    belongs to may press its buttons."""
    try:
        _, owner_s, flag = query.data.split(":")
        owner_id = int(owner_s)
    except Exception:
        await query.answer()
        return

    chat = query.message.chat if query.message else None
    thread = (query.message.message_thread_id or 0) if query.message else 0
    lang = get_chat_lang(f"{chat.id}:{thread}") if chat else DEFAULT_LANG

    if flag not in db.PRIVACY_FLAGS:
        await query.answer()
        return
    if query.from_user.id != owner_id:
        await query.answer(localized("privacy_not_yours", lang), show_alert=True)
        return

    db.toggle_privacy_flag("telegram", owner_id, flag)
    try:
        await query.message.edit_text(
            _privacy_text(owner_id, lang),
            parse_mode="HTML",
            reply_markup=_privacy_keyboard(owner_id, lang),
        )
    except Exception:
        pass
    await query.answer()

@router.message(Command("help"))
async def help_cmd(message: Message):
    """The command list, grouped by required role. Its entries live in the
    i18n help.* keys — a new command must be added there (all six languages)
    to appear here. Self-deletes after a minute."""
    thread = message.message_thread_id or 0
    chat_key = f"{message.chat.id}:{thread}"
    lang = get_chat_lang(chat_key)

    requester = message.from_user.id if message.from_user else message.chat.id
    if not rate_limit_ok(("help-cmd", "telegram", requester), limit=5, window_seconds=60):
        return

    async def _reply_autodelete(text: str):
        """Reply in HTML, then remove the answer after a minute."""
        sent = await message.reply(text, parse_mode="HTML")
        await asyncio.sleep(60)
        try:
            await sent.delete()
        except Exception:
            pass

    everyone_lines = "\n".join([
        escape_html(localized_help("cmd_bridge", lang)),
        escape_html(localized_help("cmd_wikifeeds", lang)),
        escape_html(localized_help("cmd_whois", lang)),
        escape_html(localized_help("cmd_verify", lang)),
        escape_html(localized_help("cmd_mention", lang)),
        escape_html(localized_help("cmd_poll", lang)),
        escape_html(localized_help("cmd_privacy", lang)),
        escape_html(localized_help("cmd_locale", lang)),
        escape_html(localized_help("cmd_loc_compare", lang)),
        escape_html(localized_help("cmd_loc_suggest", lang)),
        escape_html(localized_help("cmd_help", lang)),
    ])

    admins_lines = "\n".join([
        escape_html(localized_help("cmd_rfb", lang)),
        escape_html(localized_help("cmd_setadmin", lang)),
        escape_html(localized_help("cmd_lang", lang)),
        escape_html(localized_help("cmd_locallang", lang)),
        escape_html(localized_help("cmd_remindrules", lang)),
        escape_html(localized_help("cmd_shadowban", lang)),
        escape_html(localized_help("cmd_unverify", lang)),
        escape_html(localized_help("cmd_allow_bots_tg", lang)),
        escape_html(localized_help("cmd_allow_files_tg", lang)),
        escape_html(localized_help("cmd_close", lang)),
        escape_html(localized_help("cmd_close_header", lang)),
    ])

    bot_admins_lines = "\n".join([
        escape_html(localized_help("cmd_atb", lang)),
        escape_html(localized_help("cmd_setytfeed", lang)),
        escape_html(localized_help("cmd_remytfeed", lang)),
        escape_html(localized_help("cmd_setbskyfeed", lang)),
        escape_html(localized_help("cmd_rembskyfeed", lang)),
        escape_html(localized_help("cmd_settgfeed", lang)),
        escape_html(localized_help("cmd_remtgfeed", lang)),
        escape_html(localized_help("cmd_setwikifeed", lang)),
        escape_html(localized_help("cmd_remwikifeed", lang)),
        escape_html(localized_help("cmd_wikifeed_settings", lang)),
        escape_html(localized_help("cmd_remadmin", lang)),
        escape_html(localized_help("cmd_setlocaladmin", lang)),
        escape_html(localized_help("cmd_remlocaladmin", lang)),
        escape_html(localized_help("cmd_localizer_add_tg", lang)),
        escape_html(localized_help("cmd_localizer_rem_tg", lang)),
        escape_html(localized_help("cmd_backup", lang)),
        escape_html(localized_help("cmd_loc_reply", lang)),
        escape_html(localized_help("cmd_setinbox", lang)),
        escape_html(localized_help("cmd_reminbox", lang)),
        escape_html(localized_help("cmd_setinboxchat", lang)),
        escape_html(localized_help("cmd_reminboxchat", lang)),
        escape_html(localized_help("cmd_inboxanon", lang)),
        escape_html(localized_help("cmd_inboxlist", lang)),
        escape_html(localized_help("cmd_inboxban", lang)),
        escape_html(localized_help("cmd_inboxunban", lang)),
    ])

    text = (
        f"<b>{localized_help('title', lang)}</b>\n\n"
        f"<b>{localized_help('section_everyone', lang)}</b>\n{everyone_lines}\n\n"
        f"<b>{localized_help('section_admins', lang)}</b>\n{admins_lines}\n\n"
        f"<b>{localized_help('section_bot_admins', lang)}</b>\n{bot_admins_lines}"
    )
    await _reply_autodelete(text)
