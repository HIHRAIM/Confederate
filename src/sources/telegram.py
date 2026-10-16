"""Reading a public Telegram channel's posts without being a member.

The posts come from the channel's public web preview (`t.me/s/<name>`) — the
page Telegram serves to browsers with no account and no API key. There is no
Bot API for reading a channel the bot was not added to, so scraping the
preview is the only way to follow an arbitrary public channel; the price is
that the markup is undocumented and can change underfoot, which is why every
parse failure raises `FeedError` and gets reported to the service chats
instead of passing silently. A channel the bot *is* an admin of never goes
through here — its posts arrive as `channel_post` updates and the feed is
marked live (see telegram_bot/feeds.py).

The module only fetches and parses; it hands back the post dicts the relay
expects, the same shape every feed reader produces:

    {"id": str, "text": str, "link": str|None, "media": [media, …],
     "author_name": str|None, "unavailable_media": 0|1,
     "forward_type": "chat"|"user"|None, "forward_name": str|None}

    media: {"urls": [url], "kind": str, "index": int, "name": str}

`id` is the channel-message number, which is already the ordering integer
the relay wants. Note there is no `created_at`: the preview's timestamps are
not worth parsing, so channel feeds rely on the default staleness window.
"""

import asyncio
import html as html_module
import re
from html.parser import HTMLParser

import aiohttp

from utils import (
    FEED_REQUEST_HEADERS, FEED_REQUEST_TIMEOUT, FeedError, feed_media_name,
)

PREVIEW_URL = "https://t.me/s/{channel}"

PREVIEW_RETRY_DELAY = 10
MAX_POSTS_PER_FETCH = 20

CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

_MESSAGE_RE = re.compile(r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"/]+)/(\d+)"')
_PHOTO_RE = re.compile(r'tgme_widget_message_photo_wrap[^>]*background-image:url\(\'([^\']+)\'\)')
_VIDEO_RE = re.compile(r'<video[^>]+src="([^"]+)"')
_UNSUPPORTED_RE = re.compile(r'message_media_not_supported_wrap')
_FORWARD_RE = re.compile(
    r'<a class="tgme_widget_message_forwarded_from_name"(?:\s+href="([^"]*)")?[^>]*>(.*?)</a>',
    re.DOTALL,
)
_FORWARD_PLAIN_RE = re.compile(
    r'<span class="tgme_widget_message_forwarded_from_name"[^>]*>(.*?)</span>', re.DOTALL
)
_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_SERVICE_RE = re.compile(r'class="[^"]*\bservice_message\b')

class _PreviewTextParser(HTMLParser):
    """Read visible text, including emoji fallbacks and nested text blocks.

    With message_only, capture the whole message-text div rather than stopping
    at its first nested closing div. Attribute values are decoded by HTMLParser;
    decoding them a second time would corrupt literal entity spellings.
    """

    BLOCK_TAGS = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, *, message_only=False):
        super().__init__(convert_charrefs=True)
        self.message_only = message_only
        self.depth = 0
        self.finished = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.message_only:
            if self.finished:
                return
            if not self.depth:
                classes = (attrs.get("class") or "").split()
                if tag == "div" and "tgme_widget_message_text" in classes:
                    self.depth = 1
                return
            if tag == "div":
                self.depth += 1
        if tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append(attrs.get("alt") or "")
        elif tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.message_only:
            if not self.depth or self.finished:
                return
            if tag == "div":
                self.depth -= 1
                if not self.depth:
                    self.finished = True
                    return
        if tag in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.message_only or (self.depth and not self.finished):
            self.parts.append(data)

    def text(self):
        """Return the captured text without outer layout whitespace."""
        return "".join(self.parts).strip()

def normalize_source(raw):
    """Accept `name`, `@name`, `t.me/name`, `t.me/s/name` or a link to a post and
    return the bare channel name. ``None`` when it is not a channel link."""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(www\.)?t(elegram)?\.me/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^s/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("/", 1)[0]
    text = text.lstrip("@").strip()
    return text if CHANNEL_NAME_RE.match(text) else None

