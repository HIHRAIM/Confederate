"""Reading a public Bluesky account's posts.

The posts come from Bluesky's public AppView — the same service the web client
reads, open to anyone with no key, no account and no developer agreement. It is
a documented, versioned API rather than an embed endpoint read sideways, and it
answers every caller alike with a budget of thousands of reads per window, so it
is unlikely to vanish underfoot the way an undocumented endpoint can. If it ever
does, the fetch raises `FeedError` and the bot reports it to the service chats.

The module only fetches and parses; it hands back the post dicts the relay
expects, the same shape every feed reader here produces:

    {"id": str, "text": str, "link": str|None, "media": [media, …],
     "created_at": int|None, "author_name": str|None,
     "forward_type": "user"|None, "forward_name": str|None}

    media: {"urls": [largest, …, smallest], "kind": str, "index": int, "name": str}

`id` is not the post's record key but the integer that key encodes. The relay
orders posts and remembers its place with `int(id)`, and a record key is
normally a TID — a base32 rendering of a 64-bit clock reading — so decoding one
keeps that arithmetic working without teaching the relay a second ordering.
"""

import asyncio
import re
from datetime import datetime
from urllib.parse import quote

import aiohttp

from utils import (
    FEED_REQUEST_HEADERS, FEED_REQUEST_TIMEOUT, FeedError, feed_media_name,
)

AUTHOR_FEED_URL = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
                   "?actor={actor}&limit={limit}&filter=posts_no_replies")
PLC_DIRECTORY_URL = "https://plc.directory/{did}"
BLOB_URL = "{pds}/xrpc/com.atproto.sync.getBlob?did={did}&cid={cid}"

RETRY_DELAY = 30
MAX_POSTS_PER_FETCH = 20

HANDLE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
DID_RE = re.compile(r"^did:(plc:[a-z2-7]{24}|web:[a-z0-9.-]+)$")

_REPOST = "app.bsky.feed.defs#reasonRepost"
_LINK_FACET = "app.bsky.richtext.facet#link"

def normalize_source(raw):
    """Accept `handle`, `@handle`, `bsky.app/profile/<handle>`, a DID or a link
    to a post, and return the bare actor. ``None`` when it is not one."""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(www\.)?bsky\.app/profile/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("/", 1)[0]
    text = text.lstrip("@").strip().lower()
    if DID_RE.match(text):
        return text
    return text if len(text) <= 253 and HANDLE_RE.match(text) else None

def source_url(actor):
    """Link to the account's profile, for `/bridge` listings."""
    return f"https://bsky.app/profile/{actor}"

def post_url(actor, rkey):
    """Link to one post of the account."""
    return f"https://bsky.app/profile/{actor}/post/{rkey}"

_TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_TID_VALUES = {char: value for value, char in enumerate(_TID_ALPHABET)}

def _tid_to_int(rkey):
    """The 64-bit clock reading a TID record key encodes, or ``None`` when the
    key is not a TID.

    A TID is 13 base32-sortable characters holding microseconds since the epoch
    and a clock id, so the decoded integer orders exactly as the key sorts."""
    if not rkey or len(rkey) != 13:
        return None
    value = 0
    for char in rkey:
        digit = _TID_VALUES.get(char)
        if digit is None:
            return None
        value = value * 32 + digit
    return value

def _rkey(uri):
    """The record key — the last path segment of an at:// URI."""
    return uri.rsplit("/", 1)[-1] if uri else None

def _parse_time(value):
    """Epoch seconds of an ISO-8601 timestamp, or ``None``."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def _sort_id(uri, when):
    """The integer the relay orders this post by.

    A TID record key decodes straight into one. A record key that is not a TID —
    a hand-written one — falls back to the timestamp, shifted onto the scale a
    TID uses so that the two remain comparable."""
    value = _tid_to_int(_rkey(uri))
    if value is not None:
        return value
    seconds = _parse_time(when)
    return (seconds * 1_000_000) << 10 if seconds else 0

def _display_name(user):
    """An account's shown name, falling back to its handle, or None."""
    return ((user or {}).get("displayName") or (user or {}).get("handle") or "").strip() or None

