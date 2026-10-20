"""The accessor's door pin, unit-tested WITHOUT a database.

``get_installation_for_connector`` must hand the plan's ``door_name`` straight
to ``installation_for_connector_query`` — a plain string on the ``door_name``
column, never coerced to or validated against a UUID. A door_name is compared
as text (``door_name = $3``), so an odd value can only fail to MATCH, never
raise a driver DataError: the ``::uuid`` cast is gone, and with it the
malformed-pin guard that used to live here. The DB-backed selection itself
lives in test_installation_route_integration.py.

The DB pool and the query builder are both swapped for stubs: here we prove
THREADING (the name arrives verbatim, absent -> the newest-door branch), and
that None (no row) reads as None (no usable door).
"""

import asyncio
from typing import Any, Dict, List, Optional

import app.crm.connectivity.db.accessors.installation as accessor
from app.crm.connectivity.schemas.connector import ConnectorInstallation

_ROW: Dict[str, Any] = {
    "id": "00000000-0000-0000-0000-000000009001",
    "merchant_id": "m1",
    "connector_key": "merchant_http",
    "external_account_id": "payments",
    "door_name": "payments",
    "door_data": {"base_url": "https://pay.example.com/v1"},
    "display_label": "Payments API",
    "credential_id": None,
    "status": "healthy",
    "token_expires_at": None,
}


def test_pinned_door_name_is_passed_to_the_query_verbatim(monkeypatch) -> None:
    """A plan's pinned door_name reaches the SQL builder UNCHANGED, as plain
    text — not a UUID, not a URL — so the `door_name = $3` branch runs for the
    named door. This is the whole change of issue 3: the pin is a name and a
    base-URL edit is an in-place update on the same row, so the name keeps
    resolving."""
    seen: List[Optional[str]] = []

    def _query(merchant_id: str, connector_key: str, door_name=None):
        seen.append(door_name)
        return "SELECT 1", [merchant_id, connector_key, door_name]

    monkeypatch.setattr(accessor, "installation_for_connector_query", _query)
    monkeypatch.setattr(accessor, "crm_connection", _pool([dict(_ROW)]))

    door = asyncio.run(
        accessor.get_installation_for_connector(
            "m1", "merchant_http", door_name="payments"
        )
    )
    assert seen == ["payments"]
    assert isinstance(door, ConnectorInstallation)
    assert door.door_name == "payments"
    assert door.door_data["base_url"] == "https://pay.example.com/v1"


def test_absent_door_name_takes_the_newest_door_branch(monkeypatch) -> None:
    """No pinned name -> the connector's newest usable door branch; the value
    passed to the builder is None, unchanged behaviour."""
    seen: List[Optional[str]] = []

    def _query(merchant_id: str, connector_key: str, door_name=None):
        seen.append(door_name)
        return "SELECT 1", [merchant_id, connector_key, door_name]

    monkeypatch.setattr(accessor, "installation_for_connector_query", _query)
    monkeypatch.setattr(accessor, "crm_connection", _pool([dict(_ROW)]))

    door = asyncio.run(accessor.get_installation_for_connector("m1", "merchant_http"))
    assert seen == [None]
    assert door is not None and door.door_name == "payments"


def test_no_row_reads_as_no_usable_door(monkeypatch) -> None:
    """No matching row (a name nobody onboarded, a withdrawn door, a name from
    another tenant) — the query builder is reached, the fetch yields nothing,
    and None is the fail-closed answer the action root turns into a refusal."""
    monkeypatch.setattr(
        accessor,
        "installation_for_connector_query",
        lambda m, c, door_name=None: ("SELECT 1", [m, c, door_name]),
    )
    monkeypatch.setattr(accessor, "crm_connection", _pool([]))

    door = asyncio.run(
        accessor.get_installation_for_connector(
            "m1", "merchant_http", door_name="no-such-door"
        )
    )
    assert door is None


def _pool(rows: List[dict]):
    """A stand-in for crm_connection(): an async context manager yielding a
    conn whose fetchrow returns the first stubbed row, or None when empty."""

    class _Conn:
        async def fetchrow(self, *a, **k):
            return dict(rows[0]) if rows else None

    class _Pool:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a, **k):
            return None

    return _Pool
