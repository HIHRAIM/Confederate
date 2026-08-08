"""Telegram command modules, one per domain. Importing this package registers
every handler on the router (the @router.* decorators run at import) — a new
command module MUST be added to the import list below or its commands
silently never exist."""
from telegram_bot.commands import (
    admins,
    bridges,
    feeds,
    inbox,
    locale,
    polls,
    settings,
    user,
    wiki,
)
