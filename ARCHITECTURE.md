# Architecture

This is the map of the code: how a message travels, what the moving parts are called, and where to find the code behind each feature. The commands themselves are documented in [README.md](README.md); this file is about structure.

## The one idea

Confederate connects *chats* into *bridges*. A chat is a single place messages appear: a Discord channel, thread or forum post (keyed `guild_id:channel_id`), a Telegram group or one topic of a forum group (keyed `chat_id:topic_id`, topic `0` for the plain group), a user's DM (keyed `dm:user_id`, used only by the appeal system), or the private chat of a *receiver bot* (platform `inbox`, keyed `bot_id:user_id` — see Inbox conversations). A bridge is a numbered set of chats; everything posted in one chat of a bridge is copied by the bot into all the others.

Both halves of the bot run in one Python process on one asyncio loop: `discord.py` client and `aiogram` dispatcher side by side, sharing one SQLite database (`src/bridge.db`, WAL mode, guarded by a re-entrant lock in `db/__init__.py`). The two halves call each other through imports done at the call site (`from telegram_bot import bot as tg_bot` inside a function) — that is deliberate, it breaks the import cycle between them. Keep the pattern.

## The bridge-number space

One integer space cut into three regions by two floors — `APPEAL_BRIDGE_ID_FLOOR` = 100000 and `INBOX_BRIDGE_ID_FLOOR` = 1000000 — and served by three allocators that must never meet:

* **below the first floor — ordinary bridges.** `/atb <n>` takes the number the admin names (creating the bridge if it is new); `/atb new` asks `db/bridges.py: next_free_bridge_id` for the *lowest free* number. Holes are reused deliberately — a bridge disappears with its last chat, and its number returns to circulation instead of pushing a counter up forever. If everything below the line is taken, `/atb new` says so rather than spilling over.
* **between the floors — appeal bridges.** `db/appeals.py: next_appeal_bridge_id` hands out max+1 *within that band*; these are short-lived (one per appeal, garbage-collected 30 days after the verdict), so holes there are not worth reusing. The upper bound is what keeps the allocator from reading the newest inbox conversation as the newest appeal.
* **at and above the second floor — inbox conversations.** `db/inbox.py: claim_inbox_bridge_id`, same max+1 shape; a conversation closes after 30 days of silence, so holes are not worth reusing here either.

Numbers are claimed with a single `INSERT … SELECT` so that two admins running `/atb new` at once — or one of them through the control panel, which is a separate process on the same file — cannot receive the same number. The inbox allocator does the same for a sharper reason: it is reached with no human in the loop, so two people writing to one receiver bot in the same instant would otherwise land in a single merged thread.

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
| `inbox_maintenance_loop` | every 24 h     | closes inbox conversations silent for 30 days |
| `setup_deadline_loop` | every 24 h        | leaves communities added more than 7 days ago that no Bot Admin ever set up |

Plus one polling task per registered receiver bot, started by `inbox.py: start_all_inbox_bots` and owned by `inbox.py: _runtimes` rather than by a loop.

From `discord_bot/client.py` (started in `DiscordBot.setup_hook`):

| Loop                     | Period             | Job |
|--------------------------|--------------------|-----|
| `deadchat_loop`          | every 5 min        | pings a role in chats silent longer than the configured hours |
| `status_loop`            | every 60 s         | rotates the presence text through the six languages with live member/community counts |
| `bridge_rules_loop`      | every 60 s         | posts bridge rules when both the interval and the message-count threshold pass |
| `deadtopic_loop`         | daily at 00:00 UTC | sends-and-deletes a phantom message in chats silent past their own window (`deadtopic_chats.days`: 6 from `/deadtopic`, 3 for inbox conversation threads) |
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

## Inbox conversations

The one part of the bot that runs *other* bots. An admin hands Confederate the token of another Telegram bot (`/setinbox`) and names the chats it should report into (`/setinboxchat`, once per chat). Every person who then writes to that bot opens a conversation: a bridge from the reserved range holding their private chat plus a thread in each Discord host channel and a topic in each Telegram host group. Naming a second host is what widens a conversation past two chats — one private chat reaching a Discord thread and a Telegram topic at once.

