"""Reading the recent activity of any MediaWiki.

The events come from the wiki's own `api.php`, primarily `list=recentchanges`
— the same feed Special:RecentChanges is built from. Nothing else is
required of the wiki: no extension, no account, no key. Any MediaWiki
reachable over HTTP works, which is why the bot takes a plain link to the
wiki rather than a name from a list of known sites.

Finding the endpoint. A user pastes whatever link they have — an article, the
main page, `api.php` itself — so the module reduces it to a wiki key
(`host` plus the path prefix before the wiki's own entry points) and then
looks for the endpoint under it: the usual `/api.php` and `/w/api.php`
first, and failing those the `EditURI` link every MediaWiki page carries in
its head, which points at the real one. The direct probes come first
because some farms (Fandom, notably) serve `api.php` happily while refusing
plain page requests to non-browsers, so the HTML route would fail there.

Requesting politely. MediaWiki's API etiquette requires a User-Agent that
names the client and a way to reach its operator; Wikimedia answers 403 to
anything else, and the bot's usual browser-shaped header is exactly what
they reject. `maxlag` is sent so a replicating wiki can tell the bot to come
back later instead of being made slower, and that answer, like a 429, is
raised as a throttled `FeedError` for the poller's per-host backoff.

The module only fetches and parses; it hands back the post dicts the relay
expects, the same shape every feed reader here produces:

    {"id": str, "text": str, "link": str|None, "media": [],
     "created_at": int|None, "author_name": str|None,
     "forward_type": None, "forward_name": None, "event": {…}}

`id` is the change's `rcid`, which the wiki assigns in order, so the
relay's `int(id)` bookkeeping orders events and remembers its place with no
extra work. `event` carries the change in structured form — type, namespace,
title, flags, sizes, comment — for the per-chat filtering and the Discord
embeds that later build on it; the relay itself ignores keys it does not
know.
"""

import asyncio
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

import aiohttp

import config
import wiki_events
from utils import FEED_REQUEST_TIMEOUT, FeedError

CONTACT = getattr(config, "WIKI_CONTACT", "https://github.com/HIHRAIM/Confederate")

