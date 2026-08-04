"""What a wiki change means, in words a reader can use.

The platform-neutral half of the wiki relay: it classifies a change into a
group, decides whether a subscription wants to hear about it, and renders it
as one sentence in the target chat's language. Discord's embeds are built on
top of the same classification (discord_bot/wiki.py); Telegram gets the
sentence and nothing else, which is the whole point of keeping the wording
here rather than inside a formatter that only one platform can use.

The grouping is coarse on purpose. A wiki can emit a hundred distinct log
actions once extensions are counted, but nobody filters at that granularity
— an admin wants "no patrol noise" or "deletions only", so the switches are
per group and an action nobody has taught the bot about still arrives,
through the generic wording, instead of vanishing.

Not this module's zone: reading the wiki (sources/wiki.py), Discord embeds
and delivery (discord_bot/wiki.py), storing the settings (db/feeds.py).
"""

from utils import DEFAULT_LANG, localized

EVENT_GROUPS = (
    "edit", "new", "upload", "delete", "move", "protect", "block", "rights",
    "newusers", "import", "merge", "tag", "contentmodel", "pagelang",
    "patrol", "filters", "wikifarm", "global", "interwiki", "translate",
    "approval", "structured", "profile", "discussion", "other",
)

DEFAULT_DISABLED_GROUPS = ("patrol",)

_LOGTYPE_GROUPS = {
    "upload": "upload",
    "delete": "delete",
    "suppress": "delete",
    "move": "move",
    "protect": "protect",
    "block": "block",
    "rights": "rights",
    "newusers": "newusers",
    "import": "import",
    "merge": "merge",
    "tag": "tag",
    "managetags": "tag",
    "contentmodel": "contentmodel",
    "pagelang": "pagelang",
    "patrol": "patrol",
    "renameuser": "rights",

    "abusefilter": "filters",
    "abusefilter-protected-vars": "filters",
    "abusefilterblockeddomainhit": "filters",
    "abusefilterprivatedetails": "filters",
    "spamblacklist": "filters",
    "titleblacklist": "filters",

    "managewiki": "wikifarm",
    "farmer": "wikifarm",
    "farmersuppression": "wikifarm",
    "createwiki": "wikifarm",
    "requestcustomdomain": "wikifarm",
    "datadump": "wikifarm",
    "importdump": "wikifarm",
    "importdumpprivate": "wikifarm",

    "gblblock": "global",
    "gblrights": "global",
    "gblrename": "global",
    "globalauth": "global",
    "vanishuser": "global",
    "removepii": "global",
    "usermerge": "global",
    "phalanx": "global",
    "phalanxemail": "global",
    "renametool": "global",
    "editaccnt": "global",

    "interwiki": "interwiki",

    "pagetranslation": "translate",
    "translationreview": "translate",
    "messagebundle": "translate",

    "approval": "approval",
    "review": "approval",
    "stable": "approval",
    "pagetriage-curation": "approval",
    "pagetriage-copyvio": "approval",

    "cargo": "structured",
    "sprite": "structured",
    "templateclassification": "structured",

    "curseprofile": "profile",
    "comments": "profile",
    "socialprofile": "profile",
}

_GROUP_FALLBACK_KEYS = {
    "filters": "wiki_ev_filter_other",
    "wikifarm": "wiki_ev_farm_other",
    "global": "wiki_ev_global_other",
    "interwiki": "wiki_ev_interwiki",
    "translate": "wiki_ev_translate_other",
    "approval": "wiki_ev_approve_other",
    "structured": "wiki_ev_structured",
    "profile": "wiki_ev_profile",
}

