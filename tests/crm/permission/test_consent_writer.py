"""record_consent()'s boundary — order and stapling, with a faked connection.

What is under test: the lock comes before the read, the read reaches both
directions of the tree, and every state row points at the ledger row written
in the same transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from uuid import UUID

import pytest

from app.crm.permission import consent
from app.crm.permission.consent import ConsentPolicy
from app.crm.permission.schemas import (
    ConsentEventIn,
    ConsentEventType,
    ConsentReceipt,
    ConsentStatus,
)

Txn = Callable[..., Awaitable[Any]]

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
MERCHANT = "m_123"
CUSTOMER = UUID("00000000-0000-0000-0000-000000000777")
LEDGER_ID = UUID("00000000-0000-0000-0000-0000000abcde")
POLICY = ConsentPolicy(
    marketing_grant_days=5, reask_embargo_days=45, pending_confirm_hours=6
)


def _event(event_type: ConsentEventType, purpose: str, **kw: Any) -> ConsentEventIn:
    body: Dict[str, Any] = {
        "merchant_id": MERCHANT,
        "customer_id": CUSTOMER,
        "address": "+919812340000",
        "event_type": event_type,
        "channel": "whatsapp",
        "purpose_key": purpose,
        "occurred_at": NOW,
    }
    body.update(kw)
    return ConsentEventIn(**body)


def _state_row(purpose: str, status: str) -> Dict[str, Any]:
    return {
        "merchant_id": MERCHANT,
        "customer_id": CUSTOMER,
        "channel": "whatsapp",
        "purpose_key": purpose,
        "status": status,
        "expires_at": None,
        "last_event_id": LEDGER_ID,
    }


class FakeConn:
    """Routes on the SQL the builders produce, so the real query-selection
    path is exercised rather than a hand-wired accessor."""

    def __init__(self, scope: Sequence[Dict[str, Any]] = ()) -> None:
        self.scope = list(scope)
        self.calls: List[str] = []
        self.upserted: List[Dict[str, Any]] = []
        self.scope_params: List[Any] = []
        self.event_params: List[Any] = []

    async def fetch(self, query: str, *values: Any) -> List[Dict[str, Any]]:
        self.calls.append("select")
        assert "FOR UPDATE" in query
        self.scope_params = list(values)
        return self.scope

    async def fetchrow(self, query: str, *values: Any) -> Optional[Dict[str, Any]]:
        if "crm_consent_event" in query:
            self.calls.append("insert")
            self.event_params = list(values)
            # The real INSERT binds $7 into a NOT NULL column with no COALESCE,
            # so a None here is a constraint violation. Modelling a fallback
            # the SQL does not have is how a test proves the fake, not the code.
            assert values[6] is not None
            return {
                "id": LEDGER_ID,
                "merchant_id": values[0],
                "customer_id": CUSTOMER,
                "address": values[2],
                "event_type": values[3],
                "channel": values[4],
                "purpose_key": values[5],
                "occurred_at": values[6],
                "artifact_ref": values[7],
            }
        self.calls.append("upsert")
        row = {
            "merchant_id": values[0],
            "customer_id": CUSTOMER,
            "channel": values[2],
            "purpose_key": values[3],
            "status": values[4],
            "expires_at": values[5],
            "last_event_id": UUID(values[6]),
        }
        self.upserted.append(row)
        return row


async def _record(conn: FakeConn, event: ConsentEventIn) -> ConsentReceipt:
    """FakeConn duck-types the handle; widening here keeps every call site free
    of a repeated ignore."""
    txn: Any = conn
    return await consent._record_consent_in_txn(txn, event, POLICY)


async def _fixed_policy() -> ConsentPolicy:
    return POLICY


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


async def test_the_scope_is_read_before_anything_is_written(conn: FakeConn) -> None:
    """Order is the guarantee: the scope is read under FOR UPDATE, the ledger
    row is appended, and only then does any state row move."""
    await _record(conn, _event(ConsentEventType.GRANT, "marketing"))
    assert conn.calls == ["select", "insert", "upsert"]


async def test_the_locked_scope_reaches_upward_as_well_as_down(
    conn: FakeConn,
) -> None:
    """Reading only downward is how a bulk import walked past a parent's
    withdrawal."""
    await _record(
        conn, _event(ConsentEventType.IMPORT, "marketing.promotional.winback")
    )
    assert conn.scope_params[3] == [
        "marketing",
        "marketing.promotional",
        "marketing.promotional.winback",
    ]
    assert conn.scope_params[4] == "marketing.promotional.winback."


async def test_every_state_row_points_at_the_event_row_just_written(
    conn: FakeConn,
) -> None:
    """The state asserts, the event row proves — last_event_id is the thread."""
    await _record(conn, _event(ConsentEventType.GRANT, "marketing"))
    assert conn.upserted[0]["last_event_id"] == LEDGER_ID


async def test_a_withdrawal_writes_one_state_row_per_governed_purpose() -> None:
    conn = FakeConn(
        [
            _state_row("marketing", "granted"),
            _state_row("marketing.promotional", "granted"),
        ]
    )
    receipt = await _record(conn, _event(ConsentEventType.WITHDRAW, "marketing"))
    assert len(receipt.states) == 2
    assert all(s.status is ConsentStatus.WITHDRAWN for s in receipt.states)


async def test_a_refused_event_still_leaves_a_ledger_row() -> None:
    """The attempt is evidence: the record keeps what the importer tried."""
    conn = FakeConn([_state_row("marketing.promotional", "withdrawn")])
    receipt = await _record(
        conn, _event(ConsentEventType.IMPORT, "marketing.promotional")
    )
    assert conn.calls.count("insert") == 1
    assert "upsert" not in conn.calls
    assert receipt.states == []


async def test_one_write_uses_one_clock(conn: FakeConn) -> None:
    """A caller who omits the time still binds one, and the expiry derives
    from the same instant — asserted on the BOUND PARAMETER, since a receipt
    assertion would only prove what the fake handed back."""
    undated = _event(ConsentEventType.GRANT, "marketing", occurred_at=None)
    receipt = await _record(conn, undated)

    assert conn.event_params[6] is not None
    assert conn.event_params[6].tzinfo is not None
    window = conn.upserted[0]["expires_at"] - receipt.event.occurred_at
    assert window == timedelta(days=POLICY.marketing_grant_days)


async def test_a_cascade_stamps_one_embargo_date_on_every_row() -> None:
    """The policy is read once, not once per row."""
    conn = FakeConn(
        [
            _state_row("marketing", "granted"),
            _state_row("marketing.promotional", "granted"),
            _state_row("marketing.promotional.winback", "granted"),
        ]
    )
    receipt = await _record(conn, _event(ConsentEventType.WITHDRAW, "marketing"))
    assert len({s.expires_at for s in receipt.states}) == 1


async def test_the_windows_are_read_before_the_boundary_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flag lookup is network I/O; doing it inside the transaction would
    hold row locks across a Redis round-trip."""
    order: List[str] = []

    async def watched_policy() -> ConsentPolicy:
        order.append("flags")
        return POLICY

    async def watched_txn(fn: Txn, *args: Any, **kwargs: Any) -> Any:
        order.append("txn")
        return await fn(FakeConn(), *args, **kwargs)

    monkeypatch.setattr(consent, "load_policy", watched_policy)
    monkeypatch.setattr(consent, "atomically", watched_txn)

    await consent.record_consent(_event(ConsentEventType.GRANT, "marketing"))
    assert order == ["flags", "txn"]


