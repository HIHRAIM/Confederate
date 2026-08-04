"""Localization commands: /locale (status and files), /loc-compare,
/loc-suggest and the admin's /loc-reply — plus the support-chat posting
helpers the Telegram twins reuse.

The suggestion flow: a user files a suggestion (stored under a short hex
code, posted to the SUPPORT_CHATS of both platforms), an admin answers it
with /loc-reply <code>, the answer is DM'd to the suggester in their own
interface language and echoed to the support chats.
"""
import os
import secrets

import discord
from discord import app_commands

import db
import utils
from config import SUPPORT_CHATS
from utils import (
    DEFAULT_LANG, LANG_ORDER, LOCALE_STATUS_EMOJI, SUPPORTED_LANGS,
    available_locales, compare_reply, get_chat_lang, is_admin, language_name,
    locale_bar, locale_stats, localized, rate_limit_ok,
)

from discord_bot.client import bot

async def post_loc_suggestion(*, lang, key, suggestion, code, ui_lang, username, user_id, avatar_url=None):
    """Post a localization suggestion to the Discord and Telegram support chat(s)."""
    body = localized("loc_suggest_support_body", ui_lang,
                     suggestion=suggestion, name=language_name(lang), lang=lang, key=key)
    footer = f"{username} │ ID: {user_id} │ {code}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        embed = discord.Embed(description=body)
        embed.set_footer(text=footer, icon_url=avatar_url)
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), f"{body}\n\n{footer}",
                    message_thread_id=int(thread) or None
                )
            except Exception:
                pass

async def post_loc_reply(*, admin, code, ui_lang, title, body):
    """Publish an admin's /loc-reply to the support chat(s)."""
    prefix = localized("loc_reply_support_prefix", ui_lang, admin=admin, code=code)
    text = f"{prefix}\n\n{body}"

    for cid in SUPPORT_CHATS.get("discord", set()):
        channel = bot.get_channel(int(cid))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(cid))
            except Exception:
                channel = None
        if channel is None:
            continue
        try:
            await channel.send(embed=discord.Embed(title=title, description=text))
        except Exception:
            pass

    try:
        from telegram_bot import bot as tg_bot
    except Exception:
        tg_bot = None
    if tg_bot is not None:
        for chat_key in SUPPORT_CHATS.get("telegram", set()):
            try:
                tg_chat_id, thread = str(chat_key).split(":")
                await tg_bot.send_message(
                    int(tg_chat_id), text, message_thread_id=int(thread) or None
                )
            except Exception:
                pass

@bot.tree.command(name="locale", description="localization status, or a language's file")
@app_commands.describe(lang="Language code (optional). With a code, sends that language's localization file.")
async def locale_cmd(interaction: discord.Interaction, lang: str = None):
    """Without a code: the verified/unverified/untranslated bar per language.
    With one: that language's i18n JSON file, rate-limited per server —
    the file is the entire localization, not something to spam."""
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")

    if not lang or not lang.strip():
        lines = [localized("loc_list_header", ui_lang)]
        for code in available_locales():
            st = locale_stats(code)
            lines.append(f"{language_name(code)} (`{code}`): {locale_bar(code)} {st['percent']}%")
        lines.append("")
        lines.append(localized("loc_list_footer", ui_lang))
        await interaction.response.send_message("\n".join(lines))
        return

    code = lang.strip().lower()
    if code not in available_locales():
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=code, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return

    if not rate_limit_ok(("locale-file", "discord", interaction.guild_id or interaction.user.id),
                         limit=1, window_seconds=600):
        await interaction.response.send_message(localized("loc_cooldown", ui_lang), ephemeral=True)
        return

    path = os.path.join(os.path.dirname(utils.__file__), "i18n", f"{code}.json")
    st = locale_stats(code)
    caption = localized("loc_file_caption", ui_lang, name=language_name(code), code=code, percent=st["percent"])
    try:
        await interaction.response.send_message(caption, file=discord.File(path, filename=f"{code}.json"))
    except Exception:
        await interaction.response.send_message(caption, ephemeral=True)

