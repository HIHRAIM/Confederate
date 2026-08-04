"""The Discord half of the bridge, as a package.

Importing it builds the client and registers everything: the import order
below runs the @bot.event and @bot.tree.command decorators of every module.
The order is the dependency order (client first, commands last) — append new
modules in a place that respects it, and remember /appeal and /setname live
in appeals.py, not under commands/.

The re-exports are the package's public API: everything main.py, utils.py
and the Telegram half import from `discord_bot`. Add here whatever new
cross-half surface you create; keep importing it lazily (at the call site)
from the Telegram side, as everywhere else.
"""
from discord_bot.client import (
    DiscordBot,
    announce_unverified_user,
    announce_verified_user,
    bot,
)
from discord_bot.relay import (
    deliver_discord_relay,
    deliver_telegram_relay,
    edit_discord_relay_copy,
    is_dm_chat_id,
    resolve_discord_chat_channel,
    send_bridge_mention,
)
from discord_bot.mentions import resolve_discord_user
from discord_bot.feeds import (
    attach_feed,
    avatar_asset_url,
    delete_gallery_message,
    feed_module,
    feed_stale_since,
    gallery_upload,
    gallery_upload_budget,
    relay_feed_post,
    warm_feed_avatars,
    FEED_KINDS,
)
from discord_bot import appeals
from discord_bot import events
from discord_bot import commands
from discord_bot.commands.bridges import resolve_bridge_admins
from discord_bot.commands.feeds import BSKYFEED_KEYS, TGFEED_KEYS, YTFEED_KEYS
from discord_bot.commands.locale import post_loc_reply, post_loc_suggestion
from discord_bot.commands.polls import post_poll_results, publish_poll
