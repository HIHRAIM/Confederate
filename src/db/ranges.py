"""The one integer space bridge numbers are cut out of, and the only place
its boundaries are written down.

A bridge has no stored category. `bridges` is literally `id INTEGER PRIMARY
KEY`, and what kind of bridge a number is comes from *where* the number sits.
That is not a shortcut, it is what makes the scheme survive: a bridge row is
deleted together with its last chat (db/bridges.py: remove_chat_from_bridge),
so a column saying "this one was a sponsor's" would die with the row it
described. The number outlives the bridge; a column would not.

Four regions, three of them served by their own allocator:

    1 … 49999          ordinary bridges. Holes are REUSED — `/atb new` asks
                       db/bridges.py: next_free_bridge_id for the lowest free
                       number, because an ordinary bridge that empties has
                       nothing to remember and its number should come back
                       into circulation.
    50000 … 99999      sponsor bridges (db/sponsors.py:
                       claim_sponsor_bridge_id). Holes are NOT reused: the
                       row in `sponsor_bridges` saying who owns the number
                       outlives the bridge itself, so that a sponsor whose
                       last chat leaves can attach a chat to the same number
                       later and get their own bridge back. Handing that
                       number to somebody else in the meantime would give
                       them a bridge with a stranger's owner.
    100000 … 999999    appeal bridges (db/appeals.py: next_appeal_bridge_id),
                       max+1, short-lived, holes not worth reusing.
    1000000 and up     inbox conversations (db/inbox.py:
                       claim_inbox_bridge_id), same shape.

This module imports nothing — not even `db` — on purpose: db/bridges.py,
db/appeals.py, db/inbox.py and db/sponsors.py all need the boundaries, and
before this module existed they got them from each other in a chain
(bridges → appeals → inbox) that a fourth region would have made genuinely
fragile. Everything that has to know where a number belongs asks
`bridge_kind`; nothing compares against a floor by hand.
"""

ORDINARY_BRIDGE_ID_FLOOR = 1
SPONSOR_BRIDGE_ID_FLOOR = 50000
APPEAL_BRIDGE_ID_FLOOR = 100000
INBOX_BRIDGE_ID_FLOOR = 1000000

BRIDGE_RANGES = (
    ("ordinary", ORDINARY_BRIDGE_ID_FLOOR, SPONSOR_BRIDGE_ID_FLOOR),
    ("sponsor", SPONSOR_BRIDGE_ID_FLOOR, APPEAL_BRIDGE_ID_FLOOR),
    ("appeal", APPEAL_BRIDGE_ID_FLOOR, INBOX_BRIDGE_ID_FLOOR),
    ("inbox", INBOX_BRIDGE_ID_FLOOR, None),
)

RESERVED_BRIDGE_KINDS = ("appeal", "inbox")

def bridge_kind(bridge_id):
    """Which of the four regions a bridge number belongs to: 'ordinary',
    'sponsor', 'appeal', 'inbox' — or None when it is not a usable number at
    all (not an integer, or below the first floor).

    The single test every range check in the codebase goes through. Cheap
    enough to call on an inbound message: it touches no table, because the
    number itself is the answer."""
    try:
        value = int(bridge_id)
    except (TypeError, ValueError):
        return None
    for kind, floor, ceiling in BRIDGE_RANGES:
        if value >= floor and (ceiling is None or value < ceiling):
            return kind
    return None

def bridge_range(kind):
    """``(floor, ceiling)`` of one region, the ceiling exclusive and None for
    the open-ended inbox range. What the three max+1 allocators bound
    themselves with, so that none of them can read the newest number of the
    region above as its own."""
    for name, floor, ceiling in BRIDGE_RANGES:
        if name == kind:
            return floor, ceiling
    raise KeyError(kind)

def is_reserved_bridge_id(bridge_id):
    """Whether a number belongs to a region no one may name by hand.

    Appeal and inbox numbers are handed out with no human in the loop, to
    bridges that pair one person with one team; `/atb 100000` typed by
    anybody — a Bot Admin included — would attach a chat into somebody's
    appeal or somebody's private conversation. Those two regions are reachable
    through their own allocators and through nothing else."""
    return bridge_kind(bridge_id) in RESERVED_BRIDGE_KINDS
