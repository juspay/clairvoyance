"""Regression tests for the PR-review fixes on the Google Sheets connector.

The detect_change tests are the important ones: a transient Drive failure
must NOT requeue the document, because a requeue flips it to PENDING and
retrieval serves READY documents only — i.e. a Google incident would blank
out otherwise-healthy KB content for every attached agent.
"""

import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from app.schemas.breeze_buddy.knowledge_base import KbDocument
from app.services.knowledge_base.connectors.google_sheets import GoogleSheetsConnector

_SYNCED_AT = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)


def _doc() -> KbDocument:
    return cast(
        KbDocument,
        SimpleNamespace(
            id="doc-1",
            source_ref={"spreadsheet_id": "sheet-abc"},
            synced_at=_SYNCED_AT,
        ),
    )


async def test_transient_failure_reports_unchanged_keeping_last_good_ingestion():
    """429/5xx/network -> RuntimeError -> must NOT requeue (would blank the KB)."""
    with patch(
        "app.services.knowledge_base.connectors.google_sheets._api_get",
        new=AsyncMock(side_effect=RuntimeError("rate limit hit (429)")),
    ):
        changed = await GoogleSheetsConnector().detect_change(_doc())

    assert changed is False


async def test_user_fixable_failure_reports_changed_so_the_reread_records_it():
    """403/404 -> ValueError -> requeue, so the error lands on the document row."""
    with patch(
        "app.services.knowledge_base.connectors.google_sheets._api_get",
        new=AsyncMock(side_effect=ValueError("Access denied. Share the sheet...")),
    ):
        changed = await GoogleSheetsConnector().detect_change(_doc())

    assert changed is True


async def test_modified_after_sync_reports_changed():
    with patch(
        "app.services.knowledge_base.connectors.google_sheets._api_get",
        new=AsyncMock(return_value={"modifiedTime": "2026-07-02T00:00:00Z"}),
    ):
        changed = await GoogleSheetsConnector().detect_change(_doc())

    assert changed is True


async def test_modified_before_sync_reports_unchanged():
    with patch(
        "app.services.knowledge_base.connectors.google_sheets._api_get",
        new=AsyncMock(return_value={"modifiedTime": "2026-06-30T00:00:00Z"}),
    ):
        changed = await GoogleSheetsConnector().detect_change(_doc())

    assert changed is False