WIKI_REQUEST_HEADERS = {
    "User-Agent": f"Confederate-Bridge/1.0 (+{CONTACT}) python-aiohttp",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

RETRY_DELAY = 30
MAX_EVENTS_PER_FETCH = 500
API_MAXLAG = 5

_ENTRY_SEGMENTS = {
    "wiki", "w", "index.php", "api.php", "load.php", "rest.php", "wikia.php",
    "index.php5", "api.php5", "special",
}

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")
_PRIVATE_ERROR_CODES = {"readapidenied", "mustbeloggedin", "permissiondenied", "login-required"}
_SECTION_COMMENT_RE = re.compile(r"^/\*\s*(.*?)\s*\*/\s*")

def normalize_source(raw):
    """Reduce any link to a wiki down to the key the subscription is stored
    under, or ``None`` when it is not a usable address.

    The key is the host plus whatever path prefix comes before the wiki's own
    entry points, with the scheme dropped: an article, the main page and
    `api.php` of one wiki all collapse onto it, so two admins pasting two
    different pages do not end up following the same wiki twice. It keeps the
    prefix because one host can carry several wikis — a Fandom language wiki
    lives at `example.fandom.com/ru`, and cutting back to the bare host would
    merge it with its English sibling."""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = text.split("#", 1)[0].split("?", 1)[0].strip()
    if not text:
        return None

    parts = [p for p in text.split("/") if p]
    if not parts:
        return None

    host = parts[0].lower()
    if not _HOST_RE.match(host) or "." not in host.split(":")[0]:
        return None

    prefix = []
    for segment in parts[1:]:
        if segment.lower() in _ENTRY_SEGMENTS or ":" in segment:
            break
        prefix.append(segment)

    return "/".join([host] + prefix)

def source_url(source):
    """A link to the wiki a human can open, for `/bridge` and the feed list."""
    return f"https://{source}"

_api_cache = {}

def _api_query_url(api_url, **params):
    """An `api.php` URL with the given query, always JSON and formatversion 2.

    Every request carries `maxlag`, which is how a caller tells a replicating
    wiki that it may refuse the request while it is catching up rather than
    serving it slowly."""
    query = {"action": "query", "format": "json", "formatversion": "2",
             "maxlag": str(API_MAXLAG)}
    query.update({k: v for k, v in params.items() if v is not None})
    encoded = "&".join(f"{k}={quote(str(v), safe='|')}" for k, v in query.items())
    return f"{api_url}?{encoded}"

def _raise_for_api_error(payload, url):
    """Turn an API-level error into the right `FeedError`.

    A wiki that wants credentials is worth telling the admin about in those
    words — the subscription will never work without an account, unlike a
    timeout — so it is marked `reason='private'`. Lag and rate limits are
    marked throttled instead, which the poller reads as 'come back later'."""
    error = (payload or {}).get("error") or {}
    code = str(error.get("code") or "")
    if not code:
        return
    if code in _PRIVATE_ERROR_CODES:
        failure = FeedError(f"the wiki requires an account to read its API ({code})")
        failure.reason = "private"
        raise failure
    if code == "maxlag":
        failure = FeedError("the wiki is lagging behind its database")
        failure.throttled = True
        raise failure
    if code in ("ratelimited", "toomanyrequests"):
        failure = FeedError("the wiki is rate-limiting us")
        failure.throttled = True
        raise failure
    raise FeedError(f"API error {code} from {url}")

async def _get_json(url, session):
    """Fetch one API URL and return the decoded payload.

    Returns None when the address answers with something that is not a
    MediaWiki API (a 404, or a page of HTML), which is what makes it usable
    for probing candidate endpoints. Refusals that a different address would
    not fix — auth, lag, throttling — are raised instead of swallowed."""
    try:
        async with session.get(
            url, headers=WIKI_REQUEST_HEADERS, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
        ) as response:
            if response.status in (429, 503):
                failure = FeedError(f"HTTP {response.status}")
                failure.throttled = True
                raise failure
            if response.status in (401, 403):
                failure = FeedError(f"HTTP {response.status}: the wiki refuses anonymous API access")
                failure.reason = "private"
                raise failure
            if response.status != 200:
                return None
            try:
                payload = await response.json(content_type=None)
            except Exception:
                return None
    except FeedError:
        raise
    except Exception as e:
        raise FeedError(f"{type(e).__name__}: {e}") from e

    if not isinstance(payload, dict):
        return None
    _raise_for_api_error(payload, url)
    return payload

async def _probe_api(api_url, session):
    """Ask a candidate endpoint to describe itself. Returns the wiki's
    `siteinfo` when the address really is an `api.php`, otherwise None."""
    payload = await _get_json(
        _api_query_url(api_url, meta="siteinfo", siprop="general"), session)
    general = ((payload or {}).get("query") or {}).get("general")
    return general if isinstance(general, dict) else None

_EDIT_URI_RE = re.compile(
    r"""<link[^>]*?rel=["']EditURI["'][^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
_EDIT_URI_REVERSED_RE = re.compile(
    r"""<link[^>]*?href=["']([^"']+)["'][^>]*?rel=["']EditURI["']""", re.IGNORECASE)

async def _api_url_from_page(base, session):
    """The `api.php` a wiki advertises in the `EditURI` link of its pages.

    The fallback for installations whose endpoint sits somewhere the direct
    probes do not guess. It costs an HTML page rather than a small JSON
    answer, and some hosts refuse those to non-browsers, which is why it runs
    last rather than first."""
    try:
        async with session.get(
            base, headers={**WIKI_REQUEST_HEADERS, "Accept": "text/html"},
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
        ) as response:
            if response.status != 200:
                return None
            html = await response.text()
    except Exception:
        return None

    match = _EDIT_URI_RE.search(html) or _EDIT_URI_REVERSED_RE.search(html)
    if not match:
        return None
    href = match.group(1).split("?", 1)[0]
    if href.startswith("//"):
        return f"{urlsplit(base).scheme}:{href}"
    if href.startswith("http"):
        return href
    return f"{base.rstrip('/')}/{href.lstrip('/')}"

async def resolve_api(source, session):
    """``(api_url, siteinfo)`` for a wiki key, remembered once found.

    Tries the two script paths nearly every wiki uses before falling back to
    what the wiki says about itself, and tries plain HTTP only after HTTPS
    has failed outright, so an intranet wiki is still reachable without
    costing every public wiki a wasted request. A failed lookup is not
    cached: a wiki that was down for one poll should not stay unreachable for
    as long as the bot runs."""
    cached = _api_cache.get(source)
    if cached:
        return cached

    private_failure = None
    last_failure = None
    for scheme in ("https", "http"):
        base = f"{scheme}://{source}"
        candidates = [f"{base}/api.php", f"{base}/w/api.php"]
        discovered = await _api_url_from_page(base, session)
        if discovered and discovered not in candidates:
            candidates.append(discovered)

        for candidate in candidates:
            try:
                general = await _probe_api(candidate, session)
            except FeedError as e:
                if getattr(e, "throttled", False):
                    raise
                if getattr(e, "reason", None) == "private":
                    private_failure = e
                    continue
                last_failure = e
                continue
            if general:
                _api_cache[source] = (candidate, general)
                return candidate, general

    if private_failure is not None:
        raise private_failure
    if last_failure is not None:
        raise last_failure
    failure = FeedError("no MediaWiki API found at this address")
    failure.reason = "not_mediawiki"
    raise failure

def _site_base(api_url, general):
    """The wiki's own idea of its address, as an absolute URL.

    `siteinfo` reports the server protocol-relative more often than not, so
    the scheme is taken from the endpoint that answered."""
    server = str(general.get("server") or "")
    if server.startswith("//"):
        return f"{urlsplit(api_url).scheme}:{server}"
    if server.startswith("http"):
        return server
    return f"{urlsplit(api_url).scheme}://{urlsplit(api_url).netloc}"

def page_url(site_base, general, title):
    """A link to a page by title, built from the wiki's own article path."""
    article_path = str(general.get("articlepath") or "/wiki/$1")
    return site_base + article_path.replace("$1", quote(str(title or "").replace(" ", "_"), safe=":/"))

def diff_url(site_base, general, change):
    """A link to what a change actually did.

    An edit points at the diff against the revision it replaced; anything
    without a previous revision — a page creation, a log entry — points at
    the page itself, since there is no diff to show."""
    script_path = str(general.get("scriptpath") or "")
    script = f"{site_base}{script_path}/index.php"
    revid = change.get("revid")
    old_revid = change.get("old_revid")
    if revid and old_revid:
        return f"{script}?diff={revid}&oldid={old_revid}"
    if revid:
        return f"{script}?oldid={revid}"
    return page_url(site_base, general, change.get("title"))

def _parse_time(value):
    """Epoch seconds of an ISO-8601 timestamp as MediaWiki writes it, or None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def _format_size(change):
    """The `(+42)` / `(-7)` a reader expects after an edit, or an empty
    string when the change carries no before-and-after size."""
    old_len, new_len = change.get("oldlen"), change.get("newlen")
    if old_len is None or new_len is None:
        return ""
    delta = int(new_len) - int(old_len)
    return f" ({delta:+d})"

def clean_comment(comment, limit=180):
    """An edit summary as a reader can use it.

    MediaWiki stores the section a summary belongs to as a `/* … */` marker
    at its head; it is rendered as an arrow onto the section name, the way
    the wiki's own history pages do, rather than shown as raw markup. Long
    summaries are clipped — the line is a notification, not the edit."""
    text = str(comment or "").strip()
    if not text:
        return ""
    section = _SECTION_COMMENT_RE.match(text)
    if section:
        rest = _SECTION_COMMENT_RE.sub("", text).strip()
        heading = section.group(1)
        text = f"→ {heading}: {rest}" if rest else f"→ {heading}"
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text

def _build_event(change, site_base, general):
    """A recent-changes entry as the structured event the rest of the bot
    works with.

    Everything the renderers, the filters and the embeds need is flattened
    into one dict here, including the two links, so that no later step has to
    know how a wiki addresses its pages. `logparams` is kept as it arrived —
    each log type puts different things in it, and the renderer for that type
    is the only code that knows which."""
    return {
        "type": change.get("type"),
        "ns": change.get("ns"),
        "title": change.get("title"),
        "user": change.get("user"),
        "comment": change.get("comment"),
        "comment_clean": clean_comment(change.get("comment")),
        "bot": bool(change.get("bot")),
        "minor": bool(change.get("minor")),
        "new": bool(change.get("new")),
        "oldlen": change.get("oldlen"),
        "newlen": change.get("newlen"),
        "revid": change.get("revid"),
        "old_revid": change.get("old_revid"),
        "logid": change.get("logid"),
        "logtype": change.get("logtype"),
        "logaction": change.get("logaction"),
        "logparams": change.get("logparams") or {},
        "timestamp": change.get("timestamp"),
        "page_url": page_url(site_base, general, change.get("title")),
        "diff_url": diff_url(site_base, general, change),
        "site_name": str(general.get("sitename") or "") or None,
        "site_url": site_base,
    }

def _build_post(change, site_base, general):
    """One recent-changes entry as a relay-ready post, or None when it
    carries no id to order it by.

    The post's own `text` is the English sentence, which is what the relay
    falls back to; the wiki delivery path re-renders it in each target chat's
    language from the `event` payload."""
    rcid = change.get("rcid")
    if rcid is None:
        return None
    if change.get("type") not in ("edit", "new", "log"):
        return None

    event = _build_event(change, site_base, general)
    return {
        "id": str(int(rcid)),
        "text": wiki_events.render_event(event),
        "media": [],
        "link": event["diff_url"],
        "created_at": _parse_time(change.get("timestamp")),
        "author_name": str(change.get("user") or "") or None,
        "forward_type": None,
        "forward_name": None,
        "event": event,
    }

def parse_recent_changes(payload, api_url, general):
    """``(site_name, posts)`` from a `list=recentchanges` answer.

    Posts come out oldest first, the order the relay delivers them in. A wiki
    where nothing has happened yet is an empty list, not an error; an answer
    with no `recentchanges` at all means the address stopped being an API and
    is raised as one."""
    changes = ((payload or {}).get("query") or {}).get("recentchanges")
    if not isinstance(changes, list):
        raise FeedError("no recentchanges in response")

    site_base = _site_base(api_url, general)
    posts = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        post = _build_post(change, site_base, general)
        if post is not None:
            posts.append(post)

    posts.sort(key=lambda p: int(p["id"]))
    return str(general.get("sitename") or "") or None, posts[-MAX_EVENTS_PER_FETCH:]

async def fetch_posts(source, session=None, retries=1, retry_delay=RETRY_DELAY):
    """``(site_name, posts)`` for a wiki, newest activity last.

    Raises `FeedError` on anything that stops the bot from reading the wiki:
    no API at the address (`reason='not_mediawiki'`), one that wants an
    account (`reason='private'`), lag or rate limiting (`throttled`), or a
    network failure. A transient failure is retried once; a refusal that a
    retry cannot change is not."""
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(retries + 1):
            try:
                api_url, general = await resolve_api(source, session)
                payload = await _get_json(
                    _api_query_url(
                        api_url,
                        list="recentchanges",
                        rcprop="title|ids|sizes|flags|user|timestamp|comment|loginfo",
                        rclimit=str(MAX_EVENTS_PER_FETCH),
                        rctype="edit|new|log",
                    ),
                    session,
                )
                if payload is None:
                    raise FeedError("the API stopped answering with JSON")
                return parse_recent_changes(payload, api_url, general)
            except FeedError as e:
                error = e
                retryable = not (getattr(e, "throttled", False) or getattr(e, "reason", None))
            except Exception as e:
                error = FeedError(f"{type(e).__name__}: {e}")
                retryable = True
            if not retryable or attempt >= retries:
                raise error
            await asyncio.sleep(retry_delay)
        raise error
    finally:
        if owns_session:
            await session.close()
