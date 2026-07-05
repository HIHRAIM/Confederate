# Confederate Privacy Policy

_Last updated: 2026-07-05_

Confederate is a self-hosted, open-source relay bot that bridges Discord channels/threads and Telegram chats/topics into shared conversation spaces. This document describes what data the software processes, why, for how long, and what choices users have.

> **Who is responsible for your data.** Confederate is software, not a service: anyone can run their own instance. The person or team operating a given instance (the **operator**) controls that instance's database, configuration and backups, and is the data controller for it. This document describes what the software itself does; a specific operator may add their own infrastructure (hosting, logging, backups) around it.

## What the bot processes

To relay a message between chats, the bot receives it through the Discord and Telegram APIs, reformats it (header with the sender's name and community, mentions, attachments, forward/reply attribution) and posts copies to the other chats of the bridge. **Relaying is the product**: by design, messages written in a bridged chat become visible in every other chat of that bridge, including communities and platforms the author may not be in.

Messages are only relayed for users who have accepted the **forwarding consent** (the prompt with the "Accept" button, also available via `/verify`). Until consent is given, a user's messages are not relayed: the first message is held back (see retention below) and later ones are deleted from the bridged chat.

## What the bot stores

All data lives in a local SQLite database (`bridge.db`) on the operator's machine.

| Data | Contents | Retention |
|---|---|---|
| Relay metadata | Bridge ID, origin platform/chat/message IDs, sender ID and display name, reply linkage, forward attribution (type and source name), timestamp, and the IDs of the posted copies | **30 days**, then deleted (this window powers edit/delete propagation) |
| Message content | **Not stored**, with four exceptions listed below | — |
| Pending consent | The first message of a not-yet-verified user (serialized, so it can be relayed once they accept), consent prompt IDs | Until accepted, at most **24 hours** |
| Verification records | Platform, user ID, chat where consent was accepted, timestamps | **365 days** from consent, then expired and deleted |
| Bridge rules | The rules text saved with `/remindrules` | Until `/remindrules disable` |
| Polls | Question, options, per-user votes (platform + user ID + chosen option; used to enforce one changeable vote per user — other users never see who voted for what) | Deleted **7 days** after the poll closes |
| Localization suggestions | Suggester's platform, ID and username, target language, reply code, suggested text | Until answered with `/loc-reply`, at most **1 year** |
| Admin and moderation lists | Chat admins, bridge admins, shadow-banned user IDs | Until changed/removed, or until the bot leaves the chat |
| Chat settings | Language (`/lang`, `/locallang`), `/allow-bots`, `/webhooks`, deadchat/deadtopic/newschat configuration | Until changed/removed, or until the bot leaves the chat |

The four content exceptions are: pending first messages (≤ 24 h), bridge rules, poll questions/options (≤ 7 days after close), and localization suggestions (≤ 1 year). Everything else content-wise exists only as ordinary messages inside your Discord/Telegram chats, not in the bot's database.

## Where data goes

- **Discord and Telegram.** Relayed copies are posted through the official APIs of both platforms and are subject to their own privacy policies.
- **Cross-bot verification sync (optional).** When the operator runs Confederate together with [Confederate Guard](https://github.com/HIHRAIM/Confederate-Guard), the **Discord user IDs** of users who accept or revoke the forwarding consent are posted to the configured `VERIFIED`/`UNVERIFIED` Discord channels, where Confederate Guard mirrors them into its cross-server verified database. Only bare Discord IDs are published — never message content, and never Telegram IDs. Operators can turn this off at runtime with `/verify-list disable`.
- **Avatar re-hosting.** With `/webhooks` enabled, a Telegram sender's profile photo is downloaded and re-uploaded to a Discord channel configured by the operator (`SERVICE_CHATS`), because Discord cannot fetch Telegram file URLs directly. The upload is cached per user and replaced when refreshed.
- **Backups.** Every 12 hours (and on `/backup`) the database is sent to the operator-configured backup channels — **always encrypted** (authenticated BLAKE2 keystream; the key never leaves the `BACKUP_KEY` environment variable on the operator's machine). The destination channels only ever store ciphertext.
- **Service notices.** Start/stop events and daily health-check findings (unreachable chats, missing permissions) go to operator-configured service channels. They contain chat IDs, not user data.

The software contains **no analytics, tracking, advertising or data-sale pipelines**, and sends nothing to the developers of Confederate.

## What other users can see

- Relayed copies show your **display name** and your community's name in the header (or as the webhook name/avatar where `/webhooks` is enabled).
- Verified users can use `/whois` on a relayed copy to see the original sender's platform, username, ID and — on Discord — online status. `/whois` is rate-limited and available only to verified users and bot admins.
- `/mention` lets users ping a Discord user by ID/username in another bridged community (at most once per hour per target).

## Your choices

- **Consent is opt-in.** Nothing you write is relayed until you accept the forwarding consent; declining simply means your messages stay local (and are removed from the bridged chat).
- **Revoking consent.** Send `/unverify` at any time; your future messages will no longer be relayed until you verify again.
- **Editing and deleting.** Editing or deleting your message within 30 days updates or deletes all relayed copies as well. After 30 days the linkage metadata is gone and copies must be removed manually by chat moderators.
- **Questions and erasure requests** (e.g. removal of verification records or relayed copies older than 30 days) should go to the operator of your instance — they hold the database. For questions about the software itself, open an issue in the repository.

## Security

The database is a local file readable only by the operator's environment; backups leave the machine only encrypted; bot tokens and the backup key are read from environment variables / a local `.env` file and are never written to the database or sent anywhere.

## Age requirements

Confederate runs on top of Discord and Telegram and inherits their minimum-age requirements; it performs no age verification of its own and is not directed at children.

## Changes

This document is versioned together with the source code. Material changes to what the software collects or shares will be reflected here and in the release notes.