**Confederate opens the threads and topics, not the receiver bot.** On Discord there is no alternative — the receiver bot is a Telegram bot with no presence there. On Telegram there is one, and this is the cheaper side of it: Confederate already administrates the host group, while the receiver bot would have to be invited to every host and promoted with `can_manage_topics` first. Kept out of the groups, a registered bot costs exactly one long-poll connection for its own private chats.

Each receiver bot therefore gets an aiogram `Bot`, a `Dispatcher` of its own and one task (`inbox.py: _runtimes`). The dispatcher is deliberately *not* the main router: the handlers there assume the main bot's chats, and an update from a receiver bot reaching them would be relayed as though it came from a bridged group. One dispatcher per bot rather than one shared, because aiogram guards `start_polling` with a per-dispatcher lock.

Tokens are stored encrypted with `BACKUP_KEY` (`backup_crypto.py: encrypt_secret`, the same authenticated format as the backups) and are never logged or echoed; a deployment without that key refuses to register a bot rather than write one down in clear.

Two things differ from an ordinary bridge:

* **Consent.** Writing to a receiver bot *is* the consent — `/start` says what forwarding means — so the consent ladder is skipped for the private chat and for the host threads and topics alike. Unlike `/appeal`, no `verified_users` row is written: consenting to talk to one inbox is not consenting to be relayed everywhere else.
* **Anonymization.** With `/inboxanon` on, staff are signed `Staff A`, `Staff B`, … — a stable letter each, reserved per conversation (`inbox_staff`) the same way consul letters are. The substitution happens at the origin, so every copy carries the label, and the edit paths re-derive it so anonymity survives editing.

`/whois` inside a conversation answers only about the person writing to the bot. Staff are off limits from either side: they may be reading under a label, and the thread is a workplace rather than a bridge whose members agreed to be identifiable to each other. The person writing has no command surface at all — `/start` is the only command a receiver bot knows, and everything else they send is relayed as the text it is.

A conversation's thread and topic are named `<mark> <writer>`, where the mark is 🟩 (the writer spoke last), 🟨 (staff did) or ⬛ (closed) — `inbox_conversations.status` holds it so a rename is issued only when it changes, because Discord allows a thread two renames per ten minutes and a lively exchange would otherwise spend that budget on every message. A rename that is refused anyway is swallowed: a stale mark is harmless, a lost message is not.

Discord conversation threads register themselves with the `/deadtopic` keep-alive at `INBOX_DEADTOPIC_DAYS` = 3 rather than the command's 6 — nobody enabled it by hand here, so it has to act before the auto-archive window it protects against. `deadtopic_chats.days` carries the per-chat window; `touch_inbox_bridge` refreshes `last_message_ts` for copies the bot relays *into* the thread, which `on_message` would not count as activity.

The header is asymmetric. Staff get the ordinary `[Messenger | DM] Name:` line, with `inbox_place_name` supplying the localized DM marker exactly as the appeal system does for an appellant's DM; the writer gets `inbox_writer_header` — the name alone, since the platform and server behind an answer are both useless to them and a leak of who is answering. `/close-header` drops the staff-side header as well, per host community: the flag lives on `inbox_hosts.hide_header` rather than on the thread (made fresh per conversation, so a setting there would die with it) or on the bot (one team may want headers where another hosting the same bot does not). `db/inbox.py: get_inbox_host_of_community` maps a thread back onto its host through the server/group prefix they share, which is the scope the setting is defined at.

A conversation closes on `/close`, when its bot is unregistered, when its writer is banned from that bot (`/inboxban`, scoped to the one bot, unlike the bot-wide `/shadow-ban`), or after 30 days of silence. Closing tells both sides, marks the title ⬛, archives the thread, closes the topic and detaches every chat.

