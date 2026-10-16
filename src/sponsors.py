"""Patreon sponsorship: what a tier is worth, who holds one, and what
happens when one lapses.

The bot never talks to Patreon. Patrons connect Patreon to Discord on
Patreon's own site, Patreon's bot hands them a role on the creator's server,
and the only thing read here is whether a Discord account holds one of the
roles named in `config.PATREON_TIER_ROLES`. There is no Patreon token, no
webhook, no OAuth and no public endpoint anywhere in this feature -- the
money, the proof of payment and the refunds are all Patreon's business. The
price of that simplicity is one honest limitation: a patron who has not
connected Patreon to Discord holds no role, and the bot cannot know they
exist.

The live role tier, effective tier during working grace, and subsequent
retention deadline are separate. A paid role has thirty days of continued
work, a community role one day; an observed new community role matures after
seven days. Thirty days after working grace, cleanup leaves communities that
have no replacement sponsor or Bot Admin and erases their scoped records.
Quotas remain shared across the sponsor's linked Discord and Telegram account.

The storage is db/sponsors.py; the commands are
discord_bot/commands/sponsors.py and telegram_bot/commands/sponsors.py; the
hourly reconciliation is main.py: sponsor_loop.
"""
import logging
import time

import db
from config import (
    PATREON_GUILD_ID, PATREON_TIER_ROLES, COMMUNITY_SPONSOR_ROLE_IDS,
    SPONSOR_NOTICE_CHANNEL,
)
from db.ranges import bridge_kind
from utils import DEFAULT_LANG, get_chat_lang, is_admin, localized, rate_limit_ok

logger = logging.getLogger("bridge.sponsors")

SPONSOR_COMMUNITIES = 2
SPONSOR_WIKI_FEEDS = 3
SPONSOR_SOCIAL_FEEDS = 3
SPONSOR_BRIDGES = 6
SPONSOR_CHATS_PER_BRIDGE = 2
SPONSOR_CHATS = SPONSOR_BRIDGES * SPONSOR_CHATS_PER_BRIDGE
SPONSOR_RELAY_MINUTE = 12
SPONSOR_RELAY_DAY = 1000
SPONSOR_TIER2_MULTIPLIER = 3
SPONSOR_TIER3_MULTIPLIER = 5

SPONSOR_GRACE_SECONDS = 30 * 86400
SPONSOR_RETENTION_SECONDS = 30 * 86400
SPONSOR_SLOT_CHANGE_SECONDS = 30 * 86400
SPONSOR_RECONCILE_SECONDS = 3600
COMMUNITY_WAIT_SECONDS = 7 * 86400
COMMUNITY_GRACE_SECONDS = 86400

def _rank(tier):
    """The free role ranks below the three paid tiers but above no role."""
    return 0 if tier == -1 else -1 if tier == 0 else tier

WIKI_FEED_KINDS = ("wiki",)
SOCIAL_FEED_KINDS = ("bluesky", "youtube", "telegram")

def sponsor_limits(tier):
    """What one tier is worth: communities, wikis and social sources.

    Tier 2 is tier 1 multiplied, not a second set of numbers, because these
    are the figures somebody will want to change and they should only have to
    change them once. `communities` counts servers and groups TOGETHER, in
    any mixture: two of the first tier means two Discord servers, or two
    Telegram groups, or one of each."""
    if tier == 0:
        return {"communities": 0, "wikis": 0, "socials": 0,
                "bridges": 0, "chats": 0, "relay_minute": 0, "relay_day": 0}
    factor = (SPONSOR_TIER3_MULTIPLIER if tier == 3 else
              SPONSOR_TIER2_MULTIPLIER if tier == 2 else 1)
    return {
        "communities": SPONSOR_COMMUNITIES * factor,
        "wikis": SPONSOR_WIKI_FEEDS * factor,
        "socials": SPONSOR_SOCIAL_FEEDS * factor,
        "bridges": SPONSOR_BRIDGES * factor,
        "chats": SPONSOR_CHATS * factor,
        "relay_minute": SPONSOR_RELAY_MINUTE * factor,
        "relay_day": SPONSOR_RELAY_DAY * factor,
    }

def _complain_once(key, message, *args):
    """Log a configuration problem at most once a day.

    The reconciliation runs every hour and a missing role is a standing
    condition, not an event: without this the log would carry the same line
    twenty-four times a day for as long as somebody took to notice it."""
    if rate_limit_ok(("sponsor-config", key), limit=1, window_seconds=86400):
        logger.warning(message, *args)

