"""Executable inequalities between dials that constrain each other.

rules/03 §contracts: two dials whose relationship is load-bearing ship an
assertion against the LIVE config module, not a sentence in a comment. A
comment is read by whoever changes the dial it sits above — which is never
the person who changes the other one.
"""

import importlib
import os
from unittest import mock

from app.core.config import static


def test_an_action_cannot_outlive_the_lease_that_authorises_it() -> None:
    """The walker claims a run under a lease (canon T20) and performs the
    action square inside it. An action allowed to run longer than the lease
    would let a SECOND worker claim the same run while the first is still
    mid-write — the action performed twice, by two workers each believing
    they hold it. The (run, node) idempotency key makes that survivable at
    the destination; it does not make it correct here.

    Strictly less, with room: equal leaves no margin for the claim, the
    decode and the advance that bracket the call inside the same lease.
    """
    assert static.CRM_ACTION_TIMEOUT_SECONDS < static.CRM_WALKER_LEASE_SECONDS


def test_a_send_cannot_outlive_the_claim_that_marks_it_stale() -> None:
    """The same law on the send path, against the dial that plays the
    lease's part there: a row claimed by the dispatcher is re-claimable once
    CRM_DISPATCH_STALE_MINUTES have passed, so a provider call allowed to
    run longer would be re-sent underneath itself."""
    assert static.CRM_MESSAGE_SEND_TIMEOUT_SECONDS < (
        static.CRM_DISPATCH_STALE_MINUTES * 60
    )


def test_the_shared_llm_call_store_ships_off() -> None:
    """The shared llm_call store is off until the env switches it on."""
    without = {k: v for k, v in os.environ.items() if k != "LLM_CALL_L2_ENABLED"}
    with mock.patch.dict(os.environ, without, clear=True):
        assert importlib.reload(static).LLM_CALL_L2_ENABLED is False
    importlib.reload(static)
