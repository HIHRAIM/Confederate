"""Slash-command modules, one per domain. Importing this package registers
every command on the bot's command tree (the @bot.tree.command decorators
run at import) — a new command module MUST be added to the import list below
or its commands silently never exist."""
from discord_bot.commands import (
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
