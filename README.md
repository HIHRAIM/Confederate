# Confederate

Confederate is a cross-platform relay bot that bridges Discord channels/threads/forum posts and Telegram chats/topics into shared conversation spaces (“bridges”). It forwards messages in both directions, supports moderation and admin delegation per bridge, relays the posts of followed Bluesky accounts, YouTube channels and Telegram channels into a bridge, turns the private chats of other Telegram bots into threads and topics your team can answer from either platform, runs a shared ban-appeal flow together with [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard), and includes quality-of-life automation (verification prompts, cross-bridge polls and mentions, dead-chat pings, periodic rules reminders, and language settings).

## Requirements

- Python **3.10+** (recommended 3.11+)
- A Discord bot token
- A Telegram bot token
- SQLite (uses local `bridge.db`, no external DB required)
- Python packages used by the project:
  - `discord.py`
  - `aiogram`
  - `aiohttp` (used directly by the feed readers — Bluesky, YouTube and the Telegram channel preview; also a dependency of the two above)

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
   pip install discord.py aiogram aiohttp
   ```

4. **Create config file**
   - Copy `config.example.py` to `config.py`.
   - Set environment variables (the example config reads tokens from env), or copy `src/.env.example` to `src/.env` and fill it in — the config loads it automatically (already-set environment variables take precedence):
     - `DISCORD_BOT_TOKEN` — your Discord bot token.
     - `TELEGRAM_BOT_TOKEN` — your Telegram bot token.
     - `BACKUP_KEY` — the key the automatic database backups are encrypted with, and the key the tokens of registered receiver bots are stored under (see [Inbox conversations](#inbox-conversations)). Without it the bot runs, but backups fail and `/setinbox` refuses to store a token. Keep a copy somewhere other than the server: if it is lost, every existing backup is unreadable and every registered receiver bot has to be re-registered.
   - Edit `config.py`:
     - `ADMINS["discord"]` and `ADMINS["telegram"]` — sets of numeric user IDs with global bot-admin rights.
     - `SERVICE_CHATS["discord"]` and `SERVICE_CHATS["telegram"]` — chat IDs where the bot sends startup/shutdown and health events. Telegram format: `"-1000000000000:0"` (chat\_id:thread\_id); Discord format: numeric channel ID.
     - `BACKUP_CHATS["discord"]` and `BACKUP_CHATS["telegram"]` — chat IDs where the bot sends automatic database backups every 12 hours. Same format as `SERVICE_CHATS`.
     - `SUPPORT_CHATS["discord"]` and `SUPPORT_CHATS["telegram"]` — chats that receive localization suggestions submitted via `/loc-suggest` (Discord as an embed, Telegram as a message). Same format as `SERVICE_CHATS`.
     - `GALLERY` — set of Discord channel IDs where the bot re-uploads files it hands out as links: Telegram files once `/allow-files` is enabled (see [Telegram file re-upload](#telegram-file-re-upload)) and the attachments of the posts of followed sources (see [Followed sources](#followed-sources-bluesky-youtube-and-telegram-channels)). The first reachable channel is used, and the bot needs **Attach Files** there. Boosting that channel's server raises the per-message upload limit. Leave the set empty to keep both mechanics off entirely.
     - `VERIFIED` — set of Discord channel IDs where a **Discord** user's ID is published, followed by the ID of the server they accepted the forwarding consent on (`<user_id> <guild_id>`, nothing else). **Confederate Guard** reads the same channel(s) to add them to its cross-server verified database; the server ID is what lets it tell that server apart from the other servers it grants the verify role on. Only Discord user IDs are published — Telegram verifications stay local to Confederate. Use the same ID in both bots' configs.
     - `UNVERIFIED` — set of Discord channel IDs where a **Discord** user's ID is published when they unverify themselves (`/unverify`). **Confederate Guard** reads the same channel(s) to remove them from its verified database. Use the same ID in both bots' configs.
     - `PURGATORIUM_GUILD_ID` / `PURGATORIUM_INVITE_URL` — the shared appeal server and the invite link `/appeal` hands to users who are not on it yet (see [Purgatorium appeals](#purgatorium-appeals)).
     - `APPEAL_CHANNEL_ID` — the channel on Purgatorium where `/appeal` opens one thread per appellant.
     - `GUARD_BOT_ID` — Confederate Guard's bot user ID: its ban-summary messages in appeal threads are pinned instead of being relayed to the appellant's DM.
     - `APPEAL_PARDON_CHANNELS["discord"]` — channel(s) where the appellant's bare user ID is posted once the consuls grant an appeal, so Confederate Guard can lift their bans.
     - `APPEAL_BANINFO_CHANNELS["discord"]` — channel(s) where `<user_id> <thread_id>` is posted for every new appeal so Confederate Guard can publish the appellant's ban summary into the thread.
     - `CONSULS` — role IDs on Purgatorium whose holders may use the appeal verdict buttons.
     - `WIKI_CONTACT` (optional) — a link or address the operators of a followed wiki can use to reach you. It goes into the User-Agent of every request the wiki relay makes, which MediaWiki's API etiquette requires (Wikimedia answers 403 without one). Defaults to this project's repository if unset.

   > The `VERIFIED`/`UNVERIFIED` mechanic is only needed when the bot runs alongside [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard). If you don't run Confederate Guard, leave the sets empty or turn the publishing off at runtime with `/verify-list disable` (it is enabled by default).

> **Presence intent:** `/whois` reports a member's online status (online/idle/dnd), which requires the privileged **Presence Intent** — enable it for the bot in the Discord Developer Portal, otherwise the bot will not start.

5. **Run the bot**
   ```bash
   python src/main.py
   ```

---

## Project structure

The code lives in `src/`, split into packages by domain. `ARCHITECTURE.md` describes how the pieces work together and carries a feature → file table.

```
src/
  main.py              entry point: both bots plus the cross-platform loops
                       (feeds, polls, rules, consent cleanup, daily checks,
                       the setup deadline)
  config.py            this deployment's ids (untracked; config.example.py is the template)
  env_loader.py        reads src/.env
  utils.py             localization runtime, role checks, rate limiter
  message_relay.py     platform-neutral relay core and markup conversion
  wiki_events.py       what a wiki change means: grouping, filters, wording
  inbox.py             other Telegram bots whose private chats become threads
  setup_deadline.py    leaves communities nobody attached to a bridge
  backup_crypto.py     encrypted database snapshots and stored-token encryption
  restore_backup.py    their restore tool

  db/                  SQLite layer: one connection, one module per domain
    __init__.py          connection (conn/cur), init(), the whole public API
    schema.py            every CREATE TABLE + additive migrations
    bridges.py  messages.py  admins.py  users.py
    feeds.py    appeals.py   settings.py  polls.py  inbox.py
    onboarding.py        join times and the setup-deadline bookkeeping

  discord_bot/         the Discord half
    client.py            the client, its loops, Confederate Guard plumbing
    events.py            inbound events (messages, edits, deletes)
    relay.py             delivery, webhook copies, edit/delete propagation
    mentions.py          mention/embed/forward extraction
    appeals.py           Purgatorium appeal system
    feeds.py             feed kinds, avatars, GALLERY upload, attach/relay
    wiki.py              wiki delivery: filtering, embeds, burst merging
    commands/            slash commands: bridges, admins, settings, feeds,
                         user, polls, locale, wiki, inbox

  telegram_bot/        the Telegram half, mirroring the Discord one
    client.py            bot/dispatcher/router, polling, resolvers
    relay.py             inbound relay, albums, consent replay, edits
    files.py             file re-upload to GALLERY
    feeds.py             posts of followed channels the bot administrates
    commands/            same domains as the Discord side

  sources/             feed readers, one per kind, behind one interface
    bluesky.py  youtube.py  telegram.py  wiki.py  fandom.py

  i18n/                the six localization files
