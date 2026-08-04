"""The Telegram half of the bridge, as a package.

Importing it builds the aiogram objects and registers every handler: the
import order below runs the @router.* decorators of each module. Commands are
imported before relay.py on purpose — aiogram dispatches in registration
order, and although the two cannot collide (the relay handler explicitly
skips text starting with '/'), keeping commands first means the ladder reads
the way it dispatches.

The re-exports are the package's public API: `bot` is the aiogram Bot every
other module reaches for as ``telegram_bot.bot``, `main` is the polling task
main.py gathers, and the two poll renderers are what the Discord half calls
when it publishes a poll into a Telegram chat.
"""
from telegram_bot.client import (
    bot,
    dp,
    get_telegram_avatar_url,
    is_telegram_native_admin,
    main,
    resolve_telegram_user,
    router,
    username_of,
)
from telegram_bot import commands
from telegram_bot import relay
from telegram_bot import feeds
from telegram_bot.commands.polls import build_poll_keyboard, poll_start_text_telegram
