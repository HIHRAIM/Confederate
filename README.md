# Confederate

Confederate is a cross-platform relay bot that bridges Discord channels/threads/forum posts and Telegram chats/topics into shared conversation spaces (“bridges”). It forwards messages in both directions, supports moderation and admin delegation per bridge, and includes quality-of-life automation (verification prompts, dead-chat pings, periodic rules reminders, and language settings).

## Requirements

- Python **3.10+** (recommended 3.11+)
- A Discord bot token
- A Telegram bot token
- SQLite (uses local `bridge.db`, no external DB required)
- Python packages used by the project:
  - `discord.py`
  - `aiogram`

## Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/HIHRAIM/Confederate
   cd Confederate
   ```

2. **Create and activate a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install discord.py aiogram
   ```

4. **Create config file**
   - Copy `config.example.py` to `config.py`.
   - Set environment variables (the example config reads tokens from env):
     - `DISCORD_BOT_TOKEN` — your Discord bot token.
     - `TELEGRAM_BOT_TOKEN` — your Telegram bot token.
   - Edit `config.py`:
     - `ADMINS["discord"]` and `ADMINS["telegram"]` — sets of numeric user IDs with global bot-admin rights.
     - `SERVICE_CHATS["discord"]` and `SERVICE_CHATS["telegram"]` — chat IDs where the bot sends startup/shutdown and health events. Telegram format: `"-1000000000000:0"` (chat\_id:thread\_id); Discord format: numeric channel ID.
     - `BACKUP_CHATS["discord"]` and `BACKUP_CHATS["telegram"]` — chat IDs where the bot sends automatic database backups every 12 hours. Same format as `SERVICE_CHATS`.
     - `SUPPORT_CHATS["discord"]` and `SUPPORT_CHATS["telegram"]` — chats that receive localization suggestions submitted via `/loc-suggest` (Discord as an embed, Telegram as a message). Same format as `SERVICE_CHATS`.
     - `VERIFIED` — set of Discord channel IDs where a user's ID is published once they accept the forwarding consent. **guard_bot** reads the same channel(s) to add them to its cross-server verified database. Use the same ID in both bots' configs.
     - `UNVERIFIED` — set of Discord channel IDs where a user's ID is published when they unverify themselves (`/unverify`). **guard_bot** reads the same channel(s) to remove them from its verified database. Use the same ID in both bots' configs.

> **Presence intent:** `/whois` reports a member's online status (online/idle/dnd), which requires the privileged **Presence Intent** — enable it for the bot in the Discord Developer Portal, otherwise the bot will not start.

5. **Run the bot**
   ```bash
   python src/main.py
   ```

---

## Commands

Permission roles used below:

- **Everyone** — any user in the connected chat/channel.
- **Bridge Admins** — delegated moderators for a specific bridge (and/or chat-level admins managed by the bot).
- **Bot Admins** — global admins defined in `config.py` (`ADMINS`).

> Notes:
> - On Telegram, bridge-level delegation is done via `/setadmin` and stored per bridge/chat.
> - On Discord, bridge/chat management permissions are enforced through bot-managed admin checks.
> - Telegram `/rfb` must be run inside the chat/topic to be removed — removal by ID is not supported.

### Discord commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id>` | Attach current Discord channel to a bridge | ❌ | ❌ | ✅ |
| `/rfb [channel_or_chat_id]` | Remove channel from a bridge (current channel if no ID given) | ❌ | ✅ | ✅ |
| `/setadmin <user>` | Grant Bridge Admin rights across the whole bridge; DMs the user | ❌ | ✅ | ✅ |
| `/remadmin <user>` | Revoke bridge admin permissions in current chat | ❌ | ❌ | ✅ |
| `/deadchat <role_id\|disable> <hours>` | Ping a role after N hours of inactivity in the channel | ❌ | ✅ | ✅ |
| `/deadtopic <enable\|disable>` | Post a phantom message every 6 days to keep the thread alive | ❌ | ✅ | ✅ |
| `/newschat <add <emoji>\|disable>` | Auto-react to new messages in channel | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m\|disable> [messages] [message_id] [text]` | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for this channel/topic | ❌ | ✅ | ✅ |
| `/webhooks <enable\|disable>` | Relay incoming messages into this channel as per-sender webhooks (avatar + name). Refused in threads/forum posts | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message, or context menu on message) | Show original sender identity (incl. online status) | ✅ | ✅ | ✅ |
| `/poll <text> <duration> <option1> <option2> [option3…5]` | Start an anonymous poll in every bridge chat; verified users vote via buttons; results post on expiry (max 30 days, up to 5 options) | ✅ | ✅ | ✅ |
| `/locale [code]` | Show localization status (bar + verified %), or send a language's localization file (10-min per-server cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc-compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc-suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify [user]` | Unverify yourself (no argument), or another user (Bot Admins). Discord usage also notifies guard_bot via the `UNVERIFIED` channel | ✅ | ✅ | ✅ |
| `/loc-reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` | List all Discord guilds and Telegram groups known to the bot | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force bot to leave a guild/chat and clean up DB records | ❌ | ❌ | ✅ |
| `/allow-bots <enable\|disable>` | Allow or block relay of bot/webhook messages from this channel | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