```

---

## Commands

Permission roles used below:

- **Everyone** — any user in the connected chat/channel.
- **Bridge Admins** — delegated moderators for a specific bridge (and/or chat-level admins managed by the bot).
- **Local Admins** — users delegated with `/setlocaladmin`: they hold chat-admin rights in every chat of one server/group and may manage that server through the [control panel](https://github.com/HIHRAIM/Confederate-Panel).
- **Localizers** — users granted `/localizer-add`: they may edit this bot's localization through the control panel. Bridge Admins and Local Admins hold the status implicitly while they keep those roles.
- **Consuls** — holders of a `CONSULS` role on the appeal server (see “Purgatorium appeals”): they decide appeal verdicts and may set their own alias with `/setname`. Bot Admins count as consuls implicitly.
- **Bot Admins** — global admins defined in `config.py` (`ADMINS`).

> Notes:
> - `/setadmin` covers every bridge the server/group takes part in, including bridges it joins later; `scope: local` (Telegram: a trailing `local`) narrows it to the current bridge.
> - On Discord, bridge/chat management permissions are enforced through bot-managed admin checks.
> - Telegram `/rfb` must be run inside the chat/topic to be removed — removal by ID is not supported.

### Discord commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id \| new>` | Attach current Discord channel to a bridge — `new` opens one on the lowest free number | ❌ | ❌ | ✅ |
| `/setbskyfeed <account>` · `/rembskyfeed <account>` | Attach/detach a public Bluesky account: its posts are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/setytfeed <channel>` · `/remytfeed <channel>` | Attach/detach a public YouTube channel: its uploads are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/settgfeed <channel>` · `/remtgfeed <channel>` | Attach/detach a public Telegram channel: its posts are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/setwikifeed <link>` · `/remwikifeed <link>` | Attach/detach a MediaWiki: its activity is relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/wikifeed-settings <link> <option>=<value>` | Filters and output format of a followed wiki | ❌ | ✅ | ✅ |
| `/rfb [channel_or_chat_id]` | Remove channel from a bridge (current channel if no ID given) | ❌ | ✅ | ✅ |
| `/setadmin <user> [scope: local]` | Grant Bridge Admin rights across every bridge of this server — or, with `local`, in this bridge alone; DMs the user | ❌ | ✅ | ✅ |
| `/remadmin <user> [scope: local]` | Revoke Bridge Admin rights across this server, or in this bridge alone | ❌ | ❌ | ✅ |
| `/setlocaladmin <user>` · `/remlocaladmin <user>` | Grant/revoke server-wide Local Admin rights (ping or ID); DMs the user | ❌ | ❌ | ✅ |
| `/localizer-add <user>` · `/localizer-rem <user>` | Grant/revoke Localizer status — localization editing in the control panel; DMs the user | ❌ | ❌ | ✅ |
| `/deadchat <role_id\|disable> <hours>` | Ping a role after N hours of inactivity in the channel | ❌ | ✅ | ✅ |
| `/deadtopic <enable\|disable>` | Post a phantom message every 6 days to keep the thread alive | ❌ | ✅ | ✅ |
| `/newschat <add <emoji>\|disable>` | Auto-react to new messages in channel | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m\|disable> [messages] [message_id] [text]` | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole server (used wherever no `/locallang` override is set) | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for this channel/thread (overrides the server-wide `/lang`) | ❌ | ✅ | ✅ |
| `/webhooks <enable\|disable> [scope: local]` | Relay incoming messages as per-sender webhooks (avatar + name), for the whole server or — with `local` — for this bridge only | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/wikifeeds` | List the wikis followed here and their settings | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message, or context menu on message) | Show original sender identity (incl. online status) | ✅ | ✅ | ✅ |
| `/privacy` | Choose what the bot may share about you: hide yourself from `/whois`, hide your avatar in webhook copies, refuse `/mention` pings | ✅ | ✅ | ✅ |
| `/mention <user_id_or_username>` | Mention a Discord user from another bridge community: posts a relay-style message pinging them into a random bridge Discord chat; 1-hour cooldown per target user | ✅ | ✅ | ✅ |
| `/poll <text> <duration> <option1> <option2> [option3…5]` | Start an anonymous poll in every bridge chat; verified users vote via buttons; results post on expiry (max 30 days, up to 5 options) | ✅ | ✅ | ✅ |
| `/appeal` | File a ban appeal: opens a thread on the appeal server bridged to the user's DM (see “Purgatorium appeals”); works in DM after joining the appeal server | ✅ | ✅ | ✅ |
| `/locale [code]` | Show localization status (bar + verified %), or send a language's localization file (10-min per-server cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc-compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc-suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify [user]` | Unverify yourself (no argument), or another user (Bot Admins). Discord usage also notifies Confederate Guard via the `UNVERIFIED` channel | ✅ | ✅ | ✅ |
| `/verify-list <enable\|disable>` | Toggle publishing of (un)verified Discord user IDs to the `VERIFIED`/`UNVERIFIED` sync channels (enabled by default; only needed alongside Confederate Guard) | ❌ | ❌ | ✅ |
| `/setinbox <token>` | Register a Telegram bot whose private chats become threads and topics here — or pass the token again to rotate it (see “Inbox conversations”) | ❌ | ❌ | ✅ ⁽²⁾ |
| `/reminbox [bot]` | Unregister a receiver bot: its open conversations are closed and its polling stops | ❌ | ❌ | ✅ ⁽²⁾ |
| `/setinboxchat <bot>` | Open this channel to a receiver bot's conversations — each person writing to it gets a thread here | ❌ | ❌ | ✅ ⁽²⁾ |
| `/reminboxchat [bot]` | Stop opening a receiver bot's conversations here; threads already open keep working | ❌ | ❌ | ✅ ⁽²⁾ |
| `/inboxanon <enable\|disable> [bot]` | Sign staff as “Staff A”, “Staff B”, … to the people writing to a receiver bot | ❌ | ❌ | ✅ ⁽²⁾ |
| `/inboxlist` | List the registered receiver bots, their state, host chats and open conversations (non-admins see only their own) | ❌ | ❌ | ✅ |
| `/close` | Close the inbox conversation of this thread — run inside it; the title goes ⬛ | ❌ | ✅ ⁽³⁾ | ✅ |
| `/close-header <hide\|show> [bot]` | Hide the `[Telegram \| DM] Name:` line from the copies a receiver bot's conversations deliver into this server | ❌ | ✅ | ✅ |
| `/inboxban [user] [bot]` | Bar a user from one receiver bot and close their conversation; no arguments needed inside their thread | ❌ | ❌ | ✅ ⁽²⁾ |
| `/inboxunban [user] [bot]` | Let a banned user write to a receiver bot again | ❌ | ❌ | ✅ ⁽²⁾ |
| `/setname [name] [user]` ⁽¹⁾ | Set the alias appellants see instead of “Consul A”. Without `name` it resets to the anonymous signature | ❌ | ❌ | ✅ |
| `/loc-reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/list_chats` | List all Discord guilds and Telegram groups known to the bot | ❌ | ❌ | ✅ |
| `/force_leave <platform> <id>` | Force bot to leave a guild/chat and clean up DB records | ❌ | ❌ | ✅ |
| `/allow-bots <enable\|disable>` | Allow or block relay of bot/webhook messages from this channel | ❌ | ✅ | ✅ |
| `/allow-files <enable\|disable> [local]` | Consent to re-uploading Telegram files to Discord and handing out their links. Without `local` it covers every chat of this server, in any bridge; with `local`, every chat of this bridge | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

⁽¹⁾ `/setname` is a **Consuls** command — the table has no column for that role. Any consul may set their own alias, on the appeal server where their `CONSULS` role is visible; the `user` parameter, which changes someone else's alias, is Bot Admins only.

⁽²⁾ Also whoever registered that receiver bot: a Bot Admin hands in the first token, and from then on the bot answers to them and to its registrant alike — token rotation, host chats, anonymization, bans and unregistering. Registering a *new* bot stays Bot Admins only.

