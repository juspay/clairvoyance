"""Journey view (V01, A12): query builder param binding and row decoding
(call arm only). Owned by record — column shape matches canon's
crm.journey_event 12-column contract."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.crm.record.db.decoder import decode_journey_card
from app.crm.record.db.queries import get_customer_journey_query


def test_first_page_has_no_cursor_predicate() -> None:
    sql, params = get_customer_journey_query("merchant-1", "cust-777", 50)
    assert params == ["merchant-1", "cust-777", 50]
    assert "crm_journey_event" in sql
    assert "$1" in sql and "$2" in sql and "$3" in sql
    assert "(started_at, id) <" not in sql
    assert "LIMIT $3" in sql


def test_cursor_page_binds_keyset_positionally() -> None:
    before = datetime(2026, 8, 24, tzinfo=timezone.utc)
    sql, params = get_customer_journey_query(
        "merchant-1", "cust-777", 50, before_started_at=before, before_id="evt-9"
    )
    assert params == ["merchant-1", "cust-777", before, "evt-9", 50]
    assert "(started_at, id) < ($3, $4)" in sql
    assert "LIMIT $5" in sql
    assert "OFFSET" not in sql


def test_cursor_ignored_without_both_halves() -> None:
    sql, params = get_customer_journey_query(
        "merchant-1", "cust-777", 50, before_started_at=datetime.now(timezone.utc)
    )
    assert params == ["merchant-1", "cust-777", 50]
    assert "(started_at, id) <" not in sql


def test_decode_call_row() -> None:
    customer_id = uuid4()
    row = {
        "id": "call-123",
        "merchant_id": "merchant-1",
        "customer_id": customer_id,
        "channel": "call",
        "direction": "outbound",
        "handled_by": None,
        "started_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 8, 24, 0, 2, 40, tzinfo=timezone.utc),
        "outcome": "CONFIRM",
        "recording_ref": "https://recordings.example/call-123.mp3",
        "transcript_ref": None,
        "source_kind": "call",
    }
    card = decode_journey_card(row)
    assert card.id == "call-123"
    assert card.customer_id == customer_id
    assert card.channel == "call"
    assert card.direction == "outbound"
    assert card.handled_by is None
    assert card.outcome == "CONFIRM"
    assert card.recording_ref == "https://recordings.example/call-123.mp3"
    assert card.transcript_ref is None
    assert card.source_kind == "call"


def test_migration_matches_the_view_contract() -> None:
    # Tripwire (the 048 pattern, tests/crm/test_suppression.py): if the
    # view's exclusion filter or direction normalization drifts, this
    # fails and forces a look at both sides together.
    sql = Path("app/database/migrations/055_create_crm_journey_view.sql").read_text()
    assert "WHERE customer_id IS NOT NULL" in sql
    assert "LOWER(call_direction)" in sql
