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
   - Set environment variables (the example config reads tokens from env), or copy `src/.env.example` to `src/.env` and fill it in — the config loads it automatically (already-set environment variables take precedence):
     - `DISCORD_BOT_TOKEN` — your Discord bot token.
     - `TELEGRAM_BOT_TOKEN` — your Telegram bot token.
   - Edit `config.py`:
     - `ADMINS["discord"]` and `ADMINS["telegram"]` — sets of numeric user IDs with global bot-admin rights.
     - `SERVICE_CHATS["discord"]` and `SERVICE_CHATS["telegram"]` — chat IDs where the bot sends startup/shutdown and health events. Telegram format: `"-1000000000000:0"` (chat\_id:thread\_id); Discord format: numeric channel ID.
     - `BACKUP_CHATS["discord"]` and `BACKUP_CHATS["telegram"]` — chat IDs where the bot sends automatic database backups every 12 hours. Same format as `SERVICE_CHATS`.
     - `SUPPORT_CHATS["discord"]` and `SUPPORT_CHATS["telegram"]` — chats that receive localization suggestions submitted via `/loc-suggest` (Discord as an embed, Telegram as a message). Same format as `SERVICE_CHATS`.
     - `VERIFIED` — set of Discord channel IDs where a **Discord** user's ID is published once they accept the forwarding consent. **Confederate Guard** reads the same channel(s) to add them to its cross-server verified database. Only Discord user IDs are published — Telegram verifications stay local to Confederate. Use the same ID in both bots' configs.
     - `UNVERIFIED` — set of Discord channel IDs where a **Discord** user's ID is published when they unverify themselves (`/unverify`). **Confederate Guard** reads the same channel(s) to remove them from its verified database. Use the same ID in both bots' configs.

   > The `VERIFIED`/`UNVERIFIED` mechanic is only needed when the bot runs alongside [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard). If you don't run Confederate Guard, leave the sets empty or turn the publishing off at runtime with `/verify-list disable` (it is enabled by default).

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
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole server (used wherever no `/locallang` override is set) | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for this channel/thread (overrides the server-wide `/lang`) | ❌ | ✅ | ✅ |
| `/webhooks <enable\|disable>` | Relay incoming messages into this channel as per-sender webhooks (avatar + name). Refused in threads/forum posts | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message, or context menu on message) | Show original sender identity (incl. online status) | ✅ | ✅ | ✅ |
| `/mention <user_id_or_username>` | Mention a Discord user from another bridge community: posts a relay-style message pinging them into a random bridge Discord chat; 1-hour cooldown per target user | ✅ | ✅ | ✅ |
| `/poll <text> <duration> <option1> <option2> [option3…5]` | Start an anonymous poll in every bridge chat; verified users vote via buttons; results post on expiry (max 30 days, up to 5 options) | ✅ | ✅ | ✅ |
| `/locale [code]` | Show localization status (bar + verified %), or send a language's localization file (10-min per-server cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc-compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc-suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify [user]` | Unverify yourself (no argument), or another user (Bot Admins). Discord usage also notifies Confederate Guard via the `UNVERIFIED` channel | ✅ | ✅ | ✅ |
| `/verify-list <enable\|disable>` | Toggle publishing of (un)verified Discord user IDs to the `VERIFIED`/`UNVERIFIED` sync channels (enabled by default; only needed alongside Confederate Guard) | ❌ | ❌ | ✅ |
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
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole group (used wherever no `/locallang` override is set) | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for the current chat/topic (overrides the group-wide `/lang`) | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m> [messages]` (as reply) | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original sender identity | ✅ | ✅ | ✅ |
| `/mention <user_id_or_username>` | Mention a Discord user from another bridge community: posts a relay-style message pinging them into a random bridge Discord chat; 1-hour cooldown per target user | ✅ | ✅ | ✅ |
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

## Mechanics

Every user-facing mechanic of Confederate, in one place.

### Message relay

Chats attached to the same bridge (`/atb`) exchange messages in both directions. Relayed copies carry a `[{Messenger} | {Community}] {Sender}:` header, native replies are preserved (or represented with a link/note where the platform can't reference the original), edits and deletions of the original propagate to all copies for 30 days, and attachments/stickers/voice/video notes are represented with localized markers or links. Messages from other bots and webhooks are relayed only where `/allow-bots` enables it.

### Verification and forwarding consent

The first time someone writes in a bridged chat, the bot replies with a localized consent prompt (an "Accept" button). Until they accept, their messages are not relayed: the first message is stored and relayed after consent, later ones are deleted. Consent is stored per platform and is valid for **365 days**; accepting it once covers every bridged chat of that platform. `/verify` re-issues the prompt, `/unverify` revokes consent (Bot Admins can unverify others).

### Languages

Replies and relayed service texts are localized **per target chat**. The language is resolved in this order: the channel/topic's own `/locallang` setting → the server/group-wide default set with `/lang` (Bridge Admins) → English. The bot's Discord presence line rotates through all six languages.

### Webhooks relay

`/webhooks enable` makes incoming relayed messages in that Discord channel appear as **webhook** messages — the webhook's name is `{sender} [{platform} | {server}]` (matching the `[{platform} | {server}] {sender}` header of normal relayed messages) and its avatar is the sender's avatar. One webhook per channel is created and reused, with per-message name/avatar overrides, and edits to an original message are propagated to its webhook copy. Webhooks don't exist in threads/forum posts, so the command refuses there, and the bot needs the **Manage Webhooks** permission (otherwise it silently falls back to normal relay messages).

Webhook messages can't carry a native Discord reply reference, so when the relayed message is a reply, the bot prepends a localized first line — e.g. `(replying to [{sender}'s message](link))` — whose bracketed text links to the replied-to message in the same channel. If the replied-to message can't be resolved, the usual "reply to an unknown message" line is shown instead.

Telegram avatars can't be a webhook avatar directly (the Telegram file URL embeds the bot token and isn't reliably fetched by Discord), so the bot downloads the Telegram photo and re-hosts it on Discord's CDN by uploading it to the first `SERVICE_CHATS["discord"]` channel (cached per user; the previous upload is replaced on refresh). If no service channel is configured/reachable, Telegram senders fall back to the default webhook avatar.

### Forwarded messages

Relayed copies of forwarded messages get a localized attribution prefix. On Telegram the forward origin comes straight from the Telegram API. On Discord, forward snapshots intentionally omit the original author, so the bot resolves the original message through the forward reference: if it can read the source channel, the prefix is “(forwarded from {user's nickname})”; otherwise, if the source server is known to the bot, “(forwarded from {server name})”; otherwise “(forwarded from unknown source)”.

### Mentions across the bridge

`/mention <user>` lets anyone call a Discord user who lives in another community of the bridge. The bot posts a relay-style message (webhook-styled where `/webhooks` is enabled) containing `<@user>` into a random Discord chat of the bridge — chats other than the origin are preferred, and among them ones where the target actually is a member, so the ping can reach them. Each target user can be mentioned at most **once per hour** (shared across all chats and both platforms).

### Polls

`/poll` starts an **anonymous** poll that is posted (with vote buttons) to every chat in the bridge. Only **verified** users (who accepted the forwarding consent) can vote; each user has one vote that they can change. On Discord, options are separate arguments (up to 5); on Telegram, the question, duration and options are separated by `|` (up to 10 options). Duration units: `1h`, `2d`, `1w`, `1m` (= 30 days); capped at 30 days.

When the timer expires the bot posts the results to every bridge chat, replying to that chat's poll message (or without a reply if the chat joined the bridge after the poll started). Deleting a poll message in a Discord chat closes the poll and deletes it everywhere (Telegram deletions aren't detectable by bots, so use the Discord side to cancel a poll).

### Rules reminders

`/remindrules` stores a rules text per bridge (on Telegram — taken from the replied-to message; on Discord — from the `text` argument or a `message_id` in the channel) and periodically re-posts it to **all** bridge chats at the configured interval (`2h`, `30m`, …), optionally holding off until at least N messages have been posted since the last reminder. `/remindrules disable` turns it off for the bridge.

### Dead chat ping

`/deadchat <role_id> <hours>` (Discord only) pings the given role in the channel whenever no one has written there for N hours, then waits for the next N-hour stretch of silence. `/deadchat disable` turns it off.

### Dead topic keep-alive

`/deadtopic enable` keeps a thread/topic from being auto-archived: after every 6 days without activity (checked at midnight UTC) the bot sends a phantom message and deletes it right away. `/deadtopic disable` turns it off.

### News channel auto-reactions

`/newschat add <emoji>` (Discord only) makes the bot automatically react with the configured emoji(s) to every new message in the channel — handy for news/announcement channels. `/newschat disable` turns it off.

### Whois

Replying to a relayed copy with `/whois` (or using the message context menu on Discord) reveals the original sender: platform, username/nickname, ID, profile details and — on Discord — online status (requires the privileged **Presence Intent**). Available to verified users and Bot Admins, rate-limited; on Telegram the reply self-deletes after a minute.

### Shadow bans

`/shadow-ban <user>` (Bridge Admins) silently drops a user from the relay: their new messages are deleted in the origin chat and never forwarded, with no notification to them.

### Localization

All bot-facing strings live in per-language JSON files under `src/i18n/` (`ru`, `uk`, `pl`, `en`, `es`, `pt`). Each entry carries a translation **status**: `verified` (🟩), `unverified` (🟧) or `untranslated` (🟥, a key missing relative to the reference `DEFAULT_LANG`).

- `/locale` shows each language with an emoji bar and the percentage of verified strings; `/locale <code>` sends that language's JSON file (so the reply codes are visible for use with the other commands).
- `/loc-compare <code>` compares one reply across all languages with status emoji.
- `/loc-suggest <lang> <code> <text>` forwards a translation suggestion to `SUPPORT_CHATS`, tagged with a unique dialog code.
- `/loc-reply <code> <text>` (Bot Admins) DMs the original suggester **and** posts the reply into the support chats, so the team can see how a suggestion was resolved; the dialog code is then removed. Suggestion codes are kept at most **1 year**.

### Cross-bot verification sync

When a **Discord** user accepts the forwarding consent, their ID is posted to the `VERIFIED` Discord channel; when they `/unverify` themselves on Discord, their ID is posted to `UNVERIFIED`. **Confederate Guard** watches the same channels to mirror users into / out of its cross-server verified database. Only Discord user IDs are published to these channels — Telegram verifications are tracked only in Confederate's own database, since Confederate Guard's database holds Discord IDs.

This sync is only useful when the bot runs together with [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard). Bot Admins can toggle the publishing at runtime with `/verify-list enable|disable` (enabled by default; the setting is stored in the database and survives restarts).

### Service events and automatic backups

Start/stop notices and daily health-check findings (unreachable chats, missing **Manage Messages** on Discord or delete rights on Telegram) go to the `SERVICE_CHATS` channels; a chat that stays unreachable for 24 hours is detached from its bridge automatically. Encrypted database backups (authenticated BLAKE2 keystream, standard library only) are posted to `BACKUP_CHATS` every 12 hours; `/backup` returns one on demand, and `python src/restore_backup.py <input.db.enc> <output.db>` decrypts it with the `BACKUP_KEY` environment variable.

---

## Data collection and retention

The bot stores operational data in local SQLite (`bridge.db`) to provide relaying, moderation, and automation features. The full privacy policy lives in [PRIVACY.md](PRIVACY.md).

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
