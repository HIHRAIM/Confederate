"""Reading a public YouTube channel's uploads.

The uploads come from the channel's own Atom feed — the one YouTube has published
at `/feeds/videos.xml` for as long as it has had channels. It needs no API key,
no quota and no developer project, which is what sets it apart from the Data API:
nothing here can be switched off for this bot in particular, and there is no
credential to keep. The price is that the feed holds only the newest fifteen
uploads and says nothing about Community-tab posts, so those are not relayed.

Only uploads pass through here, and Shorts and livestreams arrive among them:
the feed marks neither, and telling them apart would mean a second request per
video for something the feed does not promise to keep answering.

An upload is relayed as its title and its watch link, and nothing else. The
thumbnail the feed offers is deliberately dropped: Discord and Telegram both
turn a YouTube link into a preview card carrying that same still, so relaying it
as well would put the picture in the message twice — once as the card, once as a
file the bot re-uploaded to GALLERY under a name like `maxresdefault.jpg`. So
`media` is always empty here, and the relay's "[N files from …]" footer never
appears for a YouTube post either.

The module only fetches and parses; it hands back the post dicts the relay
expects, the same shape the other feed readers produce:

    {"id": str, "text": str, "link": str|None, "media": [],
     "created_at": int|None, "author_name": str|None,
     "forward_type": None, "forward_name": None}

`id` is the upload's publication time in whole seconds rather than its video id.
The relay orders posts and remembers its place with `int(id)`, and a video id is
eleven characters of base64 that carry no order at all — the timestamp is the one
number in the feed that does.
"""

import asyncio
import re
from datetime import datetime
from xml.etree import ElementTree

import aiohttp

from utils import FEED_REQUEST_HEADERS, FEED_REQUEST_TIMEOUT, FeedError

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHANNEL_PAGE_URL = "https://www.youtube.com/{source}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

RETRY_DELAY = 30
MAX_POSTS_PER_FETCH = 15

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
LEGACY_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"

_CANONICAL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'
)
_EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')

_CONSENT_HEADERS = dict(FEED_REQUEST_HEADERS, Cookie="SOCS=CAI")

def normalize_source(raw):
    """Accept a `UC…` channel id, an `@handle`, a legacy `/c/` or `/user/` name,
    or a link to any of them, and return the bare source. ``None`` when it is not
    one."""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(www\.|m\.)?youtube\.com/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(channel|c|user)/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("/", 1)[0].strip()

    if CHANNEL_ID_RE.match(text):
        return text
    if not text.startswith("@"):
        text = f"@{text}" if LEGACY_NAME_RE.match(text) else text
    return text if HANDLE_RE.match(text) else None

def source_url(source):
    """Link to the channel, for `/bridge` listings — the /channel/ form for a
    raw id, the plain path for a handle."""
    if CHANNEL_ID_RE.match(source or ""):
        return f"https://www.youtube.com/channel/{source}"
    return f"https://www.youtube.com/{source}"

def post_url(video_id):
    """Watch link of one video."""
    return WATCH_URL.format(video_id=video_id)

def _parse_time(value):
    """Epoch seconds of an RFC-3339 timestamp, or ``None``."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.strip()).timestamp())
    except Exception:
        return None

def _text(node):
    """Stripped text of an XML node, or None when absent or empty."""
    return (node.text or "").strip() if node is not None and node.text else None

def parse_feed(xml, source):
    """``(channel_title, posts)`` from a channel's Atom feed.

    Posts are ordered oldest first. Raises `FeedError` when the document carries
    no feed the parser recognizes — which is also what a withdrawn or restructured
    feed looks like. A channel that has never uploaded is a feed with no entries,
    not an error."""
    try:
        root = ElementTree.fromstring(xml or "")
    except Exception as e:
        raise FeedError(f"unreadable feed: {type(e).__name__}") from e
    if not root.tag.endswith("feed"):
        raise FeedError("no feed in response")

    title_node = root.find(f"{_ATOM}title")
    channel_title = (title_node.text or "").strip() if title_node is not None else None

    posts = []
    for entry in root.findall(f"{_ATOM}entry"):
        video_node = entry.find(f"{_YT}videoId")
        video_id = (video_node.text or "").strip() if video_node is not None else None
        published = _parse_time(_text(entry.find(f"{_ATOM}published")))
        if not video_id or not published:
            continue

        group = entry.find(f"{_MEDIA}group")
        title = _text(entry.find(f"{_ATOM}title")) or ""
        if group is not None:
            title = _text(group.find(f"{_MEDIA}title")) or title

        author = entry.find(f"{_ATOM}author")
        author_name = _text(author.find(f"{_ATOM}name")) if author is not None else None

        posts.append({
            "id": str(published),
            "text": title.strip(),
            "media": [],
            "link": post_url(video_id),
            "created_at": published,
            "author_name": author_name or channel_title,
            "forward_type": None,
            "forward_name": None,
        })

    posts.sort(key=lambda p: int(p["id"]))
    return channel_title or None, posts[-MAX_POSTS_PER_FETCH:]

_channel_id_cache = {}

async def resolve_channel_id(source, session):
    """The `UC…` id behind a handle or legacy name, remembered once resolved.

    The Atom feed is only addressable by channel id, and nothing but the channel
    page maps a handle onto one. The page is two megabytes, so it is read once per
    channel and the answer kept; a failed read is not kept, so a passing outage
    does not cost that channel its feed for as long as the bot stays up."""
    if CHANNEL_ID_RE.match(source or ""):
        return source
    if source in _channel_id_cache:
        return _channel_id_cache[source]

    url = CHANNEL_PAGE_URL.format(source=source)
    async with session.get(
        url, headers=_CONSENT_HEADERS, allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
    ) as resp:
        if resp.status != 200:
            error = FeedError(f"HTTP {resp.status}")
            error.throttled = resp.status == 429
            error.retryable = resp.status >= 500
            raise error
        page = await resp.text()

    match = _CANONICAL_RE.search(page) or _EXTERNAL_ID_RE.search(page)
    if not match:
        raise FeedError("channel page carries no channel id")
    _channel_id_cache[source] = match.group(1)
    return match.group(1)

async def fetch_posts(source, session=None, retries=1, retry_delay=RETRY_DELAY):
    """``(channel_title, posts)`` for a public YouTube channel.

    Raises `FeedError` on anything that stops the bot from reading the channel: a
    network failure, a non-200 answer (no such channel, or one taken down), or a
    document the parser no longer understands. A network blip or a server error is
    retried once after a short wait; a 429 is not, and carries ``throttled = True``
    so the caller can back off."""
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(retries + 1):
            try:
                channel_id = await resolve_channel_id(source, session)
                async with session.get(
                    FEED_URL.format(channel_id=channel_id), headers=FEED_REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        return parse_feed(await resp.text(), source)
                    error = FeedError(f"HTTP {resp.status}")
                    error.throttled = resp.status == 429
                    retryable = resp.status >= 500
            except FeedError as e:
                error, retryable = e, getattr(e, "retryable", False)
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