_GROUP_COLOURS = {
    "edit": 0x3366CC,
    "new": 0x2ECC71,
    "upload": 0x1ABC9C,
    "delete": 0xE74C3C,
    "move": 0xE67E22,
    "protect": 0x9B59B6,
    "block": 0xC0392B,
    "rights": 0xF1C40F,
    "newusers": 0x27AE60,
    "import": 0x5D6D7E,
    "merge": 0x5D6D7E,
    "tag": 0x7F8C8D,
    "contentmodel": 0x7F8C8D,
    "pagelang": 0x7F8C8D,
    "patrol": 0xAAB7B8,
    "filters": 0xD35400,
    "wikifarm": 0x2980B9,
    "global": 0x8E44AD,
    "interwiki": 0x16A085,
    "translate": 0x2E86C1,
    "approval": 0x229954,
    "structured": 0x839192,
    "profile": 0xAF7AC5,
    "discussion": 0x00B5AD,
    "other": 0x95A5A6,
}

_ACTION_KEYS = {
    ("upload", "upload"): "wiki_ev_upload",
    ("upload", "overwrite"): "wiki_ev_upload_overwrite",
    ("upload", "revert"): "wiki_ev_upload_revert",
    ("delete", "delete"): "wiki_ev_delete",
    ("delete", "delete_redir"): "wiki_ev_delete",
    ("delete", "restore"): "wiki_ev_restore",
    ("delete", "revision"): "wiki_ev_delete_revision",
    ("delete", "event"): "wiki_ev_delete_revision",
    ("move", "move"): "wiki_ev_move",
    ("move", "move_redir"): "wiki_ev_move_redir",
    ("protect", "protect"): "wiki_ev_protect",
    ("protect", "modify"): "wiki_ev_protect_modify",
    ("protect", "move_prot"): "wiki_ev_protect_modify",
    ("protect", "unprotect"): "wiki_ev_unprotect",
    ("block", "block"): "wiki_ev_block",
    ("block", "reblock"): "wiki_ev_reblock",
    ("block", "unblock"): "wiki_ev_unblock",
    ("rights", "rights"): "wiki_ev_rights",
    ("rights", "autopromote"): "wiki_ev_rights_auto",
    ("renameuser", "renameuser"): "wiki_ev_renameuser",
    ("newusers", "create"): "wiki_ev_newuser",
    ("newusers", "autocreate"): "wiki_ev_newuser_auto",
    ("newusers", "create2"): "wiki_ev_newuser_created_by",
    ("newusers", "byemail"): "wiki_ev_newuser_created_by",
    ("import", "upload"): "wiki_ev_import",
    ("import", "interwiki"): "wiki_ev_import",
    ("merge", "merge"): "wiki_ev_merge",
    ("tag", "update"): "wiki_ev_tag",
    ("managetags", "create"): "wiki_ev_managetags",
    ("managetags", "delete"): "wiki_ev_managetags",
    ("managetags", "activate"): "wiki_ev_managetags",
    ("managetags", "deactivate"): "wiki_ev_managetags",
    ("contentmodel", "change"): "wiki_ev_contentmodel",
    ("contentmodel", "new"): "wiki_ev_contentmodel_new",
    ("pagelang", "pagelang"): "wiki_ev_pagelang",
    ("patrol", "patrol"): "wiki_ev_patrol",
    ("patrol", "autopatrol"): "wiki_ev_patrol_auto",

    ("abusefilter", "modify"): "wiki_ev_filter_modify",
    ("abusefilter", "create"): "wiki_ev_filter_modify",
    ("abusefilter", "hit"): "wiki_ev_filter_hit",

    ("farmer", "requestwiki"): "wiki_ev_farm_request",
    ("farmer", "createwiki"): "wiki_ev_farm_create",
    ("createwiki", "createwiki"): "wiki_ev_farm_create",
    ("managewiki", "settings"): "wiki_ev_farm_settings",
    ("datadump", "generate"): "wiki_ev_dump",
    ("datadump", "delete"): "wiki_ev_dump",
    ("importdump", "started"): "wiki_ev_import_request",
    ("importdump", "request"): "wiki_ev_import_request",

    ("gblblock", "gblock"): "wiki_ev_global_block",
    ("gblblock", "gblock2"): "wiki_ev_global_block",
    ("gblblock", "modify"): "wiki_ev_global_block",
    ("gblblock", "gunblock"): "wiki_ev_global_unblock",
    ("gblrename", "rename"): "wiki_ev_global_rename",
    ("renametool", "rename"): "wiki_ev_global_rename",

    ("interwiki", "iw_add"): "wiki_ev_interwiki",
    ("interwiki", "iw_edit"): "wiki_ev_interwiki",
    ("interwiki", "iw_delete"): "wiki_ev_interwiki",

    ("pagetranslation", "mark"): "wiki_ev_translate_mark",
    ("pagetranslation", "unmark"): "wiki_ev_translate_unmark",
    ("translationreview", "message"): "wiki_ev_translate_review",
    ("translationreview", "group"): "wiki_ev_translate_review",

    ("approval", "approve"): "wiki_ev_approve",
    ("approval", "approvefile"): "wiki_ev_approve",
    ("approval", "unapprove"): "wiki_ev_unapprove",
    ("approval", "unapprovefile"): "wiki_ev_unapprove",
    ("review", "approve"): "wiki_ev_approve",
    ("review", "approve2"): "wiki_ev_approve",
    ("review", "unapprove"): "wiki_ev_unapprove",
    ("review", "unapprove2"): "wiki_ev_unapprove",
}