⁽³⁾ Closing a conversation is routine support work, so the host chat's own admins may do it besides the bot's registrant and the Bot Admins.

### Telegram commands

| Command | Purpose | Everyone | Bridge Admins | Bot Admins |
|---|---|:---:|:---:|:---:|
| `/atb <bridge_id \| new>` | Attach current Telegram chat/topic to a bridge — `new` opens one on the lowest free number | ❌ | ❌ | ✅ |
| `/setbskyfeed <account>` · `/rembskyfeed <account>` | Attach/detach a public Bluesky account: its posts are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/setytfeed <channel>` · `/remytfeed <channel>` | Attach/detach a public YouTube channel: its uploads are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/settgfeed <channel>` · `/remtgfeed <channel>` | Attach/detach a public Telegram channel: its posts are relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/setwikifeed <link>` · `/remwikifeed <link>` | Attach/detach a MediaWiki: its activity is relayed to every chat of this bridge | ❌ | ✅ | ✅ |
| `/wikifeed-settings <link> <option>=<value>` | Filters and output format of a followed wiki | ❌ | ✅ | ✅ |
| `/rfb` | Remove current chat/topic from a bridge (run inside the target chat) | ❌ | ✅ | ✅ |
| `/setadmin <user_id_or_username> [local]` | Grant Bridge Admin rights across every bridge of this group — or, with `local`, in this bridge alone; DMs the user | ❌ | ✅ | ✅ |
| `/remadmin <user_id_or_username> [local]` | Revoke Bridge Admin rights across this group, or in this bridge alone | ❌ | ❌ | ✅ |
| `/setlocaladmin <user>` · `/remlocaladmin <user>` | Grant/revoke group-wide Local Admin rights (ID, `@username`, or reply); DMs the user | ❌ | ❌ | ✅ |
| `/localizer_add <user>` · `/localizer_rem <user>` | Grant/revoke Localizer status — localization editing in the control panel (ID, `@username`, or reply); DMs the user | ❌ | ❌ | ✅ |
| `/lang <ru\|uk\|pl\|en\|es\|pt>` | Set the default bot language for the whole group (used wherever no `/locallang` override is set) | ❌ | ✅ | ✅ |
| `/locallang <ru\|uk\|pl\|en\|es\|pt>` | Set bot language for the current chat/topic (overrides the group-wide `/lang`) | ❌ | ✅ | ✅ |
| `/remindrules <5h\|30m> [messages]` (as reply) | Post rules to all bridge chats on a schedule | ❌ | ✅ | ✅ |
| `/bridge` | Show the bridge, connected chats, and bridge admins | ✅ | ✅ | ✅ |
| `/wikifeeds` | List the wikis followed here and their settings | ✅ | ✅ | ✅ |
| `/verify` | Request/refresh user verification prompt | ✅ | ✅ | ✅ |
| `/whois` (as reply to relay message) | Show original sender identity | ✅ | ✅ | ✅ |
| `/privacy` | Choose what the bot may share about you: hide yourself from `/whois`, hide your avatar in webhook copies, refuse `/mention` pings | ✅ | ✅ | ✅ |
| `/mention <user_id_or_username>` | Mention a Discord user from another bridge community: posts a relay-style message pinging them into a random bridge Discord chat; 1-hour cooldown per target user | ✅ | ✅ | ✅ |
| `/poll <text> \| <duration> \| <option1> \| <option2> \| …` | Start an anonymous poll in every bridge chat (pipe-separated, up to 10 options, max 30 days) | ✅ | ✅ | ✅ |
| `/locale [code]` | Show localization status, or send a language's localization file (10-min per-group cooldown for the file) | ✅ | ✅ | ✅ |
| `/loc_compare <code>` | Compare a reply across all languages with status emoji | ✅ | ✅ | ✅ |
| `/loc_suggest <lang> <code> <text>` | Suggest a localization; sent to the support chats | ✅ | ✅ | ✅ |
| `/help` | Show command reference | ✅ | ✅ | ✅ |
| `/shadow-ban <user_id_or_username>` | Shadow-ban a user (messages silently not relayed) | ❌ | ✅ | ✅ |
| `/unverify [user_id_or_username]` | Unverify yourself (no argument), or another user (Bot Admins) | ✅ | ✅ | ✅ |
| `/loc_reply <code> <text>` | Reply (via DM) to a user's localization suggestion | ❌ | ❌ | ✅ |
| `/setinbox <token>` | Register a Telegram bot whose private chats become threads and topics here — or pass the token again to rotate it. Private chat with this bot only; the message is deleted (see “Inbox conversations”) | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/reminbox [bot]` | Unregister a receiver bot: its open conversations are closed and its polling stops | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/setinboxchat <bot>` | Open this forum group to a receiver bot's conversations — each person writing to it gets a topic here | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/reminboxchat [bot]` | Stop opening a receiver bot's conversations here; topics already open keep working | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/inboxanon <enable\|disable> [bot]` | Sign staff as “Staff A”, “Staff B”, … to the people writing to a receiver bot | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/inboxlist` | List the registered receiver bots, their state, host chats and open conversations (non-admins see only their own) | ❌ | ❌ | ✅ |
| `/close` | Close the inbox conversation of this topic — run inside it; the title goes ⬛ | ❌ | ✅ ⁽²⁾ | ✅ |
| `/close-header <hide\|show> [bot]` | Hide the `[Telegram \| ЛС] Name:` line from the copies a receiver bot's conversations deliver into this group | ❌ | ✅ | ✅ |
| `/inboxban [user] [bot]` | Bar a user from one receiver bot and close their conversation; no arguments needed inside their topic | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/inboxunban [user] [bot]` | Let a banned user write to a receiver bot again | ❌ | ❌ | ✅ ⁽¹⁾ |
| `/allow_bots <enable\|disable>` | Allow or block relay of bot messages from this chat | ❌ | ✅ | ✅ |
| `/allow_files <enable\|disable> [local]` | Consent to re-uploading this group's files to Discord and handing out their links. Without `local` it covers every chat of this group, in any bridge; with `local`, every chat of this bridge | ❌ | ✅ | ✅ |
| `/backup` | Send current database backup file | ❌ | ❌ | ✅ |

⁽¹⁾ Also whoever registered that receiver bot — see the same note under the Discord table. ⁽²⁾ Also the host group's own admins.

> Telegram command names use underscores where Discord uses hyphens (`/loc_compare` ↔ `/loc-compare`); both spellings are accepted on Telegram. The `/inbox*` commands are spelled the same on both platforms.

---

## Mechanics

Every user-facing mechanic of Confederate, in one place.

### Message relay

Chats attached to the same bridge (`/atb`) exchange messages in both directions. Relayed copies carry a `[{Messenger} | {Community}] {Sender}:` header, native replies are preserved (or represented with a link/note where the platform can't reference the original), edits and deletions of the original propagate to all copies for 30 days, and attachments/stickers/voice/video notes are represented with localized markers or links (Telegram files can be re-uploaded in full instead — see [Telegram file re-upload](#telegram-file-re-upload)). Messages from other bots and webhooks are relayed only where `/allow-bots` enables it, and a copy of one carries a bot marker after the sender's name (a custom emoji on Discord, 🤖 on Telegram).

Formatting is translated between the platforms rather than dropped: Telegram entities become Discord markdown and back, a message carrying several files is relayed as one message per file so every one of them gets a preview, Discord embeds are flattened into the text (author, title, description, fields, image links, footer), and Discord's `<t:…>` timestamp markup — which Telegram cannot render — is written out as a readable date in the target chat's language. Discord's own system notices (a member joining, a boost, a pin, a thread being created) are relayed as localized one-line notes.

### Telegram file re-upload

By default a file sent on Telegram is only *represented* in the relayed copies — as a localized `[N files from Telegram]` marker, or as a link to the original message where the group has a public username. Telegram file URLs embed the bot token, so they can't simply be handed out.

`/allow-files` turns on re-uploading instead: the bot downloads the files, posts them into the first reachable `GALLERY` channel as **one message per source Telegram message** (an album counts as one), and hands out the resulting Discord CDN links in the copies. On Discord all links go into the message footer, the first one expanding into a preview as usual; on Telegram the first link joins the message body and the remaining files follow as separate messages, one per file.

The consent is deliberately coarse and explicit, because the links are public to anyone who has them (see [PRIVACY.md](PRIVACY.md)). It comes in two forms, and both cover chats that join later:

- `/allow-files enable` — for the whole Discord server or Telegram group, in every bridge it takes part in;
- `/allow-files enable local` — for every chat of the current bridge.

**A bridge re-uploads only when every one of its chats is covered**, through its own server/group consent or through the bridge-wide one — consent covers both sending a chat's files and receiving such copies. A single uncovered chat puts the whole bridge back on the marker/link behaviour, and this is rechecked on every message rather than cached, since a chat may have joined a minute ago.

Limits are handled per file: Telegram's `getFile` serves at most 20 MB and Discord accepts 10 MB on a non-boosted server (more if `GALLERY`'s server is boosted). Files that fit are uploaded and linked; the ones that don't keep the old marker/link footer, so one message can carry both. The same fallback applies to any other failure — an unreachable `GALLERY` channel, a missing **Attach Files** permission, a failed download — the message always arrives, if only in the older form. Stickers, voice messages and video notes are never re-uploaded; they keep their own markers.

Editing the original re-uploads its files and updates the copies. `GALLERY` messages are **kept for as long as the channel keeps them**: the bot never deletes a re-upload because of its age, so the links handed out in the copies stay working. A re-upload is removed only when the message it belongs to is — deleting the original Telegram message takes its `GALLERY` message along, as does an edit that replaces the attachments.

### Verification and forwarding consent

The first time someone writes in a bridged chat, the bot replies with a localized consent prompt (an "Accept" button). Until they accept, their messages are not relayed: the first message is stored and relayed after consent, later ones are deleted. Consent is stored per platform and is valid for **365 days**; accepting it once covers every bridged chat of that platform. `/verify` re-issues the prompt, `/unverify` revokes consent (Bot Admins can unverify others).

### Languages

Replies and relayed service texts are localized **per target chat**. The language is resolved in this order: the channel/topic's own `/locallang` setting → the server/group-wide default set with `/lang` (Bridge Admins) → English. The bot's Discord presence line rotates through all six languages.

### Webhooks relay

`/webhooks enable` makes incoming relayed messages appear as **webhook** messages — the webhook's name is `{sender} [{server}]` (matching the `[{platform} | {server}] {sender}` header of normal relayed messages) and its avatar is the sender's avatar. One webhook per channel is created and reused, with per-message name/avatar overrides, and edits to an original message are propagated to its webhook copy.

Like `/allow-files`, the setting comes in two scopes, and both cover channels that join later:

- `/webhooks enable` — every channel of this Discord server, in every bridge it takes part in;
- `/webhooks enable scope: local` — this server's channels in the current bridge only.

`disable` turns off the scope it is given, and also clears the per-channel setting written by older versions of the command in the channel it is run in. Webhooks don't exist in threads and forum posts, so copies there keep the ordinary `[{platform} | {server}] {sender}:` format whatever the setting says; the bot also needs the **Manage Webhooks** permission, without which it silently falls back to normal relay messages.

Webhook messages can't carry a native Discord reply reference, so when the relayed message is a reply, the bot prepends a localized first line — e.g. `(replying to [{sender}'s message](link))` — whose bracketed text links to the replied-to message in the same channel. If the replied-to message can't be resolved, the usual "reply to an unknown message" line is shown instead.

Telegram avatars can't be a webhook avatar directly (the Telegram file URL embeds the bot token and isn't reliably fetched by Discord), so Telegram senders get one of the bundled neutral pictures, picked by the last digit of their user ID. Discord senders who hid their avatar in [`/privacy`](#privacy) get the same treatment.

### Forwarded messages

Relayed copies of forwarded messages get a localized attribution prefix. On Telegram the forward origin comes straight from the Telegram API. On Discord, forward snapshots intentionally omit the original author, so the bot resolves the original message through the forward reference: if it can read the source channel, the prefix is “(forwarded from {user's nickname})”; otherwise, if the source server is known to the bot, “(forwarded from {server name})”; otherwise “(forwarded from unknown source)”.

### Mentions across the bridge

`/mention <user>` lets anyone call a Discord user who lives in another community of the bridge. The bot posts a relay-style message (webhook-styled where `/webhooks` is enabled) containing `<@user>` into a random Discord chat of the bridge — chats other than the origin are preferred, and among them ones where the target actually is a member, so the ping can reach them. Each target user can be mentioned at most **once per hour** (shared across all chats and both platforms), and users who turned mentions off in [`/privacy`](#privacy) can't be called at all.

### Polls

`/poll` starts an **anonymous** poll that is posted (with vote buttons) to every chat in the bridge. Only **verified** users (who accepted the forwarding consent) can vote; each user has one vote that they can change. On Discord, options are separate arguments (up to 5); on Telegram, the question, duration and options are separated by `|` (up to 10 options). Duration units: `1h`, `2d`, `1w`, `1m` (= 30 days); capped at 30 days.

When the timer expires the bot posts the results to every bridge chat, replying to that chat's poll message (or without a reply if the chat joined the bridge after the poll started). Deleting a poll message in a Discord chat closes the poll and deletes it everywhere (Telegram deletions aren't detectable by bots, so use the Discord side to cancel a poll).

### Rules reminders

`/remindrules` stores a rules text per bridge (on Telegram — taken from the replied-to message; on Discord — from the `text` argument or a `message_id` in the channel) and periodically re-posts it to **all** bridge chats at the configured interval (`2h`, `30m`, …), optionally holding off until at least N messages have been posted since the last reminder. `/remindrules disable` turns it off for the bridge.

### Dead chat ping

`/deadchat <role_id> <hours>` (Discord only) pings the given role in the channel whenever no one has written there for N hours, then waits for the next N-hour stretch of silence. `/deadchat disable` turns it off.

### Dead topic keep-alive

`/deadtopic enable` keeps a thread/topic from being auto-archived: after every 6 days without activity (checked at midnight UTC) the bot sends a phantom message and deletes it right away. `/deadtopic disable` turns it off.

The window is per chat. Inbox conversation threads register themselves with it at **3 days** and need no command — an unanswered conversation must survive a weekend without anyone touching it (see [Inbox conversations](#inbox-conversations)).

### News channel auto-reactions

`/newschat add <emoji>` (Discord only) makes the bot automatically react with the configured emoji(s) to every new message in the channel — handy for news/announcement channels. `/newschat disable` turns it off.

### Whois

Replying to a relayed copy with `/whois` (or using the message context menu on Discord) reveals the original sender: platform, username/nickname, ID, profile details and — on Discord — online status (requires the privileged **Presence Intent**). Available to verified users and Bot Admins, rate-limited; on Telegram the reply self-deletes after a minute. A sender who turned `/whois` off in [`/privacy`](#privacy) is shown with their nickname and avatar only.

### Privacy

`/privacy` opens a personal menu — buttons on both platforms — where anyone can decide what the bot may share about them. The switches are per user and global: they hold in every chat of every bridge, on the platform the command was used on.

- **Whois** — `/whois` and the Discord context menu may then show no more than the person's nickname and avatar: no username, ID, status, bio, banner or registration date.
- **Avatar** — where a Discord channel has `/webhooks` enabled, copies of that person's messages carry a neutral placeholder instead of their real avatar, picked by the last digit of their ID exactly like the placeholders Telegram senders get.
- **Mentions** — `/mention` refuses to ping them from another community.

Nothing else about relaying changes: messages still carry the sender's nickname, since that is what makes a bridged conversation readable.

### Followed sources: Bluesky, YouTube and Telegram channels

A bridge can follow public sources that are not chats of its own:

- `/setbskyfeed <account>` — a public **Bluesky** account, given as `handle`, `@handle`, a link to the profile or a `did:plc:…` / `did:web:…` identifier. A handle can be reassigned by its owner, so a feed meant to outlive a rename is better attached to the DID;
- `/setytfeed <channel>` — a public **YouTube** channel, given as `@handle`, a `UC…` channel id, a legacy `/c/` or `/user/` name, or a link to any of them;
- `/settgfeed <channel>` — a public **Telegram channel**, given as the name after `t.me/`, `@name` or a link.

All three are Bridge Admins commands, each has a `/rembskyfeed` · `/remytfeed` · `/remtgfeed` counterpart, and `/bridge` lists what a bridge follows. A source is attached in the chat the command was run in and delivers to **every chat of that chat's bridge, including chats attached later**. A chat that is in no bridge yet may attach a source too: the posts go to that chat alone, and start reaching the whole bridge the moment it joins one. One bridge never follows the same source twice — attaching it again from another of its chats is refused.

Posts arrive as ordinary relayed messages: the header is `[Bluesky] {account}:`, `[YouTube] {channel}:` or `[Telegram] {channel}:` where webhooks are off, and where `/webhooks` is on the copy carries the source's name and its own avatar instead of that header. A repost keeps the bridge's usual “(forwarded from {account})” / “(forwarded from {channel})” line above the text. Each post is relayed as its text, a link to the post, and its attachments — re-uploaded to `GALLERY` first, so the chats get links the bot controls rather than links back to the source's CDN. Unlike the Telegram file re-upload, this needs no `/allow-files` consent: the posts are public to begin with and an admin chose to bring them in.

Attachments are fetched in the largest size that fits the `GALLERY` channel's upload limit — a Bluesky video is offered in several renditions — so a video normally arrives in a smaller size rather than not at all. Whatever still does not fit is represented by the same footer the Telegram file re-upload uses: `[2 files from Bluesky]`, `[1 file from Telegram]`. The text and the link to the post always arrive.

A **YouTube** upload is the exception: it is relayed as its title and its watch link and nothing else. Both Discord and Telegram turn a YouTube link into a preview card carrying the video's own thumbnail, so re-uploading that same still would put the picture in the message twice.

**Where the posts come from.**

- A **Bluesky** account is read through the public AppView (`public.api.bsky.app`) — the same documented, versioned API the web client uses, open to anyone with no key and no account. Replies the account writes to other people are filtered out at the source, so only its own posts and reposts are relayed; a shortened link is restored to its full target, since a relayed copy is plain text with no room for the markup that would otherwise hide it. A video's downloadable file lives on the account's own server rather than on the AppView's CDN, so that address is looked up once per account through the DID document (`plc.directory`, or the account's `did:web` host); if the lookup fails the post arrives with its poster frame instead of nothing.
- A **YouTube** channel is read through the channel's own Atom feed at `/feeds/videos.xml` — no API key, no quota and no developer project, so nothing here can be switched off for this bot in particular. The feed holds only the newest fifteen uploads and says nothing about Community-tab posts, so those are never relayed; Shorts and livestreams arrive among the uploads, because the feed does not mark them. A handle or legacy name is resolved to its `UC…` id once through the channel page and remembered. Nothing but the title and the watch link is taken from the feed — the thumbnail it offers is deliberately left alone.
- A **Telegram** channel is read through the Bot API when the bot is one of the channel's administrators, in which case posts are relayed the moment they are published; otherwise through the channel's public web preview (`t.me/s/<name>`), the page anyone gets by opening the channel link in a browser. If the bot later loses its admin rights in a channel it was following that way, the feed falls back to the web preview by itself rather than going quiet — everything published so far counting as seen, so the fallback does not replay the channel.

No developer account and no third-party service is involved in any of the three: the bot talks to Bluesky, YouTube and Telegram directly, and sends them nothing about your community.

The Telegram web preview is an undocumented page and can change or be withdrawn, and a channel whose owner turned the preview off cannot be read at all — for that one, add the bot to the channel as an administrator and run `/settgfeed` again. When a fetch fails the feed goes quiet and the bot reports it to `SERVICE_CHATS`, at most once an hour per source, instead of failing silently.

A feed can also go quiet because the source itself stopped: where a source dates its posts, the bot watches the newest one and reports a feed whose newest post has not moved in a fortnight — four months for YouTube, since a channel legitimately goes months between uploads — to `SERVICE_CHATS` once a day. `/setbskyfeed` and `/setytfeed` say the same thing in their reply at attach time, so a source that has plainly gone somewhere else is visible right away rather than through silence.

Polled sources are checked at a pace their host tolerates: the bot wakes every 30 seconds and asks at most one source of each kind, the one waiting longest, so a bridge with many sources of one kind never arrives at that kind's host as a burst. A Telegram channel is due about once a minute, a Bluesky account once every two minutes, a YouTube channel once every five — the Atom feed is a cached document that a new upload takes a few minutes to reach anyway. A source that stops answering is set aside for 15 minutes, doubling with each further failure up to 4 hours; a source that answers `429` is set aside for a flat 15 minutes together with the rest of its site, because there the far end is working and will take the bot back shortly. The newest post at the moment of attaching counts as already seen, so a new feed never dumps its backlog into the chats, and at most 5 posts per attached feed are relayed in one cycle.

### Wiki activity

`/setwikifeed <link to a wiki>` follows any MediaWiki and relays its recent changes into the bridge. Any link to the wiki works — an article, its main page, or its `api.php`; the bot finds the API endpoint itself and stores the subscription under one key, so two admins pasting two different pages do not follow the same wiki twice. Relaying starts from the moment of attaching: the history is never replayed. Like every other followed source, the subscription belongs to the **bridge** — chats that join later start receiving automatically — and `/remwikifeed` detaches it.

Unlike the other feed commands, this one answers to **Bot Admins and Bridge Admins** rather than chat admins, because the activity pours into every chat of the bridge. In a chat that has not joined a bridge yet there are no bridge admins to ask, so its own chat admins may set it up and the reply says the setting becomes the bridge's once it joins one.

**What is relayed.** Edits and page creations, file uploads, and the standard MediaWiki logs: deletions and restorations, moves, protection, blocks, rights changes, account creations, imports, history merges, tags, content-model and page-language changes.

Extensions are covered too, grouped so that one switch turns off one kind of noise. Which log types belong to which extension was taken from the log-type list the wikis themselves publish, not guessed:

- **`filters`** — Abuse Filter, and the spam and title blacklists (everywhere).
- **`wikifarm`** — ManageWiki, CreateWiki, DataDump, ImportDump and the wiki-request queue (Miraheze and other farms running that stack).
- **`global`** — cross-wiki account actions: GlobalBlocking, CentralAuth, global renames and vanishing (Wikimedia, Miraheze), and Phalanx, Fandom's equivalent.
- **`interwiki`** — the Interwiki table.
- **`translate`** — the Translate extension: page translation and translation review (translatewiki, mediawiki.org, Miraheze).
- **`approval`** — revision approval: ApprovedRevs, FlaggedRevs, and Wikipedia's PageTriage curation.
- **`structured`** — Cargo tables, Sprite sheets, TemplateClassification (Fandom-only).
- **`profile`** — social features: CurseProfile and article comments (Fandom and the Gamepedia-era wikis).

A log type the bot has never seen — every farm invents a few — still arrives, described generically, rather than being dropped.

**Fandom Discussions.** On a Fandom wiki that has Discussions switched on, `/setwikifeed` also follows the forum and says so; posts and replies arrive like any other event and are filtered by the `discussion` group. A Fandom wiki without the feature, and every wiki that is not on Fandom, simply gets the one stream and no mention of a feature it does not have. Discussions are tracked as a separate subscription under the same wiki key — their post ids come from a different counter than `rcid`, and sharing one position between the two streams would let the first forum post silently stop the wiki's edits from ever being relayed again — but they share the wiki's settings row, so it is still configured once.

**How it looks.** On Discord each change is an embed: the sentence links to the diff, the edit summary is the body, the wiki's name is in the footer, and the stripe colour follows the event type so a channel can be skimmed by colour. On Telegram the same change is one plain readable line with the link — not an embed with the fields stripped out. Both are written in the target chat's own language.

Relayed activity is attributed to the wiki by name and to its hosting: a change from a Fandom wiki is headed `[Fandom]`, one from Miraheze `[Miraheze]`, one from Wiki.gg `[Wiki.gg]`, and only a self-hosted wiki (or a farm the bot does not know) falls back to the generic `[Wiki]`. Where the bridge relays through webhooks, each wiki gets a webhook of its own, named after the wiki and wearing its hosting's logo — the `webhook` setting below turns that off in favour of one shared webhook per channel. A channel holds at most 15 webhooks, so a channel following many wikis will fall back to the shared one once that limit is reached.

**Bursts.** An active wiki can produce hundreds of edits a minute. Nothing is ever dropped — every change the poll fetched is delivered — but several changes to the same page collapse into a single line naming the page once, so a page being worked on hard reads as one event rather than fifteen. A poll reads up to 500 changes, the maximum the MediaWiki API serves in one request.

**Settings** belong to the subscription, so a wiki is configured once for the whole bridge: change them in any chat of the bridge and every chat that receives the wiki follows. `/wikifeeds` lists the followed wikis and their settings; `/wikifeed-settings` changes them:

| Option | Values | Meaning |
|---|---|---|
| `events` | `edit`, `new`, `upload`, `delete`, `move`, `protect`, `block`, `rights`, `newusers`, `import`, `merge`, `tag`, `contentmodel`, `pagelang`, `patrol`, `filters`, `wikifarm`, `global`, `interwiki`, `translate`, `approval`, `structured`, `profile`, `discussion`, `other` — or `all`/`none` | which event types to relay (patrol is off by default: it is noisy) |
| `namespaces` | namespace numbers, or `all` | which namespaces to relay (0 main, 6 File, 14 Category, …) |
| `bots` | `on`/`off` | relay edits flagged as made by a bot (off by default) |
| `minor` | `on`/`off` | relay edits flagged as minor |
| `format` | `embed`/`text` | how Discord shows the activity |
| `delivery` | `auto`/`message` | `auto` follows the bridge's `/webhooks` setting; `message` always uses a plain bot message |
| `webhook` | `own`/`shared` | `own` gives the wiki its own Discord webhook, named after it and wearing its hosting's logo; `shared` sends it through the bridge's common webhook instead |

On Discord these are typed options (`/wikifeed-settings wiki:… events:… bots:…`); on Telegram they are `key=value` pairs (`/wikifeed-settings example.org events=edit,new bots=off`). Both go through one validation path, so an unknown value is refused the same way on either platform instead of being stored.

Wiki activity reaches **threads and forum posts** as well as ordinary channels: Discord webhooks cannot post into a thread, so there the bot falls back to a normal message instead of losing the activity — and `delivery: message` forces that everywhere, for admins who want human relay through webhooks but wiki activity as plain bot messages.

Requests to a wiki carry a User-Agent naming this bot and a contact address (`WIKI_CONTACT` in the config), which MediaWiki's API etiquette requires — Wikimedia refuses anything else — and `maxlag`, so a wiki catching up with its database can defer the bot instead of being made slower. A wiki that rate-limits us, is unreachable, requires an account, or is not a MediaWiki at all each produce their own clear error rather than silence.

### Shadow bans

`/shadow-ban <user>` (Bridge Admins) silently drops a user from the relay: their new messages are deleted in the origin chat and never forwarded, with no notification to them.

### Localization

All bot-facing strings live in per-language JSON files under `src/i18n/` (`ru`, `uk`, `pl`, `en`, `es`, `pt`). Each entry carries a translation **status**: `verified` (🟩), `unverified` (🟧) or `untranslated` (🟥, a key missing relative to the reference `DEFAULT_LANG`).

- `/locale` shows each language with an emoji bar and the percentage of verified strings; `/locale <code>` sends that language's JSON file (so the reply codes are visible for use with the other commands).
- `/loc-compare <code>` compares one reply across all languages with status emoji.
- `/loc-suggest <lang> <code> <text>` forwards a translation suggestion to `SUPPORT_CHATS`, tagged with a unique dialog code.
- `/loc-reply <code> <text>` (Bot Admins) DMs the original suggester **and** posts the reply into the support chats, so the team can see how a suggestion was resolved; the dialog code is then removed. Suggestion codes are kept at most **1 year**.

### Cross-bot verification sync

When a **Discord** user accepts the forwarding consent, their ID and the ID of the server they accepted it on are posted to the `VERIFIED` Discord channel; when they `/unverify` themselves on Discord, their ID is posted to `UNVERIFIED`. **Confederate Guard** watches the same channels to mirror users into / out of its cross-server verified database. The server ID keeps Guard's notice on that server plain — without it, Guard announced every verification, even on the server where it had just happened, as "verified on another server". Only Discord user IDs are published to these channels — Telegram verifications are tracked only in Confederate's own database, since Confederate Guard's database holds Discord IDs.

This sync is only useful when the bot runs together with [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard). Bot Admins can toggle the publishing at runtime with `/verify-list enable|disable` (enabled by default; the setting is stored in the database and survives restarts).

### Purgatorium appeals

Together with Confederate Guard, the bot runs a shared ban-appeal flow on a dedicated appeal server (“Purgatorium”, `PURGATORIUM_GUILD_ID` in `config.py`). Guard invites banned users there and gate-keeps who can stay; Confederate handles the appeals themselves:

- **`/appeal`.** Available to members of Purgatorium (anyone else gets the invite link). It creates a thread named after the user in the `APPEAL_CHANNEL_ID` channel, bridges that thread with the user's **DM** using the regular relay machinery (replies, edits and deletions included), and pins a message with the verdict buttons. Sending the command counts as forwarding consent — no consent button is shown, and the consent is *not* published to the `VERIFIED` sync channel (a banned appellant must not enter Guard's verified database). The user's language is taken from their Discord client locale.
- **Ban summary.** After the thread is created, `<user_id> <thread_id>` is posted to the `APPEAL_BANINFO_CHANNELS` sync channel. Confederate Guard answers in the thread with everything it knows about the appellant's bans across its servers; that message is **pinned** in the thread and deliberately *not* relayed to the appellant's DM. Guard also attaches its own per-network unban buttons to that summary, so consuls can lift the appellant's ban in one network without deciding the appeal as a whole — those buttons are Guard's, and the verdict buttons below remain Confederate's.
- **Anonymous consuls.** Messages the appellant writes in DM appear in the thread under their real name; messages from server members in the thread reach the user's DM anonymized as “Consul A”, “Consul B”, … — a stable per-thread mapping. The signature is **not localized**: every appellant sees the English wording and the Latin alphabet whatever language they read the rest of the bridge in, so one consul is the same “Consul B” to everybody. No consent prompt fires inside appeal threads, and no avatars are forwarded.
- **Consul aliases (`/setname`).** A consul may replace that letter with a permanent alias of their own — up to 32 characters, pings and markdown stripped out. The alias is **one per consul and shared by every appeal**, present and future. It cannot be made to look like the bot's own “Consul A” signature — which is checked against all six languages, not only the English one the bot now signs with, so an alias like “Консул Б” is refused too — and it must be unique among consuls (compared case-insensitively). Changing or clearing an alias only affects **new** messages — copies already delivered keep the signature they were sent with. Inside the thread on Purgatorium consuls remain under their real Discord names; the alias only applies to the thread → DM direction. Calling `/setname` without a name resets to the anonymous signature, and the consul keeps the letter they had rather than being assigned a new one. Replies are always ephemeral, so the command is safe to run inside an appeal thread.

  The bot does **not** check an alias against real Discord nicknames, so impersonating another person this way is technically possible. This is treated as a moderation matter: a Bot Admin can overwrite or clear anyone's alias with `/setname <name> <user>`.
- **Verdicts.** The pinned buttons — usable by holders of a `CONSULS` role on Purgatorium, or Bot Admins, with an ephemeral confirmation step — either **unban** (the user ID is posted to the `APPEAL_PARDON_CHANNELS` sync channel, where Confederate Guard lifts all of the user's bans, network and local; the user is notified by DM and silently kicked from Purgatorium) or **permanently ban on Purgatorium** (executed directly, deliberately *not* recorded in any database; the user is notified by DM first). Either way the thread is archived and locked.
- **Housekeeping.** A daily sweep silently kicks Purgatorium members who spent more than 7 days on the server without filing an appeal (bots, Bot Admins and anyone holding a role are exempt). Appeals resolved more than 30 days ago are deleted together with their bridge attachments, and deleting an appeal thread cleans its records immediately.

### Inbox conversations

Confederate can serve the private chats of **other Telegram bots**. Give it the token of one (`/setinbox <token>`) and name the chats that bot should report into (`/setinboxchat <bot>`, run once in each of them) — from then on every person who writes to that bot opens a **conversation**: a thread in each Discord host channel, a topic in each Telegram host group, and their private chat, all in one bridge. Messages travel between them exactly as in any other bridge, replies, edits and deletions included.

Naming more than one host chat is the point of the second command: with a Discord channel and a Telegram forum group both registered, one incoming private chat reaches a thread *and* a topic, and a conversation spans three chats rather than two. A host chat can be added or dropped at any time; conversations already open keep working until they close.

- **Who may do what.** A Bot Admin registers the first token. From then on the bot answers to them and to whoever handed the token in — that person may rotate the token (just run `/setinbox` again with the new one), add and remove host chats, switch anonymization, ban users and unregister the bot. Registering a *new* bot stays Bot Admins only.
- **Threads and topics are opened by Confederate**, not by the receiver bot — the receiver bot never joins the host chats at all, and needs no rights anywhere. Confederate does: on Telegram a host must be a **forum group** where Confederate holds **Manage topics**, on Discord a channel where it holds **Create Public Threads** and **Send Messages in Threads**. `/setinboxchat` checks all of that on the spot and says exactly what is missing, rather than accepting the chat and failing silently when somebody finally writes.
- **The title says whose turn it is.** A conversation's thread and topic are named after the writer behind a status mark: **🟩** they wrote last and are waiting, **🟨** staff answered last, **⬛** closed. The mark leads the name so a channel list or topic list reads at a glance. It is rewritten only when it actually changes — Discord allows a thread two renames per ten minutes, and a busy exchange would burn that budget in a minute otherwise; a rename that does get rate-limited is skipped, leaving the previous mark until the next change.
- **Threads are kept alive.** A Discord conversation thread is registered with the [dead-topic keep-alive](#dead-topic-keep-alive) automatically, at 3 days rather than the 6 `/deadtopic` sets, so a conversation nobody has answered is not archived out from under the person waiting. The registration goes when the conversation closes.
- **Token handling.** The token is checked against Telegram before anything is stored, encrypted with `BACKUP_KEY` on the way into the database, and never logged, echoed or shown in a reply. A deployment with no `BACKUP_KEY` refuses to register a bot rather than write a token down in clear. On Telegram the command works **only in a private chat with Confederate**, and the message carrying the token is deleted immediately; sent in a group, it is deleted and refused.
- **Consent.** Writing to a receiver bot is itself the consent: `/start` answers with what forwarding means — that the message goes to the team's chat together with the writer's name and Telegram username, that answers come back, and that it goes nowhere else — and no consent button is shown afterwards. This consent does **not** verify the user anywhere else in the bot: agreeing to talk to one inbox is not agreeing to be relayed in bridged communities.
- **Anonymous staff (`/inboxanon enable`).** Everyone answering from a host chat reaches the writer as “Staff A”, “Staff B”, … — one stable letter each for as long as the conversation lasts, kept even if anonymization is switched off and on again. As with consul signatures the label is not localized, so one person is the same “Staff B” to everybody. Avatars are dropped along with the name, and edits keep the label. Switching it changes only **new** messages; copies already delivered keep the signature they were sent with.
- **The two sides read differently, on purpose.** Staff see the full `[Telegram | DM] Name:` header the appeal threads use: the platform and the DM marker are what tell a conversation apart from a bridged chat, and the name is who is on the other end. The writer sees just `Name:` — which platform an answer was typed on and which server it came from say nothing they can use, and naming the server would hand out an internal detail the “Staff A” anonymization exists to withhold. `/close-header hide` drops the staff-side header too, for teams whose threads read better without it: a thread is one person talking to one team, so the line largely repeats what the thread already says. It is scoped to one community and one receiver bot — another team hosting the same bot keeps its own answer — and applies from the next message on.
- **`/whois` answers about one person only** — whoever is writing to the bot. Staff are off limits: they may be reading under a label, and a conversation is a workplace rather than a bridge whose members agreed to be identifiable to each other. The writer has no commands of their own either: `/start` is the only one a receiver bot knows, and anything else they send is relayed as the text it is.
- **Bans (`/inboxban`).** Run inside someone's conversation it needs no arguments at all — the thread names both the bot and the person. It closes their conversation, tells them once, and drops everything they send that bot afterwards; `/inboxunban` lifts it, and their next message opens a new conversation. The ban covers **that receiver bot only** — `/shadow-ban` remains the bot-wide instrument.
- **Closing and reopening.** `/close` inside a conversation closes it; so does unregistering the bot, banning the writer, or **30 days** without a message from either side. Both sides are told, the title goes ⬛, the thread is archived and locked, the topic is closed, and the bridge goes.

  The writer's next message opens a conversation again — asymmetrically, and on purpose. On **Telegram the same topic is reopened**: it was closed rather than deleted, so the group keeps one topic per person with all the history in it instead of collecting a new one per exchange. On **Discord a new thread is created**: the archived one is left alone, and a channel reads better as a list of conversations than as a few threads reopened over months. If a remembered topic has since been deleted, a new one is made.
- **Attachments** cross where the host communities allow it. Files sent to a receiver bot are re-uploaded to the `GALLERY` channel and handed to the thread as links, exactly like [Telegram file re-upload](#telegram-file-re-upload) elsewhere — downloaded through the receiver bot's own token, since a `file_id` means nothing to any other bot. The consent asked is the **host chats'** `/allow-files`, not the writer's private chat, which belongs to no community and so could never carry one; with it off, files arrive as the usual localized `[N files from Telegram]` marker. Stickers, voice messages and video notes keep their own markers, and an album arrives as one message rather than one per file.

### The seven-day setup deadline

The bot is invited far more often than it is put to use, so a community it has just been added to has **seven days** to be set up: one of its channels, threads or topics attached to a bridge with `/atb`, or some other setting only a Bot Admin can make — a followed source, an inbox host, a Bridge Admin or a Local Admin grant. Nothing of the sort within the week and the bot leaves the server or group on its own, and says so in `SERVICE_CHATS`, where it also announces every community it is added to.

Nothing is said in the community itself, neither on arrival nor on the way out. Every setting the deadline asks for is a Bot Admin one, so the people who could act on a warning are the operators — and `SERVICE_CHATS` is exactly where they read.

The rule is deliberately narrow:

- **It never touches a community the bot was already in.** The deadline counts from the join, and the moment the rule came into force is recorded once, on the first start of the version that introduced it; everything that joined before that instant is out of reach for good.
- **It fires at most once per community.** The first daily sweep that finds a community set up settles it permanently — detaching every bridge years later does not put the bot out of the door.
- **It skips the deployment's own chats.** A server or group holding any chat named in `config.py` — `SERVICE_CHATS`, `BACKUP_CHATS`, `SUPPORT_CHATS`, `GALLERY`, `VERIFIED`/`UNVERIFIED`, the appeal channels or Purgatorium — is never left, however it is configured.

On Discord the week is measured from Discord's own record of when the bot joined, so a restart or a missed event cannot shorten it. Telegram publishes no such timestamp, so there the clock starts from the update that adds the bot to the group, and a group whose arrival the bot never saw is never examined.

Leaving is not a deletion: a community that is invited back starts a fresh seven days.

### Service events and automatic backups

Start/stop notices, the communities the bot is added to and the ones it leaves on the setup deadline, and daily health-check findings (unreachable chats, missing **Manage Messages** on Discord or delete rights on Telegram) go to the `SERVICE_CHATS` channels; a chat that stays unreachable for 24 hours is detached from its bridge automatically. Encrypted database backups (authenticated BLAKE2 keystream, standard library only) are posted to `BACKUP_CHATS` every 12 hours; `/backup` returns one on demand, and `python src/restore_backup.py <input.db.enc> <output.db>` decrypts it with the `BACKUP_KEY` environment variable.

---

## Data collection and retention

The bot stores operational data in local SQLite (`bridge.db`) to provide relaying, moderation, and automation features. The full privacy policy lives in [PRIVACY.md](PRIVACY.md).

### What data is stored

- **Bridge topology**
  - Bridge IDs, attached chat IDs, platform mapping.
- **Relayed message metadata**
  - Origin platform/chat/message IDs.
  - Origin sender ID and display name.
  - Reply linkage and forward attribution (type and source name).
  - Copy message IDs across platforms.
  - Creation timestamp.
- **Telegram file re-upload (`/allow-files`)**
  - Per-server/group and per-bridge consent records: platform, server/group or bridge ID, who enabled it and when.
  - `GALLERY` uploads: the channel and message holding the re-uploaded files, the CDN links handed out in the copies, the source Telegram message IDs, and the upload timestamp.
- **Admin and moderation data**
  - Chat admins, bridge admins, server-wide Local Admins and Localizers (with the username at delegation time, who delegated and when).
  - Shadow-ban records.
- **Automation settings**
  - Deadchat config (`role_id`, timeout, last activity timestamp).
  - Deadtopic config (phantom-message schedule per thread).
  - Newschat emoji reaction rules.
  - Rules reminder configuration (including the rules text itself).
  - Chat language settings, the `/allow-bots` toggle and the per-server/per-bridge webhook-relay scopes.
- **Polls**
  - Question, options, closing time, the per-chat poll messages, and one vote row per voter (platform, user ID, chosen option) — used to give each user one changeable vote.
- **Verification data**
  - Verified users with expiration timestamp.
  - Pending consent records for verification flows.
- **Appeal data**
  - Appeals: appellant user ID, thread ID, bridge ID, language, status and verdict metadata.
  - Consul anonymization map: thread ID, consul user ID, per-thread index (no names).
  - Consul aliases (`/setname`): consul user ID, the alias, its case-insensitive comparison form, who set it and when.
- **Localization suggestions**
  - `/loc-suggest` dialog codes: submitter platform/ID/username, target language, reply code, suggested text.
- **Privacy switches (`/privacy`)**
  - Platform, user ID and the three switches (whois, avatar, mentions), with the time they were last changed. A row exists only for users who opened the command.
- **Followed sources (`/setbskyfeed`, `/setytfeed`, `/settgfeed`)**
  - Kind (Bluesky, YouTube or Telegram), the handle and display name, the chat it was attached in, the channel's numeric ID where Telegram gives one, whether the posts arrive live, the ID of the last relayed post, who attached it and when.
- **Setup deadline**
  - One row per community added after the rule came into force: platform, server or group ID, when the bot joined, and — once the community has been set up — when that was noticed. No user is named.

### Retention periods

- **Message relay metadata (`messages` + `message_copies`)**: up to **30 days** (cleaned on startup).
- **`GALLERY` file re-uploads (`gallery_uploads` + the Discord messages themselves)**: kept **indefinitely** — there is no age-based deletion, so the links handed out in the relayed copies keep working. A re-upload is dropped together with the message's copies when a deletion is proxied, or replaced when an edit changes the attachments.
- **File re-upload consents (`/allow-files`)**: kept until turned off with `/allow-files disable`.
- **Pending consent records**: up to **24 hours** if not confirmed (cleaned continuously).
- **Verified user records**: default validity **365 days**, then auto-removed after expiry.
- **Localization-suggestion codes**: up to **1 year**, and removed immediately once answered with `/loc-reply`.
- **Appeal records (incl. the consul map)**: open appeals are kept while open; resolved appeals are deleted **30 days** after the verdict (daily sweep), or immediately if the appeal thread is deleted. Relayed appeal messages follow the regular 30-day metadata cleanup.
- **Consul aliases (`/setname`)**: kept **indefinitely**, until changed or reset with `/setname` (by the consul themselves or a Bot Admin). Unlike the consul anonymization map, they are deliberately **not** deleted together with the appeal after 30 days — an alias has to outlive the appeals it was used in, otherwise it would vanish after the first verdict. Losing the `CONSULS` role does not delete the alias either; it starts working again if the role comes back.
- **Privacy switches (`/privacy`)**: kept until the user changes them; never expired, so a protection stays on until it is switched off.
- **Polls (question, options, votes and poll messages)**: deleted **7 days** after the poll closes; deleting a poll message on Discord closes and removes it everywhere at once.
- **Followed sources (`/setbskyfeed`, `/setytfeed`, `/settgfeed`, `/setwikifeed`)**: kept until detached with `/rembskyfeed` / `/remytfeed` / `/remtgfeed` / `/remwikifeed`, or removed when the bot leaves the server/group whose chat they were attached in. A wiki's filter and format settings belong to the subscription and are deleted together with it.
- **Wiki relay bookkeeping**: each chat keeps the records of only its **100 most recent** relayed wiki changes, and nothing older than **30 days**. Wiki activity cannot be edited after the fact, so the rows exist only briefly to link the copies of one change across a bridge; past that window, deleting one relayed wiki message no longer removes the others.
- **Webhook-relay scopes (`/webhooks`)**: kept until turned off, or removed when the bot leaves the server.
- **Setup-deadline rows**: kept while the week runs, and afterwards as the note that the community was set up. Removed when the bot leaves the community, so that a re-invitation starts a fresh seven days.
- **Settings/admin/bridge mappings**: kept until manually changed/removed, or automatically cleaned when the bot leaves a server/chat.

### Data usage boundaries

- The bot uses stored data only to operate bridge relays, moderation, permissions, and automation.
- It does not implement analytics/tracking pipelines in this repository.
- Data is local to the bot runtime environment unless your deployment adds external backup/logging.

---

## Acknowledgements

The wiki-relay feature owes its existence to two earlier projects. Neither was copied from: the implementation here was written independently against [MediaWiki's own API documentation](https://www.mediawiki.org/wiki/API:Main_page), and no source code, comment text or localization string from either project appears in Confederate. They are named because their behaviour showed what such a feature should do.

- **[Wiki-Bot](https://github.com/Markus-Rost/discord-wiki-bot)** by Markus-Rost, licensed **ISC**. Wiki commands for Discord, and the original source of the idea that a chat bot should bring a wiki into a conversation. Confederate's wiki relay is *inspired by* it.
- **[RcGcDb](https://gitlab.com/chicken-riders/rcgcdb)** by Frisk (chicken-riders), licensed **GPL-3.0**. The backend that distributes wiki activity into Discord — the same job Confederate's wiki relay does. Confederate's *behaviour is modelled after* what that project demonstrates is worth relaying: which MediaWiki API queries answer the question, which kinds of events a wiki produces, and which fields of an event a reader actually cares about. Those are facts about MediaWiki and about the problem, not expression, and they were reimplemented from scratch here.

Confederate is licensed under **Apache-2.0**, and nothing in the wiki relay is derived from GPL-3.0 code.