def configured_tier_roles():
    """The `{tier: role_id}` entries that are actually usable numbers.

    A tier whose role is missing from the configuration is simply a tier
    nobody can be found to hold; it is not an error, and the bot goes on
    working with whichever roles it does have."""
    roles = {}
    for tier, role_id in (PATREON_TIER_ROLES or {}).items():
        try:
            roles[int(tier)] = int(role_id)
        except (TypeError, ValueError):
            _complain_once(f"role-{tier}", "sponsor tier %s has no usable role id", tier)
    return roles

def _patreon_guild():
    """The creator's server as the Discord client sees it, or None when it
    cannot be read -- unconfigured, or the bot is not on it."""
    from discord_bot import bot as dc

    try:
        guild_id = int(PATREON_GUILD_ID)
    except (TypeError, ValueError):
        _complain_once("guild", "no usable PATREON_GUILD_ID is configured")
        return None
    guild = dc.get_guild(guild_id)
    if guild is None:
        _complain_once("guild-missing",
                       "the bot is not on the Patreon server %s", guild_id)
    return guild

def _tier_from_roles(role_ids):
    """The tier a set of role ids amounts to. Holding several of the roles
    counts as the highest of them -- Patreon can leave an old tier's role
    behind after an upgrade, and charging somebody the lower tier's limits
    for having paid more would be exactly backwards."""
    configured = configured_tier_roles()
    tiers = [tier for tier, role_id in configured.items() if role_id in role_ids]
    if tiers:
        return max(tiers)
    return -1 if set(role_ids).intersection(map(int, COMMUNITY_SPONSOR_ROLE_IDS)) else 0

async def live_discord_tier(discord_id):
    """The tier a Discord account holds RIGHT NOW, read off its roles.

    THE function: every other tier question in the codebase is answered from
    the state this one writes, so a second source of truth (a Patreon API, a
    manual grant) would be added here and nowhere else.

    Returns -1, 0, 1, 2 or 3 -- and None when the answer is unknown rather than
    zero, which is a distinction the caller must respect: an unreachable
    server means "do not touch anything", never "everybody's subscription
    ended"."""
    guild = _patreon_guild()
    if guild is None:
        return None
    try:
        uid = int(discord_id)
    except (TypeError, ValueError):
        return 0
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except Exception as e:
            if "Unknown Member" in str(e) or "404" in str(e):
                return 0
            logger.info("sponsor tier lookup failed for %s: %s", uid, e)
            return None
    role_ids = {r.id for r in getattr(member, "roles", ())}
    db.observe_community_role(discord_id,
                              bool(role_ids.intersection(map(int, COMMUNITY_SPONSOR_ROLE_IDS))))
    return _tier_from_roles(role_ids)

async def live_tier_table():
    """`{discord_id: tier}` for everybody currently holding one of the roles,
    or None when the server cannot be read.

    Built from the roles' own member lists rather than by asking about one
    account at a time: the reconciliation needs every holder anyway, and one
    pass over two cached role objects costs no API call at all."""
    guild = _patreon_guild()
    if guild is None:
        return None
    tiers = {}
    for tier, role_id in sorted(configured_tier_roles().items()):
        role = guild.get_role(role_id)
        if role is None:
            _complain_once(f"role-missing-{tier}",
                           "the tier %s role %s is not on the Patreon server", tier, role_id)
            return None
        for member in role.members:
            uid = str(member.id)
            if tier > tiers.get(uid, 0):
                tiers[uid] = tier
    community_members = set()
    for role_id in COMMUNITY_SPONSOR_ROLE_IDS:
        role = guild.get_role(int(role_id))
        if role is None:
            return None
        community_members.update(str(member.id) for member in role.members)
    for owner in {str(row["discord_id"]) for row in db.get_known_sponsors()} | set(tiers) | community_members:
        db.observe_community_role(owner, owner in community_members)
        if owner in community_members and owner not in tiers:
            tiers[owner] = -1
    return tiers

def _rights_tier(row):
    """The tier whose rights are in force for one stored state row.

    During a grace period that is the tier held BEFORE the drop: the whole
    point of the period is that nothing is taken away while it runs. The
    comparison is against the timestamp in the row, never against a count of
    loop iterations, so a bot that was switched off for six weeks works out
    the truth on its first pass rather than starting the clock again."""
    if row is None:
        return 0
    tier = int(row["tier"] or 0)
    grace_since = row["grace_since"]
    grace_from = row["grace_from_tier"]
    former = int(grace_from or 0)
    window = COMMUNITY_GRACE_SECONDS if former == -1 else SPONSOR_GRACE_SECONDS
    if (grace_since and _rank(former) > _rank(tier)
            and time.time() - int(grace_since) < window):
        return int(grace_from)
    if tier == -1:
        seen = row["community_role_since"]
        return -1 if seen is not None and time.time() - int(seen) >= COMMUNITY_WAIT_SECONDS else 0
    return tier