_HOSTINGS = (
    (("fandom.com", "wikia.org", "gamepedia.com"), "fandom", "Fandom"),
    (("miraheze.org",), "miraheze", "Miraheze"),
    (("wiki.gg",), "wikigg", "Wiki.gg"),
)

def wiki_hosting(source):
    """Which wiki farm an address belongs to, or None for a wiki that is
    self-hosted or on a farm nobody has taught the bot about.

    Used for two visible things: the name a relayed change is attributed to
    (a reader recognizes 'Fandom' where '[Wiki]' says nothing), and the
    avatar its Discord webhook wears. Adding a farm is one row here."""
    host = str(source or "").split("/", 1)[0].lower()
    for suffixes, key, _label in _HOSTINGS:
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return key
    return None

def hosting_label(source):
    """What to print where a relayed message names the platform it came from.

    The farm's own name when the wiki lives on one, and the generic 'Wiki'
    only when it does not — a self-hosted wiki has no brand to borrow."""
    key = wiki_hosting(source)
    for _suffixes, hosting_key, label in _HOSTINGS:
        if hosting_key == key:
            return label
    return "Wiki"

def event_group(event):
    """Which switch decides whether this change is relayed.

    Ordinary edits and page creations are their own groups; a Fandom
    Discussions post is its own; everything else is a log entry and follows
    its log type. A log type the bot has never heard of — every wiki farm
    invents a few — lands in 'other' rather than being dropped, so an admin
    who leaves the filters alone still sees that something happened.

    The extension groups exist because a switch per extension is what an
    admin actually wants ('no wiki-farm noise', 'no translation admin'), and
    which log types belong to which extension was taken from the log-type
    enum real wikis publish, not guessed:

      * `filters` — Abuse Filter (everywhere) plus the spam and title
        blacklists.
      * `wikifarm` — ManageWiki, CreateWiki, DataDump, ImportDump and the
        wiki-request queue. Miraheze and other farms running that stack.
      * `global` — cross-wiki account actions: GlobalBlocking, CentralAuth,
        global renames and vanishing (Wikimedia, Miraheze), and Phalanx,
        which is Fandom's equivalent.
      * `interwiki` — the Interwiki extension's table edits.
      * `translate` — the Translate extension: page translation and
        translation review (translatewiki, mediawiki.org, Miraheze).
      * `approval` — revision approval: ApprovedRevs, FlaggedRevs, and
        Wikipedia's PageTriage curation.
      * `structured` — Cargo tables, Sprite sheets and
        TemplateClassification (the last is Fandom-only).
      * `profile` — social features: CurseProfile and article comments
        (Fandom and the Gamepedia-era wikis).
    """
    kind = str(event.get("type") or "")
    if kind == "edit":
        return "edit"
    if kind == "new":
        return "new"
    if kind == "discussion":
        return "discussion"
    if kind == "log":
        return _LOGTYPE_GROUPS.get(str(event.get("logtype") or ""), "other")
    return "other"

