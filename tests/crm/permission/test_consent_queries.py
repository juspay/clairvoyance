"""Query builders for the consent tables: param binding and SQL shape."""

from datetime import datetime, timezone

from app.crm.permission.db.queries import (
    CRM_CONSENT_EVENT_TABLE,
    insert_consent_event_query,
    select_purpose_scope_for_update_query,
    upsert_consent_state_query,
)
from app.crm.permission.schemas import ConsentChannel, PurposeKey

MOMENT = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_ledger_insert_binds_every_value_and_returns_the_row() -> None:
    sql, params = insert_consent_event_query(
        "m_123",
        "c_777",
        "+919812340000",
        "GRANT",
        "whatsapp",
        "marketing.promotional",
        MOMENT,
        "evt_51",
    )
    assert f"INSERT INTO {CRM_CONSENT_EVENT_TABLE}" in sql
    assert "$8" in sql and "$9" not in sql
    assert "RETURNING" in sql
    assert params == [
        "m_123",
        "c_777",
        "+919812340000",
        "GRANT",
        "whatsapp",
        "marketing.promotional",
        MOMENT,
        "evt_51",
    ]


def test_the_time_is_always_bound_never_left_to_the_database() -> None:
    """The column DEFAULT cannot fire — the insert always names occurred_at —
    so the caller resolves the time. That is also what keeps one write on one
    clock."""
    sql, params = insert_consent_event_query(
        "m_123", "c_777", "+91", "IMPORT", "whatsapp", "marketing", MOMENT, None
    )
    assert "COALESCE" not in sql and "now()" not in sql
    assert params[6] == MOMENT


def test_scope_read_locks_both_directions_of_the_tree() -> None:
    """Down because a withdrawal cascades; up because a rule above this
    purpose governs it."""
    sql, params = select_purpose_scope_for_update_query(
        "m_123",
        "c_777",
        "whatsapp",
        "marketing.promotional",
        ["marketing", "marketing.promotional"],
    )
    assert "FOR UPDATE" in sql
    assert "purpose_key = ANY($4::text[]) OR starts_with(purpose_key, $5)" in sql
    assert "channel = $3" in sql
    assert params[3] == ["marketing", "marketing.promotional"]
    assert params[4] == "marketing.promotional."


def test_the_prefix_match_is_exact_not_a_like_pattern() -> None:
    """`transactional.order_update` contains an underscore, which LIKE reads
    as a single-character wildcard."""
    sql, _ = select_purpose_scope_for_update_query(
        "m_123", "c_777", "whatsapp", "transactional.order_update", ["transactional"]
    )
    assert "starts_with" in sql and "LIKE" not in sql


def test_the_scope_read_orders_its_locks() -> None:
    """Lock order belongs here — by the time the writes are sorted the rows
    are already locked."""
    sql, _ = select_purpose_scope_for_update_query(
        "m_123", "c_777", "whatsapp", "marketing", ["marketing"]
    )
    assert sql.index("ORDER BY purpose_key") < sql.index("FOR UPDATE")


def test_state_upsert_targets_the_four_column_key() -> None:
    sql, params = upsert_consent_state_query(
        "m_123", "c_777", "whatsapp", "marketing", "withdrawn", MOMENT, "evt_1"
    )
    assert "ON CONFLICT (merchant_id, customer_id, channel, purpose_key)" in sql
    assert "last_event_id = EXCLUDED.last_event_id" in sql
    assert params[4] == "withdrawn"


def test_the_builders_survive_enum_members_not_just_strings() -> None:
    """These are str Enums, so an f-string renders 'PurposeKey.MARKETING', not
    'marketing'. The prefix and the lock key are built with + and join for
    exactly that reason.

    This is a regression test, not a hypothetical: the trap was fixed in
    covers(), documented there, and then reintroduced here. A caller that
    forgets .value would otherwise get a prefix matching zero rows — so a
    withdrawal would stop cascading, silently, with no error anywhere.
    """
    _, params = select_purpose_scope_for_update_query(
        "m_123",
        "c_777",
        ConsentChannel.WHATSAPP,
        PurposeKey.MARKETING,
        [PurposeKey.MARKETING],
    )
    assert params[4] == "marketing."