### Telegram commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id>` | Attach current Telegram chat/topic to a bridge | ❌ | ❌ | ✅ |
| `/rfb` | Remove current chat/topic from a bridge (run inside the target chat) | ❌ | ✅ | ✅ |
| `/setadmin <user_id_or_username>` | Grant Bridge Admin rights across the whole bridge; DMs the user | ❌ | ✅ | ✅ |
| `/remadmin <user_id_or_username>` | Revoke bridge admin permissions | ❌ | ❌ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for current chat/topic | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m> [messages]` (as reply) | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original sender identity | ✅ | ✅ | ✅ |
| `/poll <text> \| <duration> \| <option1> \| <option2> \| …` | Start an anonymous poll in every bridge chat (pipe-separated, up to 10 options, max 30 days) | ✅ | ✅ | ✅ |
| `/locale [code]` | Show localization status, or send a language's localization file (10-min per-group cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc_compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc_suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user_id_or_username>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify [user_id_or_username]` | Unverify yourself (no argument), or another user (Bot Admins) | ✅ | ✅ | ✅ |
| `/loc_reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/allow_bots <enable\|disable>` | Allow or block relay of bot messages from this chat | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

> Telegram command names use underscores where Discord uses hyphens (`/loc_compare` ↔ `/loc-compare`); both spellings are accepted on Telegram.

---

## Localization

All bot-facing strings live in per-language JSON files under `src/i18n/` (`ru`, `uk`, `pl`, `en`, `es`, `pt`). Each entry carries a translation **status**: `verified` (🟩), `unverified` (🟧) or `untranslated` (🟥, a key missing relative to the reference `DEFAULT_LANG`).

- `/locale` shows each language with an emoji bar and the percentage of verified strings; `/locale <code>` sends that language's JSON file (so the reply codes are visible for use with the other commands).
- `/loc-compare <code>` compares one reply across all languages with status emoji.
- `/loc-suggest <lang> <code> <text>` forwards a translation suggestion to `SUPPORT_CHATS`, tagged with a unique dialog code.
- `/loc-reply <code> <text>` (Bot Admins) DMs the original suggester; the dialog code is then removed. Suggestion codes are kept at most **1 year**.

## Webhooks relay

`/webhooks enable` makes incoming relayed messages in that Discord channel appear as **webhook** messages — the webhook's name is `{sender} [{platform} | {server}]` (matching the `[{platform} | {server}] {sender}` header of normal relayed messages) and its avatar is the sender's avatar. One webhook per channel is created and reused, with per-message name/avatar overrides, and edits to an original message are propagated to its webhook copy. Webhooks don't exist in threads/forum posts, so the command refuses there, and the bot needs the **Manage Webhooks** permission (otherwise it silently falls back to normal relay messages).

Telegram avatars can't be a webhook avatar directly (the Telegram file URL embeds the bot token and isn't reliably fetched by Discord), so the bot downloads the Telegram photo and re-hosts it on Discord's CDN by uploading it to the first `SERVICE_CHATS["discord"]` channel (cached per user; the previous upload is replaced on refresh). If no service channel is configured/reachable, Telegram senders fall back to the default webhook avatar.

## Polls

`/poll` starts an **anonymous** poll that is posted (with vote buttons) to every chat in the bridge. Only **verified** users (who accepted the forwarding consent) can vote; each user has one vote that they can change. On Discord, options are separate arguments (up to 5); on Telegram, the question, duration and options are separated by `|` (up to 10 options). Duration units: `1h`, `2d`, `1w`, `1m` (= 30 days); capped at 30 days.

When the timer expires the bot posts the results to every bridge chat, replying to that chat's poll message (or without a reply if the chat joined the bridge after the poll started). Deleting a poll message in a Discord chat closes the poll and deletes it everywhere (Telegram deletions aren't detectable by bots, so use the Discord side to cancel a poll).

## Localization suggestions follow-up

`/loc-reply` DMs the original suggester **and** posts the admin's reply into the support chats, so the team can see how a suggestion was resolved.

## Cross-bot verification sync

When a user accepts the forwarding consent, their ID is posted to the `VERIFIED` Discord channel; when they `/unverify` themselves on Discord, their ID is posted to `UNVERIFIED`. **guard_bot** watches the same channels to mirror users into / out of its cross-server verified database.

---

## Data collection and retention

The bot stores operational data in local SQLite (`bridge.db`) to provide relaying, moderation, and automation features.

### What data is stored

- **Bridge topology**
  - Bridge IDs, attached chat IDs, platform mapping.
- **Relayed message metadata**
  - Origin platform/chat/message IDs.
  - Origin sender ID.
  - Copy message IDs across platforms.
  - Creation timestamp.
- **Admin and moderation data**
  - Chat admins and bridge admins.
  - Shadow-ban records.
- **Automation settings**
  - Deadchat config (`role_id`, timeout, last activity timestamp).
  - Deadtopic config (phantom-message schedule per thread).
  - Newschat emoji reaction rules.
  - Rules reminder configuration.
  - Chat language settings and the per-channel webhook-relay toggle.
- **Verification data**
  - Verified users with expiration timestamp.
  - Pending consent records for verification flows.
- **Localization suggestions**
  - `/loc-suggest` dialog codes: submitter platform/ID/username, target language, reply code, suggested text.

### Retention periods

- **Message relay metadata (`messages` + `message_copies`)**: up to **30 days** (cleaned on startup).
- **Pending consent records**: up to **24 hours** if not confirmed (cleaned continuously).
- **Verified user records**: default validity **365 days**, then auto-removed after expiry.
- **Localization-suggestion codes**: up to **1 year**, and removed immediately once answered with `/loc-reply`.
- **Settings/admin/bridge mappings**: kept until manually changed/removed, or automatically cleaned when the bot leaves a server/chat.

### Data usage boundaries

- The bot uses stored data only to operate bridge relays, moderation, permissions, and automation.
- It does not implement analytics/tracking pipelines in this repository.
- Data is local to the bot runtime environment unless your deployment adds external backup/logging.