def event_colour(event):
    """The Discord embed's stripe colour, by group — the same event always
    the same colour, so a channel becomes skimmable."""
    return _GROUP_COLOURS.get(event_group(event), _GROUP_COLOURS["other"])

def _strip_namespace(title):
    """A user page title reduced to the user's name.

    Logs about people put the target in the title as `User:Name`; the
    namespace prefix is noise in a sentence that already says 'blocked'."""
    text = str(title or "")
    return text.split(":", 1)[1] if ":" in text else text

def _format_size(event):
    """The `(+42)` an edit carries, or an empty string when the change has no
    before-and-after size (a log entry, or a wiki that withheld them)."""
    old_len, new_len = event.get("oldlen"), event.get("newlen")
    if old_len is None or new_len is None:
        return ""
    return f" ({int(new_len) - int(old_len):+d})"

def _log_params(event):
    """The log entry's own parameters as a plain dict (absent for edits)."""
    params = event.get("logparams")
    return params if isinstance(params, dict) else {}

def _event_key(event):
    """The localization key whose sentence describes this change.

    Three tiers, narrowing outward: the exact log action when it is worth its
    own wording, otherwise a sentence for the extension group naming the
    action, and failing both the generic line. That is what makes an
    extension nobody has taught the bot about still readable — and why adding
    support for one is usually a mapping entry, not new code."""
    group = event_group(event)
    if group == "edit":
        return "wiki_ev_edit"
    if group == "new":
        return "wiki_ev_new"
    if group == "discussion":
        return "wiki_ev_discussion_reply" if event.get("is_reply") else "wiki_ev_discussion"
    key = _ACTION_KEYS.get(
        (str(event.get("logtype") or ""), str(event.get("logaction") or "")))
    return key or _GROUP_FALLBACK_KEYS.get(group) or "wiki_ev_other"

def _format_groups(groups):
    """A rights log's group list as text. An empty list is a real answer —
    the user belonged to no groups — and reads as an em dash rather than as
    nothing at all, which would leave the sentence looking truncated."""
    if isinstance(groups, list):
        return ", ".join(groups) if groups else "—"
    return str(groups or "—")

def _event_fields(event):
    """The substitutions a sentence may ask for.

    Every key gets the same dict, so a translation is free to mention the
    target or the duration or neither — `localized` ignores what a template
    does not use, and a missing value renders as an em dash rather than
    breaking the line."""
    params = _log_params(event)
    title = str(event.get("title") or "—")
    target = params.get("target_title") or params.get("newuser")
    if not target and str(event.get("logtype")) in ("block", "rights", "newusers", "renameuser"):
        target = _strip_namespace(title)
    duration = params.get("duration-l10n") or params.get("duration") or ""
    groups_from = params.get("oldgroups")
    groups_to = params.get("newgroups")

    return {
        "user": str(event.get("user") or "—"),
        "title": title,
        "page": title,
        "target": str(target or "—"),
        "size": _format_size(event),
        "duration": f" ({duration})" if duration else "",
        "old_groups": _format_groups(groups_from),
        "new_groups": _format_groups(groups_to),
        "action": f"{event.get('logtype') or '?'}/{event.get('logaction') or '?'}",
    }

def _flag_suffix(event, lang):
    """The `[minor, bot]` marker after a sentence, in the reader's language."""
    flags = []
    if event.get("minor"):
        flags.append(localized("wiki_flag_minor", lang))
    if event.get("bot"):
        flags.append(localized("wiki_flag_bot", lang))
    return f" [{', '.join(flags)}]" if flags else ""

