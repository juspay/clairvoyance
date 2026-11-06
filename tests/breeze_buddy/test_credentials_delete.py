"""delete_credential: three outcomes, three honest answers.

The bool can carry exactly one fact — "the DELETE ran and matched no row",
which the handler reports as 404. The other two outcomes must leave by their
own doors: in-use raises CredentialInUseError (the handler's 409), and an
infrastructure error re-raises (the handler's 500). Folding the last one into
False reported a DB outage as "not found" for a credential the same handler
had fetched two lines earlier.
"""

import asyncpg
import pytest

from app.database.accessor.breeze_buddy import credentials as credentials_module
from app.database.accessor.breeze_buddy.credentials import (
    CredentialInUseError,
    delete_credential,
)


def _query_runs(monkeypatch, runner) -> None:
    """Patch the accessor's query runner with a stand-in."""
    monkeypatch.setattr(credentials_module, "run_parameterized_query", runner)


async def test_a_deleted_row_returns_true(monkeypatch) -> None:
    """A deleted row returns true."""

    async def runner(query, values):
        """Test double: stands in for run_parameterized_query."""
        return [{"id": "cred-1"}]

    _query_runs(monkeypatch, runner)
    assert await delete_credential("cred-1") is True


async def test_false_means_only_that_no_row_matched(monkeypatch) -> None:
    """False means only that no row matched."""

    async def runner(query, values):
        """Test double: stands in for run_parameterized_query."""
        return []

    _query_runs(monkeypatch, runner)
    assert await delete_credential("cred-1") is False


async def test_in_use_raises_the_domain_error_for_the_handlers_409(
    monkeypatch,
) -> None:
    """In use raises the domain error for the handlers 409.

    RESTRICT refused the DELETE: the row exists and is wired to a connector
    installation. "Not found" would send the operator after the wrong
    problem, so this must not travel through the bool — and it leaves as
    CredentialInUseError, not the driver's class: the accessor boundary is
    where asyncpg is translated, so the handler answers 409 without
    importing the driver.
    """

    async def runner(query, values):
        """Test double: stands in for run_parameterized_query."""
        raise asyncpg.ForeignKeyViolationError("violates foreign key constraint")

    _query_runs(monkeypatch, runner)
    with pytest.raises(CredentialInUseError) as excinfo:
        await delete_credential("cred-1")
    # The driver's detail is chained, not discarded — a 409 investigation
    # can still see which constraint refused.
    assert isinstance(excinfo.value.__cause__, asyncpg.ForeignKeyViolationError)


async def test_an_infrastructure_error_is_not_reported_as_not_found(
    monkeypatch,
) -> None:
    """An infrastructure error is not reported as not found."""

    # The half-fixed case the review caught: the FK path re-raised, but a
    # pool timeout still returned False — which the handler translated into
    # 404 for a credential that plainly existed. An outage must surface as
    # an incident, not as a phantom deletion.
    async def runner(query, values):
        """Test double: stands in for run_parameterized_query."""
        raise ConnectionError("pool down")

    _query_runs(monkeypatch, runner)
    with pytest.raises(ConnectionError):
        await delete_credential("cred-1")