def _looks_truncated(shown, uri):
    """Whether a link's visible text is a shortened rendering of where it points
    rather than a caption someone wrote."""
    if shown == uri:
        return False
    if shown.endswith("…") or shown.endswith("..."):
        return True
    trimmed = shown.rstrip("…").rstrip(".")
    return any(uri == candidate or uri.startswith(candidate)
               for candidate in (f"https://{trimmed}", f"http://{trimmed}"))

def _expand_text(record):
    """Post text with shortened links restored to their full form.

    Bluesky keeps a link's visible text and its target apart: a long link is
    displayed clipped, and only the facet says where it actually goes. Relayed
    copies are plain text with no room for markup, so the target has to be
    written into the text or it is lost. Facet offsets index the UTF-8 encoding,
    which is why the substitution happens on the encoded form, and run
    back-to-front so that earlier offsets stay valid."""
    text = record.get("text") or ""
    raw = text.encode("utf-8")

    spans = []
    for facet in record.get("facets") or []:
        uri = next((f.get("uri") for f in facet.get("features") or []
                    if f.get("$type") == _LINK_FACET and f.get("uri")), None)
        index = facet.get("index") or {}
        start, end = index.get("byteStart"), index.get("byteEnd")
        if uri and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(raw):
            spans.append((start, end, uri))

    for start, end, uri in sorted(spans, reverse=True):
        shown = raw[start:end].decode("utf-8", "ignore")
        if _looks_truncated(shown, uri):
            raw = raw[:start] + uri.encode("utf-8") + raw[end:]
    return raw.decode("utf-8", "ignore").strip()

def _as_jpeg(url):
    """The CDN's JPEG rendering of a picture.

    Asked plainly the CDN answers in WebP, which the GALLERY upload would then
    hand out under the `.jpg` name it derives from the kind; naming the format
    outright keeps the file and its name telling the same story."""
    if not url or "@" in url.rsplit("/", 1)[-1]:
        return url
    return f"{url}@jpeg"

def _collect_media(post):
    """Attachments of one post, as the media items described at the top.

    Pictures come straight from the AppView's CDN. A video does not: the CDN
    only offers it as a playlist of streaming chunks, and the one downloadable
    copy is the original file on the author's own server, whose address is not
    in this answer. So a video's ladder starts as just the poster frame and
    carries the two identifiers `fetch_posts` needs to prepend the real file
    once it has looked that address up — and if the lookup fails, the poster
    frame is what gets relayed instead of nothing."""
    embed = post.get("embed") or {}
    if embed.get("$type") == "app.bsky.embed.recordWithMedia#view":
        embed = embed.get("media") or {}

    out = []

    def add(urls, kind, **extra):
        """Append one attachment with its rendition ladder, largest first."""
        urls = [u for u in urls if u]
        if not urls:
            return
        index = len(out)
        out.append({"urls": urls, "kind": kind, "index": index, "prefix": "bsky-media",
                    "name": feed_media_name(urls[0], index, kind, prefix="bsky-media"),
                    "short_url": None, **extra})

    kind = embed.get("$type")
    if kind == "app.bsky.embed.images#view":
        for image in embed.get("images") or []:
            add([_as_jpeg(image.get("fullsize")), _as_jpeg(image.get("thumb"))], "photo")
    elif kind == "app.bsky.embed.gallery#view":
        for image in embed.get("items") or []:
            add([_as_jpeg(image.get("fullsize")), _as_jpeg(image.get("thumbnail"))], "photo")
    elif kind == "app.bsky.embed.video#view":
        add([embed.get("thumbnail")], "video",
            blob_did=(post.get("author") or {}).get("did"), blob_cid=embed.get("cid"))
    return out

def _parse_post(item, actor):
    """One author-feed entry as a relay-ready post, or ``None`` when it carries
    nothing the parser can make sense of."""
    post = item.get("post") or {}
    record = post.get("record") or {}
    uri = post.get("uri")
    if not uri or not isinstance(record, dict):
        return None

    author = post.get("author") or {}
    reason = item.get("reason") or {}
    repost = reason.get("$type") == _REPOST

    if repost:
        sort_uri, when = reason.get("uri"), reason.get("indexedAt")
    else:
        sort_uri, when = uri, post.get("indexedAt") or record.get("createdAt")

    return {
        "id": str(_sort_id(sort_uri, when)),
        "text": _expand_text(record),
        "media": _collect_media(post),
        "link": post_url(author.get("handle") or actor, _rkey(uri)),
        "created_at": _parse_time(when),
        "author_name": _display_name(reason.get("by")) if repost else _display_name(author),
        "forward_type": "user" if repost else None,
        "forward_name": _display_name(author) if repost else None,
    }

