"""Where the bundled avatar pictures (src/assets/) currently live on Discord.

One row per file name: the sha256 of the bytes that were uploaded, the message
the upload sits in, and the last CDN link read off it. That row is what makes
the pictures survive a deleted host message and a cleared channel — the
uploader in discord_bot/feeds.py compares the hash, uploads again whenever the
row is missing, out of date or its message is gone, and writes the new
coordinates back here.

Not this module's zone: the uploading itself, the in-process URL cache and the
choice of host channel (discord_bot/feeds.py).
"""
import time

from db import conn, cur

def get_avatar_asset(name):
    """The stored upload of one bundled avatar, or None if it has never been
    uploaded from this database."""
    return cur.execute(
        "SELECT * FROM avatar_assets WHERE name=?", (name,)
    ).fetchone()

def save_avatar_asset(name, sha256, channel_id, message_id, url):
    """Record where a freshly uploaded asset landed, replacing whatever was
    known about it before — an upload only happens when the old coordinates
    turned out to be unusable or the file itself changed."""
    cur.execute(
        "INSERT INTO avatar_assets (name, sha256, channel_id, message_id, url, url_ts)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET sha256=excluded.sha256,"
        " channel_id=excluded.channel_id, message_id=excluded.message_id,"
        " url=excluded.url, url_ts=excluded.url_ts",
        (name, sha256, str(channel_id), str(message_id), url, int(time.time()))
    )
    conn.commit()

def set_avatar_asset_url(name, url):
    """Store the signed link just read off an asset's host message, with the
    moment it was read: a link is only good for about a day, and the timestamp
    is what tells the next start-up whether it can be reused as it is."""
    cur.execute(
        "UPDATE avatar_assets SET url=?, url_ts=? WHERE name=?",
        (url, int(time.time()), name)
    )
    conn.commit()
