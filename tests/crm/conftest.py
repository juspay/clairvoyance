"""Shared test dials for tests/crm.

CRM_WEBHOOK_TEST_DSN lives here, not in app config: it is a test dial, and
the app's static config surface is for the app (review ruling, 2 Sep 2026).
Unset means the DB-backed integration tests skip.
"""

import os

CRM_WEBHOOK_TEST_DSN = os.environ.get("CRM_WEBHOOK_TEST_DSN") or None


import pytest  # noqa: E402  (the DSN above is a plain constant, not a fixture)

import app.crm.identity.db.accessor as identity_accessor  # noqa: E402
from tests.crm import doubles  # noqa: E402


@pytest.fixture(autouse=True)
def _every_customer_is_one_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests run without a database, and identity's two merge reads —
    which records are one person, which records were merged — are database
    reads. Here nobody was ever merged: a customer is the one record she is
    (identity.person falls back to [customer_id]), and a merchant has no
    merged records. A test about merges installs its own answers over these."""

    async def no_records(merchant_id: str, customer_id: str) -> list:
        return []

    async def no_merges(merchant_id: str) -> dict:
        return {}

    monkeypatch.setattr(identity_accessor, "person_records", no_records)
    monkeypatch.setattr(identity_accessor, "merged_pairs", no_merges)


@pytest.fixture
def graph_stub(monkeypatch: pytest.MonkeyPatch):
    """The shared Meta Graph transport, canned: ``graph_stub(handler)`` returns
    the list of requests the code made."""

    def _install(handler):
        return doubles.stub_graph(monkeypatch, handler)

    return _install