def parse_feed(payload, actor):
    """``(display_name, posts)`` from an author-feed answer.

    Posts are ordered oldest first. Replies are already gone before the parser
    sees them: the AppView is asked for `posts_no_replies`, which is exactly the
    "posts and reposts, without replies" the bridge relays — so the parser has
    nothing to throw away, and cannot mistake a busy account for a silent one.
    An account that has never posted is a feed with no
    entries, not an error; a name that does not exist is refused by the AppView
    long before this point. Raises `FeedError` when the answer carries no feed
    the parser recognizes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("feed"), list):
        raise FeedError("no feed in response")

    posts, display_name = [], None
    for entry in payload["feed"]:
        if not isinstance(entry, dict):
            continue
        post = _parse_post(entry, actor)
        if post is None:
            continue
        display_name = display_name or post["author_name"]
        posts.append(post)

    posts.sort(key=lambda p: int(p["id"]))
    return display_name, posts[-MAX_POSTS_PER_FETCH:]

_pds_cache = {}

async def _pds_endpoint(session, did):
    """Where an account's own server lives, read from its DID document.

    Only videos need this — everything else is served by the AppView — and only
    once per account, since the answer changes about as often as an account
    moves house. A lookup that fails is not remembered, so a passing outage does
    not cost that account its videos for as long as the bot stays up."""
    if not did:
        return None
    if did in _pds_cache:
        return _pds_cache[did]

    if did.startswith("did:web:"):
        url = f"https://{did[len('did:web:'):]}/.well-known/did.json"
    else:
        url = PLC_DIRECTORY_URL.format(did=did)
    try:
        async with session.get(
            url, headers=FEED_REQUEST_HEADERS,
            timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            document = await resp.json(content_type=None)
    except Exception:
        return None

    for service in (document or {}).get("service") or []:
        if service.get("type") == "AtprotoPersonalDataServer":
            endpoint = (service.get("serviceEndpoint") or "").rstrip("/") or None
            if endpoint:
                _pds_cache[did] = endpoint
            return endpoint
    return None

async def _attach_video_files(session, posts):
    """Put the original video file at the head of each video's ladder.

    Left alone the ladder holds only the poster frame, so this is what makes a
    relayed video a video; when the author's server cannot be found the frame
    stays, and the post arrives as a picture rather than not at all."""
    for post in posts:
        for item in post.get("media") or []:
            cid = item.pop("blob_cid", None)
            did = item.pop("blob_did", None)
            if not cid or not did:
                continue
            endpoint = await _pds_endpoint(session, did)
            if endpoint:
                item["urls"].insert(0, BLOB_URL.format(pds=endpoint, did=did, cid=cid))

async def fetch_posts(actor, session=None, retries=1, retry_delay=RETRY_DELAY):
    """``(display_name, posts)`` for a public Bluesky account.

    Raises `FeedError` on anything that stops the bot from reading the account:
    a network failure, a non-200 answer (no such account, or one taken down), or
    an answer the parser no longer understands. A network blip or a server error
    is retried once after a short wait; a 429 is not, and carries
    ``throttled = True`` so the caller can back off. A throttle here means
    something has gone wrong rather than something normal: the AppView's budget
    runs to thousands of reads per window."""
    url = AUTHOR_FEED_URL.format(actor=quote(actor, safe=""), limit=MAX_POSTS_PER_FETCH)
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(retries + 1):
            try:
                async with session.get(
                    url, headers=FEED_REQUEST_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=FEED_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        display_name, posts = parse_feed(
                            await resp.json(content_type=None), actor)
                        await _attach_video_files(session, posts)
                        return display_name, posts
                    error = FeedError(f"HTTP {resp.status}")
                    error.throttled = resp.status == 429
                    retryable = resp.status >= 500
            except FeedError:
                raise
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
