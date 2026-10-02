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


def test_the_llm_call_cache_outsizes_a_campaigns_distinct_values() -> None:
    """A cache smaller than its working set is a cache that never hits.

    llm_call memoises on (prompt, value), so its working set is the number
    of DISTINCT fact values a live campaign carries. Measured on flipkart,
    2026-09-29: 11,354 distinct product_name, 5,055 brand, 2,394
    sub_category — 18,803 values over 77,080 runs. At 50 slots every entry
    was evicted seconds before its ~7 reuses arrived, so the hit rate was
    ~0 and every lead paid the full model round trip (p50 250ms, measured).

    The bound is the working set with room for a campaign several times
    larger; the cost of holding it is ~400 bytes a value against a 4Gi
    request.
    """
    without = {k: v for k, v in os.environ.items() if k != "LLM_CALL_CACHE_SIZE"}
    with mock.patch.dict(os.environ, without, clear=True):
        assert importlib.reload(static).LLM_CALL_CACHE_SIZE >= 20_000
    importlib.reload(static)


def test_the_llm_call_cache_is_tunable_without_a_deploy() -> None:
    """A dial nobody can reach is a dial nobody revisits: the 50 above
    survived into a 554-leads-per-minute workload because changing it meant
    a code change. Every other worker dial reads its environment."""
    with mock.patch.dict(os.environ, {"LLM_CALL_CACHE_SIZE": "123"}):
        assert importlib.reload(static).LLM_CALL_CACHE_SIZE == 123
    importlib.reload(static)
