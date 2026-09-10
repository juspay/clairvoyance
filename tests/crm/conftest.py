"""Shared test dials for tests/crm.

CRM_WEBHOOK_TEST_DSN lives here, not in app config: it is a test dial, and
the app's static config surface is for the app (review ruling, 2 Sep 2026).
Unset means the DB-backed integration tests skip.
"""

import os

CRM_WEBHOOK_TEST_DSN = os.environ.get("CRM_WEBHOOK_TEST_DSN") or None


import pytest  # noqa: E402  (the DSN above is a plain constant, not a fixture)

from tests.crm import doubles  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_send_observer_slot(monkeypatch: pytest.MonkeyPatch):
    """The send-observer slot is process-global and worker_main fills it at
    IMPORT time — which any test importing worker_main does for the whole
    session. Left in place, the first dispatch test with a workflow-shaped
    message would run the REAL stamp against a live accessor, and the
    observer contract's swallow would keep the test green while logging DB
    errors. Every test starts with an empty slot and registers its own."""
    from app.crm.connectivity import send_observers

    monkeypatch.setattr(send_observers, "_OBSERVERS", [])


@pytest.fixture
def graph_stub(monkeypatch: pytest.MonkeyPatch):
    """The shared Meta Graph transport, canned: ``graph_stub(handler)`` returns
    the list of requests the code made."""

    def _install(handler):
        return doubles.stub_graph(monkeypatch, handler)

    return _install