@bot.tree.command(name="loc-compare", description="compare a reply across languages")
@app_commands.describe(key="Reply code (as shown in the localization file)")
async def loc_compare_cmd(interaction: discord.Interaction, key: str):
    """Show one reply key in all six languages side by side, with each
    translation's status emoji — the tool for spotting what needs work."""
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    key = key.strip()
    data = compare_reply(key)
    if data is None:
        await interaction.response.send_message(localized("loc_compare_not_found", ui_lang, key=key), ephemeral=True)
        return

    lines = [localized("loc_compare_header", ui_lang, key=key)]
    for code in LANG_ORDER:
        if code not in data:
            continue
        status, text = data[code]
        emoji = LOCALE_STATUS_EMOJI.get(status, "")
        if text is None:
            shown = localized("loc_compare_untranslated", ui_lang)
        else:
            shown = str(text)
            if len(shown) > 300:
                shown = shown[:297] + "..."
        lines.append(f"{emoji} {language_name(code)}: {shown}")
    msg = "\n".join(lines)
    if len(msg) > 1990:
        msg = msg[:1990]
    await interaction.response.send_message(msg)

@bot.tree.command(name="loc-suggest", description="suggest a localization")
@app_commands.describe(language="Language code", code="Reply code", text="Suggested text")
async def loc_suggest_cmd(interaction: discord.Interaction, language: str, code: str, text: str):
    """File a translation suggestion. It is stored under a short hex code and
    posted to the support chats; the code is echoed back so the suggester can
    reference the eventual /loc-reply."""
    ui_lang = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    language = language.strip().lower()
    if language not in SUPPORTED_LANGS:
        await interaction.response.send_message(
            localized("loc_unknown_lang", ui_lang, lang=language, supported=", ".join(available_locales())),
            ephemeral=True
        )
        return
    if not SUPPORT_CHATS.get("discord") and not SUPPORT_CHATS.get("telegram"):
        await interaction.response.send_message(localized("loc_suggest_no_support", ui_lang), ephemeral=True)
        return

    msg_code = secrets.token_hex(4)
    db.add_loc_suggestion(msg_code, "discord", interaction.user.id, str(interaction.user),
                          language, code.strip(), text, ui_lang)
    avatar_url = None
    try:
        avatar_url = interaction.user.display_avatar.url
    except Exception:
        avatar_url = None
    await post_loc_suggestion(lang=language, key=code.strip(), suggestion=text, code=msg_code,
                              ui_lang=ui_lang, username=str(interaction.user),
                              user_id=interaction.user.id, avatar_url=avatar_url)
    await interaction.response.send_message(localized("loc_suggest_confirm", ui_lang, code=msg_code), ephemeral=True)

@bot.tree.command(name="loc-reply", description="reply to a localization suggestion (bot admins)")
@app_commands.describe(code="Message code from the suggestion", text="Reply text")
async def loc_reply_cmd(interaction: discord.Interaction, code: str, text: str):
    """Answer a suggestion by its code: DM the suggester (in the interface
    language they filed in, wherever they filed from), echo to the support
    chats, and close the ticket only if the DM went through — otherwise it
    stays open to be answered again."""
    ui_lang_cmd = get_chat_lang(f"{interaction.guild_id}:{interaction.channel_id}")
    if not is_admin("discord", interaction.user.id):
        await interaction.response.send_message(localized("no_permission", ui_lang_cmd), ephemeral=True)
        return

    row = db.get_loc_suggestion(code.strip())
    if not row:
        await interaction.response.send_message(localized("loc_reply_not_found", ui_lang_cmd, code=code), ephemeral=True)
        return

    ui_lang = row["ui_lang"] or DEFAULT_LANG
    title = localized("loc_reply_dm_title", ui_lang)
    body = localized("loc_reply_dm_body", ui_lang,
                     suggestion=row["suggestion"], reply=text,
                     name=language_name(row["lang"]), lang=row["lang"], key=row["rkey"])

    ok = False
    if row["platform"] == "discord":
        try:
            user = await bot.fetch_user(int(row["user_id"]))
            await user.send(embed=discord.Embed(title=title, description=body))
            ok = True
        except Exception:
            ok = False
    elif row["platform"] == "telegram":
        try:
            from telegram_bot import bot as tg_bot
            await tg_bot.send_message(int(row["user_id"]), f"{title}\n\n{body}")
            ok = True
        except Exception:
            ok = False

    await post_loc_reply(admin=str(interaction.user), code=code.strip(),
                         ui_lang=ui_lang, title=title, body=body)

    if ok:
        db.delete_loc_suggestion(code.strip())
        await interaction.response.send_message(localized("loc_reply_sent", ui_lang_cmd), ephemeral=True)
    else:
        await interaction.response.send_message(localized("loc_reply_failed", ui_lang_cmd), ephemeral=True)