async def test_record_consent_runs_both_stores_in_one_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConn()

    async def one_txn(fn: Txn, *args: Any, **kwargs: Any) -> Any:
        return await fn(conn, *args, **kwargs)

    monkeypatch.setattr(consent, "atomically", one_txn)
    monkeypatch.setattr(consent, "load_policy", _fixed_policy)

    receipt = await consent.record_consent(_event(ConsentEventType.GRANT, "marketing"))

    assert conn.calls == ["select", "insert", "upsert"]
    assert receipt.event.id == LEDGER_ID
    assert len(receipt.states) == 1


async def test_the_refusal_log_names_values_not_enum_reprs(
    conn: FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator greps this line for 'whatsapp', not for
    'ConsentChannel.WHATSAPP'. f-strings on a str Enum render the repr.

    The module uses loguru, which does not feed pytest's caplog, so the sink
    is replaced rather than captured.
    """
    lines: List[str] = []
    monkeypatch.setattr(consent.logger, "info", lambda msg: lines.append(msg))

    conn.scope = [_state_row("marketing", "withdrawn")]
    await _record(conn, _event(ConsentEventType.IMPORT, "marketing"))
    logged = "".join(lines)
    assert "(whatsapp/marketing)" in logged
    assert "ConsentChannel" not in logged and "PurposeKey" not in logged
