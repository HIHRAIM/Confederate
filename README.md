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
   - Set:
     - `YOUR_DISCORD_BOT_TOKEN`
     - `YOUR_TELEGRAM_BOT_TOKEN`
     - `ADMINS["discord"]` and `ADMINS["telegram"]` with bot-admin user IDs.

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

### Discord commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id>` | Attach current Discord channel to a bridge | ❌ | ❌ | ✅ |
| `/rfb [channel_or_chat_id]` | Remove channel from a bridge | ❌ | ✅ | ✅ |
| `/setadmin <user>` | Grant chat/bridge admin permissions in current chat | ❌ | ✅ | ✅ |
| `/deadchat <role_id\|disable> [hours]` | Enable/disable inactivity role-ping automation | ❌ | ✅ | ✅ |
| `/newschat add <emoji>` / `/newschat disable` | Auto-react to new messages in channel | ❌ | ✅ | ✅ |
| `/remindrules <hours> [messages]` (as reply) | Save periodic rules repost configuration | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for this channel/topic | ❌ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original sender identity | ✅ | ✅ | ✅ |
| `/shadow-ban <user>` | Shadow-ban a user for relay | ❌ | ✅ | ✅ |
| `/unverify <user>` | Remove verification status from user | ❌ | ❌ | ✅ |
| `/list_chats` | Show known Discord/Telegram chats | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force bot to leave guild/chat and cleanup records | ❌ | ❌ | ✅ |

### Telegram commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id>` | Attach current Telegram chat/topic to a bridge | ❌ | ❌ | ✅ |
| `/rfb` | Remove current chat/topic from a bridge | ❌ | ✅ | ✅ |
| `/setadmin <user_id_or_username>` | Grant bridge admin permissions | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set language for current chat/topic | ❌ | ✅ | ✅ |
| `/remindrules <hours> [messages]` (as reply) | Save periodic rules repost configuration | ❌ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original Telegram sender identity | ✅ | ✅ | ✅ |
| `/shadow-ban <user_id_or_username>` | Shadow-ban a user for relay | ❌ | ✅ | ✅ |
| `/unverify <user_id_or_username>` | Remove verification status from user | ❌ | ❌ | ✅ |

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
