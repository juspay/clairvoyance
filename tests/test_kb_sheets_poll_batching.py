"""Regression test for a review finding (murdore, PR #896): the sheets
poller's probe pacing was 4x over its own documented Drive quota, and the
due-poll query had no batch bound, so a large backlog of connected sheets
would be probed serially in a single tick with unbounded wall-clock and
quota burn.
"""

import asyncio

from app.core.config.dynamic import KB_SHEETS_PROBE_SPACING_SECONDS
from app.database.queries.breeze_buddy.knowledge_base import (
    get_sheet_documents_due_for_poll_query,
)


def test_probe_spacing_default_matches_documented_drive_quota():
    """Quota is 60 read req/min/user; spacing must not exceed that rate."""
    spacing = asyncio.run(KB_SHEETS_PROBE_SPACING_SECONDS())
    requests_per_minute = 60 / spacing
    assert requests_per_minute <= 60


def test_due_for_poll_query_is_bounded_by_batch_size():
    text, values = get_sheet_documents_due_for_poll_query(300, 50)

    assert "LIMIT" in text
    assert values == [300, 50]


def test_due_for_poll_query_orders_oldest_synced_first():
    """Bounding by LIMIT only drains fairly if the same stale tail isn't
    starved every tick -- oldest-synced-first guarantees that."""
    text, _ = get_sheet_documents_due_for_poll_query(300, 50)

    assert 'ORDER BY "synced_at" NULLS FIRST' in text
    assert text.index('ORDER BY "synced_at"') < text.index("LIMIT")