def sponsor_tier(platform, user_id):
    """The tier whose RIGHTS this caller holds, 0 when none.

    Resolves through the account link first, so a linked Telegram user is
    asked about under their Discord identity and the pair shares one set of
    rights rather than holding one each."""
    discord_id = db.canonical_sponsor_id(platform, user_id)
    if not discord_id:
        return 0
    return _rights_tier(db.get_sponsor_state(discord_id))

def sponsor_quota_tier(platform, user_id):
    """Quota tier; a fully lapsed payment retains its tier for 30 days."""
    discord_id = db.canonical_sponsor_id(platform, user_id)
    if not discord_id:
        return 0
    row = db.get_sponsor_state(discord_id)
    if row is None:
        return 0
    return _rights_tier(row)

def is_frozen(discord_id):
    """Whether a lapsed account has finished its working grace period."""
    row = db.get_sponsor_state(discord_id)
    return bool(row) and int(row["tier"] or 0) == 0 and _rights_tier(row) == 0

def grace_days_left(discord_id):
    """Days remaining of a grace period, or None when none is running.

    Rounded UP, which is what somebody reading "you have N days" means by it:
    a period that has just begun has all thirty of them and not twenty-nine,
    and the last day is reported as one day rather than as none."""
    row = db.get_sponsor_state(discord_id)
    if not row or not row["grace_since"]:
        return None
    window = (COMMUNITY_GRACE_SECONDS if int(row["grace_from_tier"] or 0) == -1
              else SPONSOR_GRACE_SECONDS)
    left = window - (time.time() - int(row["grace_since"]))
    return max(0, -int(-left // 86400))

def apply_tier(discord_id, live_tier):
    """Record what the roles say and return the notice that owes the sponsor
    an explanation: 'frozen', 'downgraded', 'restored' or None.

    The transitions, all of which leave every row of configuration alone:

      * a tier appearing where there was none -- recorded, no notice; nobody
        needs to be told that the thing they just paid for works.
      * a rise back to what they held -- the grace period is called off and
        everything that was frozen comes back by itself.
      * a rise that is still below what they held (2 -> 0 -> 1) -- recorded,
        but the grace period keeps running on its ORIGINAL clock, because
        their rights have not finished falling.
      * a drop -- the grace period starts, or starts again from today for a
        second drop. `grace_from_tier` is the tier that was in FORCE, not the
        one last paid for, so somebody who drops 2 -> 1 -> 0 in one month
        keeps their tier-2 slots to the end rather than losing them a step at
        a time."""
    now = int(time.time())
    row = db.get_sponsor_state(discord_id)
    live_tier = int(live_tier or 0)

    if row is None:
        if live_tier == 0:
            return None
        db.save_sponsor_state(discord_id, tier=live_tier, since=now)
        return None

    previous = int(row["tier"] or 0)
    in_force = _rights_tier(row)
    if live_tier == previous:
        return None

    if _rank(live_tier) > _rank(previous):
        if _rank(live_tier) >= _rank(in_force):
            db.save_sponsor_state(discord_id, tier=live_tier, since=now,
                                  slot_changed_at=row["slot_changed_at"])
            db.set_sponsor_retention_due(discord_id, None)
            return "restored" if row["grace_since"] else None
        db.save_sponsor_state(
            discord_id, tier=live_tier, since=now,
            grace_from_tier=row["grace_from_tier"], grace_since=row["grace_since"],
            notified_grace=row["notified_grace"], notified_expired=row["notified_expired"],
            slot_changed_at=row["slot_changed_at"])
        if live_tier != 0:
            db.set_sponsor_retention_due(discord_id, None)
        return None

    db.save_sponsor_state(discord_id, tier=live_tier, since=now,
                          grace_from_tier=in_force, grace_since=now,
                          slot_changed_at=row["slot_changed_at"])
    if live_tier == 0:
        grace = COMMUNITY_GRACE_SECONDS if in_force == -1 else SPONSOR_GRACE_SECONDS
        db.set_sponsor_retention_due(discord_id, now + grace + SPONSOR_RETENTION_SECONDS)
    else:
        db.set_sponsor_retention_due(discord_id, None)
    return "frozen" if live_tier == 0 else "downgraded"

def expire_grace(row):
    """Carry out what a finished grace period was postponing, or return None
    when it is not finished.

    Rights fall to the tier actually being paid for, and the community slots
    beyond what that tier allows are given back -- the ones claimed LAST,
    keeping the ones claimed first. That rule is named in the notice sent
    when the period began, so that the choice the bot makes at the end is one
    the sponsor was told about at the start and had a month to make
    themselves.

    Bridges, chats, sources and settings are untouched, and so is every row
    in `sponsor_bridges`: the numbers stay the sponsor's for good, which is
    what lets them pick the same bridges up again later."""
    grace_since = row["grace_since"]
    window = (COMMUNITY_GRACE_SECONDS if int(row["grace_from_tier"] or 0) == -1
              else SPONSOR_GRACE_SECONDS)
    if not grace_since or time.time() - int(grace_since) < window:
        return None
    discord_id = str(row["discord_id"])
    tier = int(row["tier"] or 0)
    allowed = sponsor_limits(tier)["communities"]
    claimed = db.get_sponsor_communities(discord_id)
    released = []
    for community in claimed[allowed:] if tier != 0 else ():
        if db.release_community(community["platform"], community["server_id"], discord_id):
            released.append(community)
    db.save_sponsor_state(discord_id, tier=tier, since=row["since"],
                          slot_changed_at=row["slot_changed_at"])
    return released

async def reconcile_pass():
    """Bring the stored sponsor states in line with the roles, once an hour.

    The events (`on_member_update`) are faster and this is slower, and only
    this one is authoritative: events are lost across restarts, dropped
    gateway connections and every minute the bot spends offline, so a bot
    that trusted them alone would drift and never find out. Returns the
    notices to deliver, as ``(discord_id, kind)`` pairs.

    A server that cannot be read produces no notices and changes nothing at
    all -- an outage on Discord's side is not evidence that anybody stopped
    paying."""
    live = await live_tier_table()
    if live is None:
        return None

    notices = []
    known = {str(r["discord_id"]) for r in db.get_known_sponsors()}
    for discord_id in sorted(known | set(live)):
        notice = apply_tier(discord_id, live.get(discord_id, 0))
        if notice:
            notices.append((discord_id, notice))

    for row in db.get_sponsors_in_grace():
        if expire_grace(row) is not None:
            notices.append((str(row["discord_id"]), "expired"))
        elif not int(row["notified_grace"] or 0):
            notices.append((str(row["discord_id"]),
                            "frozen" if int(row["tier"] or 0) == 0 else "downgraded"))
    return notices

async def cleanup_due_communities():
    """Leave and erase only fully lapsed, still-owned communities.

    The role is fetched again before leaving each community. An inaccessible
    Patreon server or an uncertain member lookup stops cleanup. A replacement
    sponsor or Bot Admin can claim the community after grace and before this
    deadline; that changes its owner and cancels this owner's cleanup.
    """
    import discord
    from discord_bot import bot as dc

    for claim in db.due_sponsor_communities():
        owner = str(claim["discord_id"])
        live = await live_discord_tier(owner)
        if live is None:
            return
        apply_tier(owner, live)
        if sponsor_tier("discord", owner) != 0 or live != 0:
            continue
        platform = claim["platform"]
        server = str(claim["server_id"])
        current = db.get_community_claim(platform, server)
        if current is None or str(current["discord_id"]) != owner:
            continue
        try:
            if platform == "discord":
                if server == str(PATREON_GUILD_ID):
                    continue
                guild = dc.get_guild(int(server))
                if guild is None:
                    try:
                        guild = await dc.fetch_guild(int(server))
                    except discord.NotFound:
                        guild = None
                    except discord.HTTPException as error:
                        logger.warning("expired sponsor guild lookup failed (%s): %s",
                                       server, error)
                        continue
                if guild is not None:
                    await guild.leave()
            elif platform == "telegram":
                from telegram_bot import bot as tg_bot
                if not await tg_bot.leave_chat(int(server)):
                    continue
            else:
                continue
        except Exception as error:
            logger.warning("sponsor community leave failed (%s:%s): %s",
                           platform, server, error)
            continue
        if db.purge_sponsor_community(platform, server, owner):
            logger.info("expired sponsor community removed (%s:%s)", platform, server)

def sponsor_lang(discord_id):
    """The language to write to a sponsor in: that of the first community
    they claimed, and English when they have claimed none. There is no
    per-person language in this bot, and the community somebody put their
    subscription behind is the closest thing to one."""
    for community in db.get_sponsor_communities(discord_id):
        lang = get_chat_lang(str(community["server_id"]))
        if lang:
            return lang
    return DEFAULT_LANG

async def _notify_in_channel(discord_id, lang):
    """Fall back to SPONSOR_NOTICE_CHANNEL when a sponsor's DMs are shut.

    What is posted there is a mention and nothing else: "we could not reach
    you, run /sponsor". Not the tier, not the reason, not the deadline, not
    whether the subscription lapsed or merely dropped a step — that channel is
    read by other people, and somebody's payments are not theirs to read. The
    facts stay in the DM the sponsor can reopen, and in `/sponsor`, which
    answers only the person who runs it.

    Returns whether the nudge was posted; a channel that is unset, missing or
    unwritable simply means no fallback, not an error."""
    from discord_bot import bot as dc

    try:
        channel_id = int(SPONSOR_NOTICE_CHANNEL)
    except (TypeError, ValueError):
        return False
    try:
        channel = dc.get_channel(channel_id) or await dc.fetch_channel(channel_id)
        await channel.send(localized("sponsor_notice_fallback", lang,
                                     mention=f"<@{discord_id}>"))
    except Exception as e:
        logger.info("could not nudge sponsor %s in channel %s: %s",
                    discord_id, SPONSOR_NOTICE_CHANNEL, e)
        return False
    return True

async def deliver_notice(discord_id, kind):
    """Tell a sponsor what has happened to their subscription, once.

    The 'once' is a row in the database rather than a memory of this
    process's, which is what stops a bot returning from a week offline from
    delivering a week of them.

    Two ways of reaching them, and the difference between them is on purpose.
    The DM carries everything: which tier, why, how long, what happens at the
    end. If it bounces — closed DMs, a blocked bot, no shared server — the
    sponsor is instead mentioned in SPONSOR_NOTICE_CHANNEL and told to run
    `/sponsor`, with none of the substance repeated there, because that
    channel has an audience and a person's payment status is not something to
    announce to it.

    Only a message that actually went out is marked. When neither way worked,
    `reconcile_pass` offers the notice again every hour for as long as the
    grace period runs, so a bot that died between sending and marking, or a
    Discord that was briefly unwell, costs nothing."""
    from discord_bot import bot as dc

    field = {"frozen": "notified_grace", "downgraded": "notified_grace",
             "expired": "notified_expired"}.get(kind)
    row = db.get_sponsor_state(discord_id)
    if row is None:
        return False
    if field and int(row[field] or 0):
        return False

    lang = sponsor_lang(discord_id)
    limits = sponsor_limits(int(row["tier"] or 0))
    days = grace_days_left(discord_id)
    notice_key = ("sponsor_notice_expired_retention" if kind == "expired"
                  else "sponsor_notice_community_lost" if kind == "frozen"
                  and int(row["grace_from_tier"] or 0) == -1
                  else f"sponsor_notice_{kind}")
    text = localized(
        notice_key, lang,
        days=days if days is not None else SPONSOR_GRACE_SECONDS // 86400,
        tier=(localized("sponsor_tier_community", lang)
              if int(row["tier"] or 0) == -1 else int(row["tier"] or 0)),
        communities=limits["communities"], wikis=limits["wikis"],
        socials=limits["socials"],
    )
    delivered = False
    try:
        user = dc.get_user(int(discord_id)) or await dc.fetch_user(int(discord_id))
        await user.send(text)
        delivered = True
    except Exception as e:
        logger.info("could not tell sponsor %s about '%s' in DM: %s",
                    discord_id, kind, e)
        delivered = await _notify_in_channel(discord_id, lang)

    if delivered and field:
        db.mark_sponsor_notified(discord_id, field)
    return delivered

def _chat_bridge_id(chat_id):
    """The bridge a chat belongs to, or None when it is in none."""
    row = db.cur.execute(
        "SELECT bridge_id FROM chats WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    return row["bridge_id"] if row and row["bridge_id"] is not None else None

def sponsor_bridge_owner(bridge_id):
    """The Discord account that owns a sponsor bridge number, or None when
    the number is not a sponsor one or was never handed out."""
    if bridge_kind(bridge_id) != "sponsor":
        return None
    row = db.get_sponsor_bridge(bridge_id)
    return str(row["discord_id"]) if row else None

def sponsor_relay_owner(bridge_id):
    """Only ordinary sponsor bridges spend delivery units.

    Appeal conversations must remain usable after regular bridge traffic
    reaches its quota. Their database bookkeeping is kept for seven days.
    """
    return sponsor_bridge_owner(bridge_id)

def _owned_usage(discord_id):
    """Active bridges, attached chats and sources across one subscription."""
    return db.get_sponsor_usage(discord_id)

def sponsor_relay_allowed(bridge_id, origin_platform, origin_chat_id,
                          targets, gallery_urls=None):
    """Reserve the cost of an immediate sponsor edit fan-out.

    New messages use sponsor_delivery's persistent per-copy queue instead.

    One unit is one outgoing message or edit. Extra Telegram gallery messages
    cost extra units. Both the burst and UTC-day budgets belong to the Discord
    subscription, so adding senders, bridges or a linked Telegram account
    cannot multiply them. A lapsed subscription spends no units and relays
    nothing while its bridge configuration remains intact.
    """
    owner = sponsor_relay_owner(bridge_id)
    if owner is None or is_admin("discord", owner):
        return True
    row = db.get_sponsor_state(owner)
    tier = sponsor_quota_tier("discord", owner)
    if tier == 0:
        return False
    units = 0
    for chat in targets:
        if chat["platform"] == origin_platform and chat["chat_id"] == origin_chat_id:
            continue
        units += 1
        if chat["platform"] == "telegram" and gallery_urls:
            units += len(gallery_urls)
    if units == 0:
        return True
    limits = sponsor_limits(tier)
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    allowed = db.spend_sponsor_relay(
        owner, day, now, units, limits["relay_minute"], limits["relay_day"])
    if not allowed and rate_limit_ok(("sponsor-relay-limit", owner), 1, 60):
        logger.info("sponsor relay budget reached for %s", owner)
    return allowed

def claim_daily_limit_notice(bridge_id, origin_platform, origin_chat_id,
                             targets, gallery_urls=None):
    """Return whether this denied fan-out exhausted the day budget and is
    the first such notice for this bridge today. Minute throttling stays
    quiet, because it recovers automatically and must not create more traffic."""
    owner = sponsor_relay_owner(bridge_id)
    if owner is None or is_admin("discord", owner):
        return False
    tier = sponsor_quota_tier("discord", owner)
    if tier == 0:
        return False
    units = sum(
        1 + (len(gallery_urls)
             if chat["platform"] == "telegram" and gallery_urls else 0)
        for chat in targets
        if chat["platform"] != origin_platform or chat["chat_id"] != origin_chat_id
    )
    day = time.strftime("%Y-%m-%d", time.gmtime())
    usage = db.sponsor_relay_usage(owner, day)
    spent = int(usage["day_units"]) if usage else 0
    if spent + units <= sponsor_limits(tier)["relay_day"]:
        return False
    return db.claim_sponsor_relay_notice(bridge_id, day)

def feed_quota(chat_id, kind):
    """May another source of `kind` be attached here? Returns
    ``(allowed, reason_key, used, limit)``.

    Limits exist on sponsor bridges and NOWHERE else: an ordinary bridge is
    the operator's own business and has never been counted, which this must
    not change. `wikidisc` is not counted at all -- a wiki's discussions are
    attached automatically alongside the wiki itself and are the same
    subscription, so charging two of three for one wiki would be a lie about
    what was asked for.

    The count is over the whole BRIDGE, not the chat: feeds hang off a chat
    but deliver into every chat of its bridge, so a per-chat limit would be
    lifted by attaching a second chat."""
    bridge_id = _chat_bridge_id(chat_id)
    if bridge_id is None or bridge_kind(bridge_id) != "sponsor":
        return True, None, None, None
    owner = sponsor_bridge_owner(bridge_id)
    if owner is None:
        return True, None, None, None

    row = db.get_sponsor_state(owner)
    tier = sponsor_quota_tier("discord", owner)
    if tier == 0:
        return False, "sponsor_feed_frozen", None, None

    limits = sponsor_limits(tier)
    usage = _owned_usage(owner)
    if kind in WIKI_FEED_KINDS:
        used = usage["wikis"]
        return used < limits["wikis"], "sponsor_quota_wikis", used, limits["wikis"]
    if kind in SOCIAL_FEED_KINDS:
        used = usage["socials"]
        return used < limits["socials"], "sponsor_quota_socials", used, limits["socials"]
    return True, None, None, None

def community_slot_status(platform, user_id, server_id):
    """What would happen if this caller claimed this community, without
    claiming it. Returns one of 'ok', 'not_a_sponsor', 'yours', 'taken',
    'no_slots', 'too_soon', paired with whatever the wording needs."""
    discord_id = db.canonical_sponsor_id(platform, user_id)
    admin = is_admin(platform, user_id)
    if not discord_id and not admin:
        return "not_a_sponsor", {}
    discord_id = discord_id or str(user_id)
    tier = _rights_tier(db.get_sponsor_state(discord_id))
    if tier == 0 and not admin:
        return "not_a_sponsor", {}

    claim = db.get_community_claim(platform, server_id)
    if claim and str(claim["discord_id"]) == discord_id:
        return "yours", {}
    takeover_from = None
    if claim:
        old_owner = str(claim["discord_id"])
        if (is_admin("discord", old_owner) or is_admin("telegram", old_owner)
                or _rights_tier(db.get_sponsor_state(old_owner)) != 0):
            return "taken", {}
        takeover_from = old_owner

    allowed = sponsor_limits(tier)["communities"] if not admin else 999999
    used = len(db.get_sponsor_communities(discord_id))
    if used >= allowed:
        return "no_slots", {"used": used, "limit": allowed}

    row = db.get_sponsor_state(discord_id)
    changed = row["slot_changed_at"] if row else None
    if changed:
        waited = time.time() - int(changed)
        if waited < SPONSOR_SLOT_CHANGE_SECONDS:
            days = max(1, int((SPONSOR_SLOT_CHANGE_SECONDS - waited) // 86400) + 1)
            return "too_soon", {"days": days}
    return "ok", {"used": used, "limit": allowed, "discord_id": discord_id,
                  "takeover_from": takeover_from}

def owns_community(platform, user_id, platform_of_community, server_id):
    """Whether this caller's sponsorship covers that community -- the test
    behind every sponsor-only action, and the reason a sponsor cannot open
    bridges in a community they never claimed."""
    discord_id = db.canonical_sponsor_id(platform, user_id)
    if not discord_id or _rights_tier(db.get_sponsor_state(discord_id)) == 0:
        return False
    claim = db.get_community_claim(platform_of_community, server_id)
    return bool(claim) and str(claim["discord_id"]) == discord_id

def atb_decision(platform, user_id, chat_id, raw, is_bot_admin):
    """Who may attach this chat to which bridge. Returns
    ``(action, value, reason_key)``:

      'new'          open an ordinary bridge on the lowest free number
      'sponsor_new'  open a sponsor bridge; `value` is its owner's Discord id
      'attach'       attach to the existing number in `value`
      'deny'         refuse; `reason_key` is what to say

    Two rules meet here. The first is new: the appeal and inbox regions are
    refused to EVERYBODY, a Bot Admin included, because `/atb 100000` used to
    be accepted and would put a chat inside somebody's appeal or somebody's
    private conversation with a support team. It was untidy while only Bot
    Admins could type it and became a hole the moment sponsors could.

    The second is the sponsor bargain: a sponsor may open bridges, but only
    in the communities they have claimed and only in their own region of the
    number space, and a sponsor bridge accepts chats from its owner alone --
    not from its Bridge Admins, not from the administrators of the servers in
    it. Somebody who pays for a space they control gets a space they control.
    A Bot Admin stands above all of it, because a bot nobody can administer
    is worse than any of the things this protects against."""
    if raw == "new":
        if is_bot_admin:
            return "new", None, None
        server_id = db.chat_server_id(platform, chat_id)
        if server_id and owns_community(platform, user_id, platform, server_id):
            owner = db.canonical_sponsor_id(platform, user_id)
            limits = sponsor_limits(sponsor_quota_tier(platform, user_id))
            usage = _owned_usage(owner)
            if usage["bridges"] >= limits["bridges"]:
                return "deny", None, "sponsor_quota_bridges"
            if usage["chats"] >= limits["chats"]:
                return "deny", None, "sponsor_quota_chats"
            return "sponsor_new", owner, None
        if sponsor_tier(platform, user_id) != 0:
            return "deny", None, "atb_sponsor_claim_first"
        return "deny", None, "no_permission"

    kind = bridge_kind(raw)
    if kind is None:
        return "deny", None, "atb_invalid_id"
    if kind in ("appeal", "inbox"):
        return "deny", None, "atb_reserved_range"

    if kind == "sponsor":
        owner = sponsor_bridge_owner(raw)
        if owner is None:
            return "deny", None, "atb_reserved_range"
        if is_bot_admin:
            if len(db.get_bridge_chats(raw)) >= SPONSOR_CHATS_PER_BRIDGE:
                return "deny", None, "sponsor_quota_bridge_chats"
            return "attach", raw, None
        limits = sponsor_limits(sponsor_quota_tier("discord", owner))
        usage = _owned_usage(owner)
        if not db.get_bridge_chats(raw) and usage["bridges"] >= limits["bridges"]:
            return "deny", None, "sponsor_quota_bridges"
        if usage["chats"] >= limits["chats"]:
            return "deny", None, "sponsor_quota_chats"
        if len(db.get_bridge_chats(raw)) >= SPONSOR_CHATS_PER_BRIDGE:
            return "deny", None, "sponsor_quota_bridge_chats"
        if db.canonical_sponsor_id(platform, user_id) != owner:
            return "deny", None, "atb_sponsor_not_owner"
        server_id = db.chat_server_id(platform, chat_id)
        if not (server_id and owns_community(platform, user_id, platform, server_id)):
            return "deny", None, "atb_sponsor_claim_first"
        return "attach", raw, None

    if is_bot_admin:
        return "attach", raw, None
    return "deny", None, "no_permission"

def sponsor_overview(platform, user_id):
    """Everything `/sponsor` prints, gathered once: the tier in force, the
    tier being paid for, the slots and what fills them, every owned bridge
    with the sources counted against its limits, and the grace period if one
    is running. Returns None when the caller is nobody's sponsor."""
    discord_id = db.canonical_sponsor_id(platform, user_id)
    if not discord_id:
        return None
    row = db.get_sponsor_state(discord_id)
    rights = _rights_tier(row)
    live = int(row["tier"] or 0) if row else 0
    if row is None:
        return None

    limits = sponsor_limits(rights)
    quota_limits = sponsor_limits(sponsor_quota_tier("discord", discord_id))
    wait_days = None
    if live == -1 and rights == 0 and row["community_role_since"] is not None:
        remaining = COMMUNITY_WAIT_SECONDS - (time.time() - int(row["community_role_since"]))
        wait_days = max(0, int((remaining + 86399) // 86400))
    usage = _owned_usage(discord_id)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    relay_row = db.sponsor_relay_usage(discord_id, day)
    bridges = []
    for owned in db.get_sponsor_bridges(discord_id):
        bridge_id = int(owned["bridge_id"])
        feeds = db.get_bridge_feeds(bridge_id)
        bridges.append({
            "bridge_id": bridge_id,
            "alive": bool(db.get_bridge_chats(bridge_id)),
            "wikis": sum(1 for f in feeds if f["kind"] in WIKI_FEED_KINDS),
            "socials": sum(1 for f in feeds if f["kind"] in SOCIAL_FEED_KINDS),
        })
    return {
        "discord_id": discord_id,
        "tier": rights,
        "community_wait_days": wait_days,
        "paid_tier": live,
        "frozen": live == 0 and rights == 0,
        "grace_days": grace_days_left(discord_id),
        "limits": limits,
        "quota_limits": quota_limits,
        "usage": usage,
        "relay_used": int(relay_row["day_units"]) if relay_row else 0,
        "communities": db.get_sponsor_communities(discord_id),
        "bridges": bridges,
        "link": db.get_link_by_discord(discord_id),
    }

def limit_text(owner, lang):
    """Current account-wide slots, UTC delivery usage and persistent backlog."""
    caps = sponsor_limits(sponsor_quota_tier("discord", owner))
    used = db.get_sponsor_usage(owner)
    row = db.sponsor_relay_usage(owner, time.strftime("%Y-%m-%d", time.gmtime()))
    minute = int(row["minute_units"]) if row and time.time() - row["minute_started"] < 60 else 0
    return localized("limit_status", lang, communities=len(db.get_sponsor_communities(owner)),
        community_limit=caps["communities"], bridges=used["bridges"], bridge_limit=caps["bridges"],
        chats=used["chats"], chat_limit=caps["chats"], wikis=used["wikis"], wiki_limit=caps["wikis"],
        socials=used["socials"], social_limit=caps["socials"],
        day=int(row["day_units"]) if row else 0, day_limit=caps["relay_day"],
        minute=minute, minute_limit=caps["relay_minute"], queued=db.queued_sponsor_copies(owner))
