"""Shared test dials for tests/crm.

CRM_WEBHOOK_TEST_DSN lives here, not in app config: it is a test dial, and
the app's static config surface is for the app (review ruling, 2 Sep 2026).
Unset means the DB-backed integration tests skip.
"""

import os

CRM_WEBHOOK_TEST_DSN = os.environ.get("CRM_WEBHOOK_TEST_DSN") or None


import pytest  # noqa: E402  (the DSN above is a plain constant, not a fixture)

from tests.crm import doubles  # noqa: E402


@pytest.fixture
def graph_stub(monkeypatch: pytest.MonkeyPatch):
    """The shared Meta Graph transport, canned: ``graph_stub(handler)`` returns
    the list of requests the code made."""

    def _install(handler):
        return doubles.stub_graph(monkeypatch, handler)

    return _install


@pytest.fixture(autouse=True)
def _no_parked_receipt_io(monkeypatch: pytest.MonkeyPatch):
    """Both send paths drain parked receipts after recording an outcome, and
    the dispatcher's claim sweeps them — real database work that a unit test
    of dispatch or session must never reach by accident. No-ops here; a test
    that watches the drain patches over them (monkeypatch runs in order)."""
    from app.crm.connectivity import dispatch, session

    async def _drain(merchant_id, provider_message_id) -> None:
        return None

    async def _sweep() -> int:
        return 0

    monkeypatch.setattr(dispatch, "apply_parked", _drain)
    monkeypatch.setattr(session, "apply_parked", _drain)
    monkeypatch.setattr(dispatch, "sweep_parked", _sweep)
