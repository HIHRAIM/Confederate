"""Template for src/config.py — this deployment's ids and channels.

Copy it to src/config.py and fill in the values; the real file is deliberately
untracked (it names one particular set of servers), so this template is the
documentation of what the bot expects to find there.

Tokens are never written here: they come from src/.env through env_loader.
Chat keys follow the bot's own convention — 'guild:channel' on Discord,
'group:topic' on Telegram (topic 0 = the plain group).
"""
import os

from env_loader import load_env
load_env()

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ADMINS = {
    "discord": {ADMINISTRATOR_ID, ADMINISTRATOR_ID},
    "telegram": {ADMINISTRATOR_ID, ADMINISTRATOR_ID}
}

SERVICE_CHATS = {
    "discord": {
        CHAT_ID,
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID", # Example: -1000000000000:00000
        "CHAT_ID",
    },
}

BACKUP_CHATS = {
    "discord": {
        CHAT_ID,
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID",
        "CHAT_ID",
    },
}

# GALLERY — Discord channels where the bot re-uploads the files it hands out as
# links: Telegram files, once `/allow-files` is enabled for every chat of a
# bridge (see README: Telegram file re-upload), and the attachments of the posts
# of sources attached with `/setbskyfeed` / `/setytfeed` / `/settgfeed` (see README: Followed
# sources). The first reachable channel is used; the CDN links of the uploaded
# attachments are handed out in the relayed copies. The bot never deletes these
# uploads on its own — a link that has been handed out keeps working.
GALLERY = {
    CHAT_ID,
    CHAT_ID,
}

SUPPORT_CHATS = {
    "discord": {
        CHAT_ID,
    },
    "telegram": {
        "CHAT_ID",
    },
}

VERIFIED = {
    CHAT_ID,
}

UNVERIFIED = {
    CHAT_ID,
}

# PURGATORIUM — the shared appeal server (see README: Purgatorium appeals).
PURGATORIUM_GUILD_ID = GUILD_ID
PURGATORIUM_INVITE_URL = "https://discord.gg/INVITE_CODE"
# Confederate Guard's bot user id: its ban-summary messages in appeal threads
# are pinned instead of being relayed to the appellant's DM.
GUARD_BOT_ID = BOT_USER_ID
# Channel on PURGATORIUM where /appeal opens one thread per appellant.
APPEAL_CHANNEL_ID = CHAT_ID
# Channels where consul-granted appeals are posted (bare user IDs) for
# Confederate Guard to lift the user's bans.
APPEAL_PARDON_CHANNELS = {
    "discord": {
        CHAT_ID,
    },
}
# Channels where "<user_id> <thread_id>" is posted for every new appeal so
# Confederate Guard can publish the appellant's ban summary into the thread.
APPEAL_BANINFO_CHANNELS = {
    "discord": {
        CHAT_ID,
    },
}
# Role IDs on PURGATORIUM whose holders are consuls: they may use the appeal
# verdict buttons and set the alias appellants see for them (/setname).
CONSULS = {
    ROLE_ID,
}

WIKI_CONTACT = "https://github.com/HIHRAIM/Confederate"

