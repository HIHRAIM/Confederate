# Architecture

This is the map of the code: how a message travels, what the moving parts are called, and where to find the code behind each feature. The commands themselves are documented in [README.md](README.md); this file is about structure.

## The one idea

Confederate connects *chats* into *bridges*. A chat is a single place messages appear: a Discord channel, thread or forum post (keyed `guild_id:channel_id`), a Telegram group or one topic of a forum group (keyed `chat_id:topic_id`, topic `0` for the plain group), or a user's DM (keyed `dm:user_id`, used only by the appeal system). A bridge is a numbered set of chats; everything posted in one chat of a bridge is copied by the bot into all the others.

Both halves of the bot run in one Python process on one asyncio loop: `discord.py` client and `aiogram` dispatcher side by side, sharing one SQLite database (`src/bridge.db`, WAL mode, guarded by a re-entrant lock in `db/__init__.py`). The two halves call each other through imports done at the call site (`from telegram_bot import bot as tg_bot` inside a function) — that is deliberate, it breaks the import cycle between them. Keep the pattern.

## The bridge-number space

One integer space, split at `APPEAL_BRIDGE_ID_FLOOR` = 100000 and served by two allocators that must never meet:

* **below the line — ordinary bridges.** `/atb <n>` takes the number the admin names (creating the bridge if it is new); `/atb new` asks `db/bridges.py: next_free_bridge_id` for the *lowest free* number. Holes are reused deliberately — a bridge disappears with its last chat, and its number returns to circulation instead of pushing a counter up forever. If everything below the line is taken, `/atb new` says so rather than spilling over.
* **at and above the line — appeal bridges.** `db/appeals.py: next_appeal_bridge_id` hands out max+1 within that range; these are short-lived (one per appeal, garbage-collected 30 days after the verdict), so holes there are not worth reusing.

Numbers are claimed with a single `INSERT … SELECT` so that two admins running `/atb new` at once — or one of them through the control panel, which is a separate process on the same file — cannot receive the same number.

## The path of a message

Discord → Telegram (the reverse is symmetrical):

1. `discord_bot/events.py: on_message` fires. Filters run in order: news-chat auto-reactions, dead-chat bookkeeping, bot-sender rules (`/allow-bots`, own-webhook detection), appeal-thread detour, shadow-ban delete, verification consent (unverified senders get a consent prompt and their message is held), per-user rate limit.
2. `discord_bot/relay.py: _relay_verified_discord_message` builds the relay payload: resolves the reply target to a `messages` row, extracts forwarded snapshots, embeds and attachments (`discord_bot/mentions.py`), splits a multi-attachment message into one relayed message per file.
3. `message_relay.py: relay_message` — the platform-neutral core. Records the message in `messages`, walks the target chats, renders per-target language and per-target form (plain text for Telegram, markdown for Discord), adds the `[Messenger | Community] Sender:` header, forward/reply prefix lines and file-count footers, then hands each target to a `send_to_chat` callback.
4. The callback delivers: `deliver_telegram_relay` (in `discord_bot/relay.py`) sends via the aiogram bot with an HTML body and a native reply reference when the replied-to copy exists in that chat; `deliver_discord_relay` chooses between a webhook copy and a plain bot message (see Webhooks).
5. Every delivered copy is recorded in `message_copies`. Later edits and deletes of the origin find the copies through that table and propagate; deleting any *copy* likewise deletes the origin and all other copies.

## Roles

* **Bot Admin** — hard-coded in `config.py: ADMINS`. Operates the bot itself: attaches chats to bridges, manages feeds, forces the bot out of chats.
* **Bridge Admin** — per-bridge moderator (`/setadmin scope: local`), stored in `bridge_admins`. May shadow-ban, set the bridge language, deadtopic.
* **Server Bridge Admin** — `/setadmin` without scope: same rights, but for every bridge the server/group takes part in, including bridges it joins later (`server_bridge_admins`; joining a bridge materializes the grant into `bridge_admins`/`chat_admins` rows — see `db/bridges.py: attach_chat`).
* **Chat Admin** — per-chat rows in `chat_admins`, written as a side effect of the grants above; checked by `utils.is_chat_admin`, which also honors the two server-wide roles.
* **Local Admin** — `/setlocaladmin`, stored in `server_admins`. Exists for the external control panel (scoped panel login), not for bot commands.
* **Localizer** — `/localizer-add`, stored in `localizers`. May edit this bot's localization through the control panel.

## Background loops

From `main.py` (cross-platform, started in `main()`):

