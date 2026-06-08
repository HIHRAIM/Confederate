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
| `/setadmin <user>` | Grant bridge admin permissions in current chat | ❌ | ✅ | ✅ |
| `/remadmin <user>` | Revoke bridge admin permissions in current chat | ❌ | ❌ | ✅ |
| `/deadchat <role_id\|disable> <hours>` | Ping a role after N hours of inactivity in the channel | ❌ | ✅ | ✅ |
| `/deadtopic <enable\|disable>` | Post a phantom message every 6 days to keep the thread alive | ❌ | ✅ | ✅ |
| `/newschat <add <emoji>\|disable>` | Auto-react to new messages in channel | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m\|disable> [messages] [message_id] [text]` | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for this channel/topic | ❌ | ✅ | ✅ |
| `/bridge` | Show which bridge and chats the current channel belongs to | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message, or context menu on message) | Show original sender identity | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify <user>` | Remove verification status from user | ❌ | ❌ | ✅ |
| `/list_chats` | List all Discord guilds and Telegram groups known to the bot | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force bot to leave a guild/chat and clean up DB records | ❌ | ❌ | ✅ |
| `/allow-bots <enable\|disable>` | Allow or block relay of bot/webhook messages from this channel | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

### Telegram commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id>` | Attach current Telegram chat/topic to a bridge | ❌ | ❌ | ✅ |
| `/rfb` | Remove current chat/topic from a bridge (run inside the target chat) | ❌ | ✅ | ✅ |
| `/setadmin <user_id_or_username>` | Grant bridge admin permissions | ❌ | ✅ | ✅ |
| `/remadmin <user_id_or_username>` | Revoke bridge admin permissions | ❌ | ❌ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for current chat/topic | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m> [messages]` (as reply) | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/bridge` | Show which bridge and chats the current chat belongs to | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original Telegram sender identity | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user_id_or_username>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify <user_id_or_username>` | Remove verification status from user | ❌ | ❌ | ✅ |
| `/allow_bots <enable\|disable>` | Allow or block relay of bot messages from this chat | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

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
  - Chat language settings.
- **Verification data**
  - Verified users with expiration timestamp.
  - Pending consent records for verification flows.

### Retention periods

- **Message relay metadata (`messages` + `message_copies`)**: up to **30 days** (cleaned on startup).
- **Pending consent records**: up to **24 hours** if not confirmed (cleaned continuously).
- **Verified user records**: default validity **365 days**, then auto-removed after expiry.
- **Settings/admin/bridge mappings**: kept until manually changed/removed, or automatically cleaned when the bot leaves a server/chat.

### Data usage boundaries

- The bot uses stored data only to operate bridge relays, moderation, permissions, and automation.
- It does not implement analytics/tracking pipelines in this repository.
- Data is local to the bot runtime environment unless your deployment adds external backup/logging.
