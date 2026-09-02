"""Meta's error codes, split by the only question an adapter may ask: could
the same request plausibly succeed later?

An adapter CLASSIFIES, it never DECIDES — dispatch.plan_for_outcome alone
turns a classification into queued / failed / dead. Keeping the tables in
their own file is what lets the onboarding and template faces share the
throttle half of them without importing the send door.
"""

from typing import Optional

from app.crm.connectivity.providers.meta.graph import GRAPH_THROTTLE_CODES
from app.crm.connectivity.reasons import REASON_UNREADABLE
from app.crm.connectivity.schemas.message import SendOutcome

# Retryable — the provider is busy or pacing us, not refusing on the merits.
# The Graph-wide throttles arrive as HTTP 400, which the unknown-4xx default
# below would read as terminal: that would permanently fail every message
# queued during a throttle window instead of backing off. They are taken from
# meta/graph.py rather than restated, so one list serves both faces.
RETRYABLE_CODES = GRAPH_THROTTLE_CODES | {
    "130429",  # Cloud API throughput limit
    "131048",  # spam rate limit
    "131049",  # per-user engagement limit ("healthy ecosystem")
}

# Terminal — waiting changes nothing; retrying just collects the identical
# refusal three times.
TERMINAL_CODES = {
    "100",  # invalid parameter
    "131008",  # required parameter missing
    "131009",  # parameter value invalid
    "131026",  # undeliverable: recipient cannot receive WhatsApp messages
    "131047",  # 24-hour window closed — a template is the fix, not a retry
    "132000",  # template param count mismatch
    "132001",  # template does not exist
    "132005",  # rendered template too long
    "132007",  # template content policy violation
    "132012",  # template parameter format mismatch
    "132015",  # template paused
    "132016",  # template disabled
}

# Terminal, and a statement about the CONNECTION rather than this message:
# every queued message for that merchant is about to fail the same way. The
# send path does not act on them (that is channel-lifecycle work, not built
# yet) — they are named so that module has an exact signal to watch for on
# crm_message.reason instead of guessing which codes mean "re-authenticate".
CREDENTIAL_CODES = {
    "10",  # permission denied
    "190",  # invalid or expired access token
    "200",  # permissions error
    "133010",  # phone number not registered for Cloud API
}


def classify_failure(code: Optional[str], http_status: int) -> SendOutcome:
    """Meta's refusal -> a SendOutcome. Classification only, no policy.

    Both terminal classes behave identically here; they stay separate sets
    because they differ in what they say about the CONNECTION, which
    channel-lifecycle code reads off ``reason``.
    """
    if code in TERMINAL_CODES or code in CREDENTIAL_CODES:
        return SendOutcome(status="failed", reason=code)
    if code in RETRYABLE_CODES or http_status == 429:
        return SendOutcome(status="failed", reason=code or "429", retryable=True)
    # Unknown code: 5xx is Meta's problem and may pass, 4xx is ours and will
    # not — retrying an unknown 4xx spends attempts learning nothing.
    return SendOutcome(
        status="failed",
        reason=code or f"http_{http_status}",
        retryable=http_status >= 500,
    )


def error_of(body: dict) -> tuple:
    """(code, human detail) from Meta's error envelope.

    The code lands in ``reason`` verbatim — the provider's own word, not our
    paraphrase, so "why?" has an answer matching Meta's docs.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return None, REASON_UNREADABLE
    code = error.get("code")
    return (
        str(code) if code is not None else None,
        str(error.get("message") or ""),
    )