def render_event(event, lang=DEFAULT_LANG, with_comment=True):
    """One sentence describing the change, in `lang`.

    This is what Telegram receives verbatim and what Discord shows when a
    subscription asks for compact text instead of an embed. The comment is
    left off when the caller shows it somewhere else — an embed puts it in
    its own body rather than trailing the title."""
    key = _event_key(event)
    fields = _event_fields(event)
    sentence = localized(key, lang, **fields) + _flag_suffix(event, lang)
    comment = str(event.get("comment_clean") or "").strip()
    if with_comment and comment:
        return f"{sentence}: {comment}"
    return sentence

def render_merged(events, lang=DEFAULT_LANG):
    """One sentence standing in for several changes to the same page.

    Used when a wiki is moving faster than the chat can usefully follow: the
    page is named once, with how many changes it saw, who made them and the
    net size difference, instead of a line each."""
    first = events[0]
    users = []
    for event in events:
        user = str(event.get("user") or "")
        if user and user not in users:
            users.append(user)
    shown = ", ".join(users[:3])
    if len(users) > 3:
        shown = localized("wiki_users_and_more", lang, users=shown, count=len(users) - 3)

    total = 0
    sized = False
    for event in events:
        old_len, new_len = event.get("oldlen"), event.get("newlen")
        if old_len is not None and new_len is not None:
            total += int(new_len) - int(old_len)
            sized = True

    return localized(
        "wiki_ev_merged", lang,
        title=str(first.get("title") or "—"),
        count=len(events),
        users=shown,
        size=f" ({total:+d})" if sized else "",
    )

def parse_group_list(raw):
    """A comma- or space-separated list of group names as a validated set.

    Returns ``(groups, unknown)``; the caller reports the unknown ones rather
    than silently filtering on a typo. The words 'all' and 'none' are
    accepted as the obvious shorthands."""
    tokens = [t.strip().lower() for t in str(raw or "").replace(",", " ").split() if t.strip()]
    if not tokens:
        return None, []
    if len(tokens) == 1 and tokens[0] == "all":
        return set(EVENT_GROUPS), []
    if len(tokens) == 1 and tokens[0] == "none":
        return set(), []
    known = {t for t in tokens if t in EVENT_GROUPS}
    unknown = [t for t in tokens if t not in EVENT_GROUPS]
    return known, unknown

def parse_namespace_list(raw):
    """Namespace numbers as a validated set.

    Returns ``(namespaces, unknown)``. 'all' clears the filter. MediaWiki
    namespaces are numbers (0 main, 6 File, 14 Category, …) and negative ones
    exist (Special is -1), so anything integral is accepted."""
    tokens = [t.strip() for t in str(raw or "").replace(",", " ").split() if t.strip()]
    if not tokens:
        return None, []
    if len(tokens) == 1 and tokens[0].lower() == "all":
        return None, []
    known, unknown = set(), []
    for token in tokens:
        try:
            known.add(int(token))
        except ValueError:
            unknown.append(token)
    return known, unknown

SETTING_CHOICES = {
    "discord_format": ("embed", "text"),
    "delivery": ("auto", "message"),
    "webhook": ("own", "shared"),
}

_TRUE_WORDS = {"on", "yes", "true", "1", "show"}
_FALSE_WORDS = {"off", "no", "false", "0", "hide"}

