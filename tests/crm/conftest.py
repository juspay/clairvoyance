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