| Loop                  | Period            | Job |
|-----------------------|-------------------|-----|
| `rules_loop`          | checks every 60 s | posts bridge rules on schedule (legacy twin of the client loop below) |
| `pending_cleanup_loop`| every 60 s        | expires consent prompts older than 24 h, expired verifications, old loc suggestions and polls |
| `poll_loop`           | every 30 s        | posts results of expired polls, closes them |
| `feed_loop`           | tick every 30 s   | polls followed sources; per-kind intervals in `FEED_POLL_INTERVALS` (telegram 60 s, wiki 90 s, bluesky 120 s, wiki discussions 180 s, youtube 5 min), one source per kind per tick, exponential backoff per source, flat per-host backoff on throttling |
| `daily_check_loop`    | every 24 h        | verifies every chat is reachable and the bot has delete rights; auto-detaches chats unreachable for 24 h |

From `discord_bot/client.py` (started in `DiscordBot.setup_hook`):

| Loop                     | Period             | Job |
|--------------------------|--------------------|-----|
| `deadchat_loop`          | every 5 min        | pings a role in chats silent longer than the configured hours |
| `status_loop`            | every 60 s         | rotates the presence text through the six languages with live member/community counts |
| `bridge_rules_loop`      | every 60 s         | posts bridge rules when both the interval and the message-count threshold pass |
| `deadtopic_loop`         | daily at 00:00 UTC | sends-and-deletes a phantom message in topics silent ≥ 6 days |
| `backup_loop`            | every 12 h         | encrypted `bridge.db` snapshot to the Discord/Telegram backup chats |
| `appeal_maintenance_loop`| every 24 h         | kicks Purgatorium members with no appeal after 7 days; garbage-collects appeals resolved > 30 days ago |

## Followed sources (feeds)

A feed attaches an outside source — a Bluesky account, a YouTube channel, a public Telegram channel, a MediaWiki, or a Fandom wiki's Discussions — to a chat. One row per `(kind, source, chat_id)` in the `feeds` table; `last_post_id` remembers the newest post already seen, so a new feed starts from "now" instead of replaying history.

A wiki on Fandom is two subscriptions under one key: `wiki` for recent changes and `wikidisc` for the forum. They are separate kinds because a Discussions post id comes from a different counter than `rcid` — around 4.4×10¹⁸ against a few million — and one shared position would let the first forum post push the remembered id past every future change and silently end the wiki's edit relay. They share one `wiki_feed_settings` row, so an admin still configures the wiki once.

Delivery targets are resolved by `db/feeds.py: feed_targets` **on every post**, not at attach time: if the chat is in a bridge, the post goes to every chat of the bridge *as of that moment*. That is why a chat that attaches a feed and joins a bridge later starts feeding the whole bridge with no extra code — the subscription follows the bridge membership automatically.

A wiki's filters and output settings follow the same rule. The `wiki_feed_settings` row is keyed on the `feeds` row it belongs to, not on the chat someone configured it from: `db/feeds.py: wiki_settings_chat` maps any chat of the bridge onto the subscription, so `/wikifeed-settings` run anywhere in the bridge is read everywhere in it, and an event either reaches every chat of the bridge or none. `discord_bot/wiki.py: relay_wiki_posts` reads the settings once per batch and filters once; what is still decided per chat is only the language of the sentence and whether Discord gets an embed.

Each kind is served by a reader module in `sources/` exposing the same three functions — `normalize_source()`, `source_url()`, `fetch_posts()` — returning post dicts with `id`, `text`, `link`, `author_name` (and optionally `media`, `forward_*`, `unavailable_media`). `discord_bot/feeds.py: feed_module()` maps kind → module; `relay_feed_post` renders posts through the normal relay with the source's name and a bundled avatar. Telegram channels are special: if the bot is an *admin* of the channel, posts arrive as `channel_post` updates (`live=1`, never polled); otherwise the public `t.me/s/` web preview is scraped by `sources/telegram.py`.

## Webhooks mode

With `/webhooks enable` a relayed copy in a Discord channel is sent through a per-channel webhook named "Confederate Bridge", carrying the original sender's name and avatar instead of the `[Messenger | Place] Sender:` header. Scopes: whole server (`server_webhooks`), one bridge (`bridge_webhooks`), plus legacy per-channel rows in `chat_settings.webhooks`.