def parse_settings_assignments(raw):
    """Read `option=value` pairs into a change set for db.set_wiki_feed_settings.

    Returns ``(changes, problems)``, where a problem is
    ``(kind, detail)`` — 'unknown_option', 'unknown_groups',
    'unknown_namespaces' or 'bad_value'. Both platforms funnel their input
    through here, Discord after turning its typed options back into pairs, so
    a setting behaves the same and reports the same complaint wherever it is
    typed."""
    changes, problems = {}, []
    for token in str(raw or "").split():
        if "=" not in token:
            problems.append(("unknown_option", token))
            continue
        name, _, value = token.partition("=")
        name = name.strip().lower()
        value = value.strip()

        if name in ("events", "types", "event"):
            groups, unknown = parse_group_list(value)
            if unknown:
                problems.append(("unknown_groups", unknown))
            else:
                changes["event_types"] = groups
        elif name in ("namespaces", "ns", "namespace"):
            namespaces, unknown = parse_namespace_list(value)
            if unknown:
                problems.append(("unknown_namespaces", unknown))
            else:
                changes["namespaces"] = namespaces
        elif name in ("bots", "minor"):
            column = "show_bots" if name == "bots" else "show_minor"
            lowered = value.lower()
            if lowered in _TRUE_WORDS:
                changes[column] = True
            elif lowered in _FALSE_WORDS:
                changes[column] = False
            else:
                problems.append(("bad_value", (name, value, "on/off")))
        elif name in ("format", "discord", "discord_format"):
            if value.lower() in SETTING_CHOICES["discord_format"]:
                changes["discord_format"] = value.lower()
            else:
                problems.append(("bad_value", (name, value, "embed/text")))
        elif name == "delivery":
            if value.lower() in SETTING_CHOICES["delivery"]:
                changes["delivery"] = value.lower()
            else:
                problems.append(("bad_value", (name, value, "auto/message")))
        elif name in ("webhook", "webhooks"):
            if value.lower() in SETTING_CHOICES["webhook"]:
                changes["webhook"] = value.lower()
            else:
                problems.append(("bad_value", (name, value, "own/shared")))
        else:
            problems.append(("unknown_option", name))
    return changes, problems

def describe_settings(settings, lang):
    """The settings as the words a listing shows, in the reader's language."""
    groups = settings.get("event_types")
    namespaces = settings.get("namespaces")
    every = localized("wikifeed_value_all", lang)
    on_word = localized("wikifeed_value_on", lang)
    off_word = localized("wikifeed_value_off", lang)
    return {
        "groups": ", ".join(sorted(groups)) if groups is not None and len(groups) < len(EVENT_GROUPS) else every,
        "namespaces": ", ".join(str(n) for n in sorted(namespaces)) if namespaces else every,
        "bots": on_word if settings.get("show_bots") else off_word,
        "minor": on_word if settings.get("show_minor", True) else off_word,
        "format": settings.get("discord_format", "embed"),
        "delivery": settings.get("delivery", "auto"),
        "webhook": settings.get("webhook", "own"),
    }

def settings_problem_text(problems, lang):
    """The complaints about a settings command, as lines to show the admin."""
    lines = []
    for kind, detail in problems:
        if kind == "unknown_groups":
            lines.append(localized("wikifeed_unknown_groups", lang,
                                   items=", ".join(detail),
                                   available=", ".join(EVENT_GROUPS)))
        elif kind == "unknown_namespaces":
            lines.append(localized("wikifeed_unknown_namespaces", lang,
                                   items=", ".join(detail)))
        elif kind == "bad_value":
            option, value, allowed = detail
            lines.append(localized("wikifeed_bad_value", lang, option=option,
                                   value=value, allowed=allowed))
        else:
            lines.append(localized("wikifeed_unknown_option", lang, items=detail))
    return lines

def event_matches(event, settings):
    """Whether a subscription wants to hear about this change.

    Four filters, cheapest first: the group switch, the namespace list, and
    the bot and minor flags. `settings` is the row db.get_wiki_feed_settings
    returns, so a subscription nobody has configured passes everything except
    the groups off by default."""
    groups = settings.get("event_types")
    if groups is not None and event_group(event) not in groups:
        return False

    namespaces = settings.get("namespaces")
    if namespaces is not None:
        ns = event.get("ns")
        if ns is None or int(ns) not in namespaces:
            return False

    if event.get("bot") and not settings.get("show_bots", False):
        return False
    if event.get("minor") and not settings.get("show_minor", True):
        return False
    return True