Reopening is deliberately asymmetric. A Telegram topic is *closed*, not deleted, and `inbox_topics` remembers which topic belongs to which writer in which host group — so their next message reopens that same topic and the group keeps one per person instead of accumulating one per exchange. Discord always gets a new thread: an archived one is cheap to leave alone, and a channel reads better as a list of conversations. A remembered topic that can no longer be reopened is forgotten and replaced.

Attachments cross through GALLERY like any other Telegram file, with two differences. They are downloaded through the *receiver bot* (`telegram_bot/files.py` takes a `source_bot`, because a `file_id` is meaningful only to the token it was issued to), and the consent asked is `db/inbox.py: inbox_file_relay_enabled` rather than the ordinary `bridge_file_relay_enabled`: the latter requires every chat of the bridge to be covered by an `/allow-files` consent, and the private chat at the heart of a conversation belongs to no community and never could be. The question is asked over the host chats alone — they are the communities whose GALLERY the files land in.

## The setup deadline

A community added to the bot has seven days (`setup_deadline.py: SETUP_GRACE_SECONDS`) to acquire a setting only a Bot Admin can make; otherwise the daily sweep leaves it and reports the departure to the service chats. "Set up" is `db/onboarding.py: community_is_configured`, which asks the six tables no one but a Bot Admin can put a first row into — `chats`, `feeds`, `inbox_hosts`, `chat_admins`, `server_admins`, `server_bridge_admins`. Everything else a community can configure is open to Chat and Bridge Admins, and those exist only because one of those grants created them, so a row anywhere else implies a row in one of the six.

Three guards decide who the rule can reach, and each answers a different failure:

* `bot_settings.setup_rule_since`, planted on the first start of the version that introduced the rule, is the grandfathering line. A community that joined before it is never examined — which is what stops a deployment from walking out of every server it was already sitting in the day the rule shipped.
* `setup_deadlines.settled_at`, written by the first sweep that finds a community configured, makes the check one-shot. Without it, a bridge detached years later would put the bot out of a door it had long since been welcomed through.
* The chats named in `config.py` are exempt outright — service, backup and support chats, `GALLERY`, the verification and appeal channels, Purgatorium. That infrastructure is the operator's own and is often attached to nothing.

The clock itself is asymmetric. Discord publishes `Guild.me.joined_at`, so the Discord half needs no stored time at all and cannot be confused by a restart, a lost event or a cold cache — the same reason the Purgatorium auto-kick reads `Member.joined_at`. Telegram publishes nothing of the kind, so `telegram_bot/client.py: my_chat_member_update` writes the row that starts the clock, and only when the old status was `left` or `kicked`: a promotion to administrator in a group the bot has been in for years must not read as a fresh arrival. A group with no row is never examined, which is exactly what every pre-rule group looks like.

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
| Inbox conversations: receiver bots, threads/topics, anonymization, bans | `inbox.py`, storage in `db/inbox.py`, commands in the two `commands/inbox.py` |
| Setup deadline: policy and sweep | `setup_deadline.py`, storage in `db/onboarding.py`, joins recorded in `discord_bot/events.py: on_guild_join` and `telegram_bot/client.py: my_chat_member_update` |
| Runtime secrets (receiver-bot tokens) | `backup_crypto.py: encrypt_secret` |
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
| Service events (bot started, feed errors, communities joined and left, …) | `utils.py: send_service_event` |
| Markup conversion (TG entities ↔ Discord markdown, timestamps) | `message_relay.py` |
| Rate limiting | `utils.py: rate_limit_ok` |

## Neighbours

The parent folder holds sibling projects this repo cooperates with but never imports: **Confederate Guard** in `guard_bot/` (cross-server ban sync; talks to us through Discord channels only), `panel` (web control panel; reads our `src/config.py`, `src/.env`, `src/i18n/` and `bridge.db` directly from disk, launches `python main.py` with cwd `src/`), and `clean_code.py` (a comment stripper run over the whole tree from time to time: it deletes every `#` comment and leaves docstrings alone, so anything worth keeping has to be a docstring — or, inside SQL, a `--` comment within the string literal).
