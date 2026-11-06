"""Cross-module lock keys (rollout phase 14, ADR 0023 §6).

A template is CONNECTIVITY's row, but the runs that will send it are
OUTREACH's — and the two modules may not share a transaction or import
each other's tables. What they can share is a NUMBER: the key of a
Postgres transaction-scoped advisory lock, computed here from the
template's natural identity so both sides derive the same one without
either knowing the other's schema.

The lock is reader/writer, exactly as the race is shaped. Every path
that PINS a document to a run (enrol, publish, migrate-forward) takes the
key SHARED for each template that document sends — shared holders never
block one another, so a hot plan's enrols stay concurrent. Retirement
takes the same key EXCLUSIVE around its "who would still send this?"
count and its withdrawal — it waits for in-flight pinners to commit (so
the count sees their rows), and pinners that arrive while it holds the
lock wait for its verdict. No cycle is possible: a pinner only ever waits
for a retirer, and a retirer waits for nothing but its own reads.

Each module executes the lock through its own accessor and query builder
(SQL stays in db/queries); only the key function lives here.
"""

import hashlib


def template_lock_key(merchant_id: str, channel: str, name: str) -> int:
    """PURE: a stable signed 64-bit key for one (merchant, channel,
    template name). hashlib, never hash(): Python salts the latter per
    process, and two pods must agree. Signed because pg_advisory_xact_lock
    takes a bigint."""
    digest = hashlib.blake2b(
        f"template\x00{merchant_id}\x00{channel}\x00{name}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)
