"""Executable inequalities between dials that constrain each other.

rules/03 §contracts: two dials whose relationship is load-bearing ship an
assertion against the LIVE config module, not a sentence in a comment. A
comment is read by whoever changes the dial it sits above — which is never
the person who changes the other one.
"""

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
