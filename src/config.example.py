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
# links: Telegram files, wherever `/allow-files` is enabled — the chat they come
# from decides whether they are taken, each target chat decides whether it gets
# the links (see README: Telegram file re-upload) — and the attachments of the posts
# of sources attached with `/setbskyfeed` / `/setytfeed` / `/settgfeed` (see README: Followed
# sources). The first reachable channel is used; the CDN links of the uploaded
# attachments are handed out in the relayed copies. The bot never deletes these
# uploads on its own — a link that has been handed out keeps working.
# The bundled avatar pictures (src/assets/) are hosted here too, uploaded once
# and re-uploaded by themselves if the message is ever deleted, so this channel
# has to stay reachable for webhook copies and feed posts to keep their faces.
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

# PATREON — the sponsor tiers (see README: Sponsors). The bot never talks to
# Patreon: patrons connect Patreon to Discord themselves, Patreon's own bot
# hands them a role on the creator's server, and the only thing read here is
# whether a Discord account holds one of these roles. There is therefore no
# Patreon token, no webhook and no secret of any kind for this feature.
# PATREON_GUILD_ID is the creator's server; PATREON_TIER_ROLES maps a tier
# number onto the role Patreon grants for it, and the tiers must be numbered
# from 1 upwards — somebody holding two of these roles counts as the higher
# tier. Leaving the mapping empty, or naming a role that does not exist,
# simply means nobody is found to be a sponsor of that tier; it is not an
# error and the bot says so once rather than on every check.
PATREON_GUILD_ID = GUILD_ID
PATREON_TIER_ROLES = {
    1: ROLE_ID,
    2: ROLE_ID,
}
# SPONSOR_NOTICE_CHANNEL — where a sponsor is nudged when their DMs are shut.
# What goes there is deliberately ONLY a mention and "check /sponsor": the tier,
# the reason and the deadline stay in the direct message, because this channel
# is read by other people and none of that is theirs to read. Set it to None to
# turn the fallback off, in which case a sponsor with closed DMs is simply
# never reached and reads the same facts with /sponsor whenever they like.
SPONSOR_NOTICE_CHANNEL = CHAT_ID
# SPONSOR_URL — the campaign page /sponsor-subscribe points people at. It
# belongs here rather than in sponsors.py because it names one particular
# creator's page, and src/config.py is the file that is deliberately kept out
# of git for exactly that reason.
SPONSOR_URL = "https://www.patreon.com/YOUR_CAMPAIGN"

WIKI_CONTACT = "https://github.com/HIHRAIM/Confederate"