Webhooks **cannot post into threads or forum posts** (that would need `thread=` targeting the parent channel's webhook; not implemented), so `deliver_discord_relay` checks `isinstance(channel, discord.Thread)` and silently falls back to a normal bot message there. DM chats never use webhooks. A webhook message also cannot carry a native reply reference or be edited as the bot's own message — replies become a markdown "(replying to …)" first line, and edits go through `webhook.edit_message` with the prefix lines rebuilt (`message_relay.build_discord_webhook_relay_body`).

## Appeals (Purgatorium)

The ban-appeal system runs together with the separate **Confederate Guard**: banned users land on the Purgatorium server (`config.PURGATORIUM_GUILD_ID`), run `/appeal`, and get a thread in `APPEAL_CHANNEL_ID` bridged to their DM. The bridge for it is an ordinary bridge whose id is allocated from a reserved range **at and above `APPEAL_BRIDGE_ID_FLOOR` = 100000** (`db/appeals.py: next_appeal_bridge_id`) — normal bridges must stay below.

Consuls (holders of the `CONSULS` roles) write in the thread; the appellant sees them anonymized as "Consul A/B/…" or under a `/setname` alias (`consul_names`, indexes per thread in `appeal_consuls`). Verdict buttons in the pinned message pardon (kick from Purgatorium + publish the user id to `APPEAL_PARDON_CHANNELS` for Confederate Guard to unban everywhere) or condemn (permanent ban). Confederate Guard also posts ban info into the thread — recognized by `GUARD_BOT_ID` and auto-pinned. Verification-state sync with Confederate Guard uses the same channel trick: `/verify`/`/unverify` publish bare user ids into the `VERIFIED`/`UNVERIFIED` channels (toggle: `/verify-list`).

## Feature → file

| Feature | Where the code lives |
|---|---|
| DB connection, schema, migrations | `db/__init__.py`, `db/schema.py` |
| Bridges, chats, attach/detach | `db/bridges.py`, commands in `discord_bot/commands/bridges.py`, `telegram_bot/commands/bridges.py` |
| Relay core (headers, replies, forwards, copies) | `message_relay.py` |
| Discord delivery, webhooks, edits/deletes | `discord_bot/relay.py` |
| Discord inbound events | `discord_bot/events.py` |
| Telegram inbound relay, albums, consent replay | `telegram_bot/relay.py` |
| Telegram file re-upload to GALLERY | `telegram_bot/files.py`, gallery helpers in `discord_bot/feeds.py` |
| Feeds: readers | `sources/bluesky.py`, `sources/youtube.py`, `sources/telegram.py`, `sources/wiki.py`, `sources/fandom.py` |
| Wiki: event meaning, filters, wording | `wiki_events.py` |
| Wiki: delivery, embeds, burst merging | `discord_bot/wiki.py` |
| Feeds: attach/relay/avatars, `FEED_KINDS` | `discord_bot/feeds.py`, live channel posts in `telegram_bot/feeds.py` |
| Feeds: polling scheduler | `main.py: feed_loop` |
| Appeals | `discord_bot/appeals.py`, storage in `db/appeals.py` |
| Verification & consent | `discord_bot/commands/user.py`, `telegram_bot/commands/user.py`, storage in `db/users.py` |
| Privacy switches | `db/users.py`, `/privacy` in the two `commands/user.py` |
| Polls | `discord_bot/commands/polls.py`, `telegram_bot/commands/polls.py`, storage in `db/polls.py` |
| Localization runtime | `utils.py` (`_load_i18n`, `localized*`), files in `src/i18n/` |
| Localization commands & suggestions | `discord_bot/commands/locale.py`, `telegram_bot/commands/locale.py` |
| Admin roles | `db/admins.py`, commands in the two `commands/admins.py` |
| Chat settings (lang, allow-bots, webhooks, files) | `db/settings.py`, commands in the two `commands/settings.py` |
| Mentions across the bridge | `/mention` in the two `commands/user.py`, send in `discord_bot/relay.py` |
| whois | the two `commands/user.py` |
| Dead chat / dead topic / news reactions / rules | `discord_bot/commands/settings.py`, loops in `discord_bot/client.py` |
| Encrypted backups | `backup_crypto.py`, `restore_backup.py`, loop in `discord_bot/client.py` |
| Service events (bot started, feed errors, …) | `main.py: send_service_event` |
| Markup conversion (TG entities ↔ Discord markdown, timestamps) | `message_relay.py` |
| Rate limiting | `utils.py: rate_limit_ok` |

## Neighbours

The parent folder holds sibling projects this repo cooperates with but never imports: **Confederate Guard** in `guard_bot/` (cross-server ban sync; talks to us through Discord channels only), `panel` (web control panel; reads our `src/config.py`, `src/.env`, `src/i18n/` and `bridge.db` directly from disk, launches `python main.py` with cwd `src/`), and `clean_code.py` (comment stripper — see `src/CLAUDE.md` for the hard rule it imposes).
