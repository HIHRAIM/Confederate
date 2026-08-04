"""Feed readers: one module per followed-source kind.

Every module here exposes the same three functions, which is the whole
contract `discord_bot/feeds.py: feed_module()` relies on:

    normalize_source(raw)  -> canonical source name, or None if unusable
    source_url(source)     -> link to the source for humans
    fetch_posts(source, session=None) -> (title, posts) or raises FeedError

with posts as the dicts described in each module's docstring (`id` must be
a string of an integer that orders the posts — the relay's read position is
``int(id)``).

A new kind (the planned wiki reader, for instance) is added by writing a
module with this interface, registering it in feed_module() and FEED_KINDS
(discord_bot/feeds.py), and giving it a poll interval in main.py.
Modules here only fetch and parse — no database, no chat delivery.
"""
