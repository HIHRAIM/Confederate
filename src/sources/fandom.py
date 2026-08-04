"""Reading a Fandom wiki's Discussions.

Discussions are Fandom's forum: a stream of posts that lives beside the wiki
and does not appear in `recentchanges` at all, so following it means asking a
different service — `wikia.php?controller=DiscussionPost` — which exists only
on Fandom and only on wikis where the feature is switched on. A Fandom wiki
without Discussions answers 404, and so does every wiki that is not on
Fandom; both are treated as 'this wiki has no discussions', which is what
lets `/setwikifeed` offer the stream where it exists and say nothing where it
does not.

Why this is a feed kind of its own rather than more events inside
sources/wiki.py: a post id is a ~19-digit number from a different counter
than `rcid`, and the relay remembers one position per subscription with
`int(id)`. Mixing the two streams would let the first discussion post push
the remembered position past every future recentchanges id and silently stop
the wiki's edits from ever being relayed again. Two kinds, two positions, no
interference — and both share one `wiki_feed_settings` row, so an admin
configures the wiki once.

The module only fetches and parses; it produces the same post shape as every
other reader here, with an `event` payload of `type='discussion'` that
wiki_events renders and filters like any other event.
"""

import asyncio

import aiohttp

from utils import FEED_REQUEST_TIMEOUT, FeedError

from sources.wiki import WIKI_REQUEST_HEADERS, normalize_source as normalize_wiki_source

RETRY_DELAY = 30
MAX_POSTS_PER_FETCH = 20

DISCUSSIONS_URL = ("https://{host}/wikia.php?controller=DiscussionPost&method=getPosts"
                   "&sortDirection=descending&sortKey=creation_date&limit={limit}&format=json")

FANDOM_HOSTS = (".fandom.com", ".wikia.org", ".gamepedia.com")

def normalize_source(raw):
    """The same wiki key the recentchanges reader uses.

    Deliberately shared: one wiki is one subscription key whichever of its
    two streams is being read, which is what lets the settings row and the
    `/wikifeeds` listing treat them as one thing."""
    return normalize_wiki_source(raw)

def source_url(source):
    """A link to the wiki's Discussions front page."""
    return f"https://{source}/f"

def looks_like_fandom(source):
    """Whether an address is worth asking about Discussions at all.

    A cheap host check before the request: everything else on the internet
    would answer 404, and there is no reason to spend a poll on finding that
    out."""
    host = str(source or "").split("/", 1)[0].lower()
    return any(host.endswith(suffix) for suffix in FANDOM_HOSTS)

def post_url(source, thread_id, post_id, is_reply):
    """The human link to a post: the thread, or the reply within it."""
    base = f"https://{source}/f/p/{thread_id}"
    return f"{base}/r/{post_id}" if is_reply and post_id != thread_id else base

def _text_from_json_model(node, out=None):
    """The readable text of a post, walked out of Fandom's document model.

    Posts are stored as a tree of typed nodes rather than as text; only the
    leaves marked `text` carry anything a reader wants, and the rest is
    layout. Anything unrecognized is skipped rather than guessed at."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(str(node["text"]))
        for child in node.get("content") or []:
            _text_from_json_model(child, out)
    elif isinstance(node, list):
        for child in node:
            _text_from_json_model(child, out)
    return " ".join(out).strip()

def _parse_post(raw, source):
    """One Discussions post as a relay-ready post, or None when it carries
    no id, or when it is deleted or hidden by a moderator — relaying a post
    the wiki has already taken down would defeat the moderation."""
    post_id = str(raw.get("id") or "")
    if not post_id.isdigit():
        return None
    if raw.get("isDeleted") or raw.get("isContentSuppressed"):
        return None

    author = (raw.get("createdBy") or {}).get("name") or "—"
    thread_id = str(raw.get("threadId") or post_id)
    is_reply = bool(raw.get("isReply"))
    created = ((raw.get("creationDate") or {}).get("epochSecond"))
    title = raw.get("title") or raw.get("forumName") or "Discussions"
    body = _text_from_json_model(raw.get("jsonModel"))
    link = post_url(source, thread_id, post_id, is_reply)

    event = {
        "type": "discussion",
        "ns": None,
        "title": title,
        "user": author,
        "comment": body,
        "comment_clean": body[:400],
        "bot": False,
        "minor": False,
        "is_reply": is_reply,
        "forum": raw.get("forumName"),
        "thread_id": thread_id,
        "timestamp": created,
        "page_url": link,
        "diff_url": link,
        "site_name": None,
        "site_url": f"https://{source}",
    }
    import wiki_events
    return {
        "id": post_id,
        "text": wiki_events.render_event(event),
        "media": [],
        "link": link,
        "created_at": int(created) if created else None,
        "author_name": author,
        "forward_type": None,
        "forward_name": None,
        "event": event,
    }

def parse_discussions(payload, source):
    """``(None, posts)`` from a Discussions answer, oldest post last.

    The title is None on purpose: the wiki's own name comes from the
    recentchanges side, which shares the subscription, and inventing a second
    one here would only make the two disagree."""
    posts_raw = ((payload or {}).get("_embedded") or {}).get("doc:posts")
    if not isinstance(posts_raw, list):
        raise FeedError("no discussion posts in response")

    posts = []
    for raw in posts_raw:
        if not isinstance(raw, dict):
            continue
        post = _parse_post(raw, source)
        if post is not None:
            posts.append(post)

    posts.sort(key=lambda p: int(p["id"]))
    return None, posts[-MAX_POSTS_PER_FETCH:]

async def fetch_posts(source, session=None, retries=1, retry_delay=RETRY_DELAY):
    """``(None, posts)`` for a Fandom wiki's Discussions.

    Raises `FeedError` with ``reason='no_discussions'`` when the wiki is not
    on Fandom or has the feature switched off — the caller reads that as
    'nothing to follow here' rather than as a failure, which is how a
    subscription degrades on a wiki without the extension."""
    if not looks_like_fandom(source):
        failure = FeedError("not a Fandom wiki")
        failure.reason = "no_discussions"
        raise failure

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        url = DISCUSSIONS_URL.format(host=source.split("/", 1)[0], limit=MAX_POSTS_PER_FETCH)
        for attempt in range(retries + 1):
            try:
                async with session.get(
                    url, headers=WIKI_REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
                ) as response:
                    if response.status == 404:
                        failure = FeedError("this wiki has no Discussions")
                        failure.reason = "no_discussions"
                        raise failure
                    if response.status in (429, 503):
                        failure = FeedError(f"HTTP {response.status}")
                        failure.throttled = True
                        raise failure
                    if response.status != 200:
                        raise FeedError(f"HTTP {response.status}")
                    payload = await response.json(content_type=None)
                return parse_discussions(payload, source)
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

async def has_discussions(source, session=None):
    """Whether this wiki has a Discussions stream worth subscribing to.

    Asked once, when a wiki is attached, so that a Fandom wiki with the
    feature off is not given a subscription that could only ever fail."""
    try:
        await fetch_posts(source, session=session, retries=0)
        return True
    except FeedError:
        return False