def source_url(channel):
    """Public link to the channel, for `/bridge` listings."""
    return f"https://t.me/{channel}"

def post_url(channel, post_id):
    """Public link to one post of the channel."""
    return f"https://t.me/{channel}/{post_id}"

def preview_html_to_text(fragment):
    """Readable preview text, with line breaks and custom emoji alt text."""
    if not fragment:
        return ""
    parser = _PreviewTextParser()
    parser.feed(fragment)
    parser.close()
    return parser.text()

def _collect_preview_media(block):
    """Attachments of one preview block, as the media items `relay_feed_post`
    expects.

    The preview serves photos as a CSS background and videos as a plain ``mp4``
    link, both in one size only — so the ladder each item carries holds one rung."""
    out = []

    def add(url, kind):
        """Append one attachment; the preview offers a single size, so each
        item's ladder holds exactly one rung."""
        if not url:
            return
        index = len(out)
        out.append({"urls": [url], "kind": kind, "index": index, "prefix": "tg-media",
                    "name": feed_media_name(url, index, kind, prefix="tg-media")})

    for url in _PHOTO_RE.findall(block):
        add(html_module.unescape(url), "photo")
    for url in _VIDEO_RE.findall(block):
        add(html_module.unescape(url), "video")
    return out

def _parse_preview_forward(block):
    """``(forward_type, forward_name)`` for a repost, or ``(None, None)``.

    A forward from another channel keeps its link in the preview; one from a
    person does not, which is what tells the two apart here."""
    m = _FORWARD_RE.search(block)
    if m:
        name = preview_html_to_text(m.group(2))
        if name:
            return ("chat" if m.group(1) else "user"), name
    m = _FORWARD_PLAIN_RE.search(block)
    if m:
        name = preview_html_to_text(m.group(1))
        if name:
            return "user", name
    return None, None

def parse_preview(html, channel):
    """``(title, posts)`` from a channel's public web preview.

    Posts are ordered oldest first; service messages ("Channel created" and the
    like) are dropped. Raises `FeedError` when the page carries no messages the
    parser recognizes — which is also what a preview-less channel and a changed
    layout look like."""
    if not html:
        raise FeedError("empty response")

    title_match = _TITLE_RE.search(html)
    title = html_module.unescape(title_match.group(1)) if title_match else None

    bounds = list(_MESSAGE_RE.finditer(html))
    if not bounds:
        raise FeedError("no posts in preview (private channel, or the preview is off)")

    posts = []
    for i, match in enumerate(bounds):
        end = bounds[i + 1].start() if i + 1 < len(bounds) else len(html)
        block = html[match.start():end]
        if _SERVICE_RE.search(block):
            continue

        post_id = match.group(2)
        text_parser = _PreviewTextParser(message_only=True)
        text_parser.feed(block)
        text_parser.close()
        media = _collect_preview_media(block)
        forward_type, forward_name = _parse_preview_forward(block)
        posts.append({
            "id": post_id,
            "text": text_parser.text(),
            "media": media,
            "unavailable_media": 1 if not media and _UNSUPPORTED_RE.search(block) else 0,
            "link": post_url(channel, post_id),
            "author_name": title,
            "forward_type": forward_type,
            "forward_name": forward_name,
        })

    posts.sort(key=lambda p: int(p["id"]))
    return title, posts[-MAX_POSTS_PER_FETCH:]

async def fetch_posts(channel, session=None, retries=1, retry_delay=PREVIEW_RETRY_DELAY):
    """``(title, posts)`` for a public Telegram channel, read from its web
    preview. Raises `FeedError` when the channel can't be read."""
    url = PREVIEW_URL.format(channel=channel)
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
                        return parse_preview(await resp.text(), channel)
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
