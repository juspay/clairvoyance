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
def _no_receipt_read_back(monkeypatch: pytest.MonkeyPatch):
    """Both send paths read a message's filed receipts back after recording
    its outcome — real database work a unit test of dispatch or session must
    never reach by accident. A no-op here; a test that watches it patches
    over it (monkeypatch runs in order)."""
    from app.crm.connectivity.messaging.conversations import session
    from app.crm.connectivity.messaging.outbound import dispatch

    async def _read_back(merchant_id, channel, provider_message_id) -> None:
        return None

    monkeypatch.setattr(dispatch, "apply_filed_receipts", _read_back)
    monkeypatch.setattr(session, "apply_filed_receipts", _read_back)
