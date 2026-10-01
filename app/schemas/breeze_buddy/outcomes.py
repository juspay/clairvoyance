"""Call outcome facts, and the one function that turns them into ``outcome``.

``lead_call_tracker.outcome`` is written today by about thirty places — the
dispatcher, the carrier callback, the pipeline's own fallbacks, the agent —
and the last writer wins. The replacement records FACTS instead, each written
by the part of the system that knows it:

  connection  connection_status, connection_reason, provider_status,
              hangup_cause — how far the attempt got and why it stopped
              (dispatcher, inbound policy, carrier callback, reconcile, reaper)
  ending      end_reason — how an answered session ended (pipeline, IVR
              walker, reaper)
  agent       agent_outcome, outcome_source — what the agent decided, as the
              agent said it (LLM functions, observers, IVR options)
  eval        eval_outcome, eval_status, eval_result_id — the post-call eval

and ``legacy_outcome(facts)`` computes the word, byte-for-byte the word the
old writers produce. Phase 1 records the facts beside the old writes and
compares the two on every terminal write (the shadow check); phase 2 deletes
the old writes and writes ``outcome`` only from this function (or the eval).

The vocabulary lives here, never in CHECKs (repo rule): a value the code does
not know is dropped at the writer, so it can never fail the write it rides
with. Plan: docs/CALL_OUTCOMES.md.
"""

from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel


class ConnectionStatus(str, Enum):
    """How far the attempt got. One per attempt row, written by the system."""

    NOT_DIALED = "NOT_DIALED"  # refused before dialing
    REJECTED = "REJECTED"  # inbound call turned away by policy / capacity
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"  # the carrier's busy line — never the agent's "call later"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    ANSWERED = "ANSWERED"
    UNKNOWN = "UNKNOWN"  # no signal survived (stuck-call reaper)


class ConnectionReason(str, Enum):
    """Why the attempt stopped before a conversation: today's exact words, so
    ``outcome`` for a refused or turned-away attempt is a straight copy."""

    # NOT_DIALED
    PRECHECK_FAILED = "PRECHECK_FAILED"
    BLACKLISTED = "BLACKLISTED"
    NUMBER_UNAVAILABLE = "NUMBER_UNAVAILABLE"
    INVALID_PHONE = "INVALID_PHONE"
    NO_CONFIG = "NO_CONFIG"
    CALL_LIMIT_REACHED = "CALL_LIMIT_REACHED"
    ABORT = "ABORT"  # lead API, campaign, widget, demo (handle_lead_abort)
    ABORTED = "ABORTED"  # WooCommerce order cancel, CRM call-node daily cap
    # REJECTED
    BLOCKED_REJECT = "BLOCKED_REJECT"
    BLOCKED_REDIRECT = "BLOCKED_REDIRECT"
    CAPACITY_REJECTED = "CAPACITY_REJECTED"
    # carrier (the attempt still counts as NO_ANSWER)
    TIMEOUT = "TIMEOUT"


class EndReason(str, Enum):
    """How an answered session ended. The first ending recorded wins
    (``record_end_reason``), except where noted."""

    AGENT_ENDED = "AGENT_ENDED"  # the flow's end_conversation
    GLOBAL_END = "GLOBAL_END"  # the LLM called the global end_conversation
    CUSTOMER_HANGUP = "CUSTOMER_HANGUP"  # customer / client disconnected
    USER_IDLE_TIMEOUT = "USER_IDLE_TIMEOUT"  # idle retries exhausted
    IDLE_TIMEOUT = "IDLE_TIMEOUT"  # the pipeline's own idle disconnect
    TRANSFERRED = "TRANSFERRED"  # successful transfer (set at completion)
    EARLY_HANGUP = "EARLY_HANGUP"  # transport setup failed before the agent
    WIDGET_ENDED = "WIDGET_ENDED"  # the widget visitor ended voice
    PIPELINE_ERROR = "PIPELINE_ERROR"  # end_call_with_errors (setup error)
    IVR_ENDED = "IVR_ENDED"  # the caller reached an IVR END option
    IVR_NO_INPUT = "IVR_NO_INPUT"  # IVR retries exhausted with no key press
    IVR_ERROR = "IVR_ERROR"  # IVR could not start (no socket, flow, voice)
    IVR_LOOP_GUARD = "IVR_LOOP_GUARD"  # IVR transition limit hit
    IVR_NODE_MISSING = "IVR_NODE_MISSING"  # IVR transitioned to no node
    IVR_EXCEPTION = "IVR_EXCEPTION"  # the IVR walker raised mid-walk
    REAPED = "REAPED"  # stuck-call reaper closed a lead a pipeline had run on


class OutcomeSource(str, Enum):
    """Which part of the agent decided agent_outcome."""

    LLM = "LLM"
    IVR = "IVR"
    OBSERVER = "OBSERVER"


class EvalStatus(str, Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Column widths (migration 081). A raw provider string is clipped to fit; an
# agent word that does not fit is dropped (the legacy varchar(50) refuses the
# same value).
_AGENT_OUTCOME_MAX = 50
_PROVIDER_STATUS_MAX = 50
_HANGUP_CAUSE_MAX = 100

E = TypeVar("E", bound=Enum)


def parse_enum(enum_cls: Type[E], value: Any) -> Optional[E]:
    """``enum_cls(value)``, or None for a missing or unknown value."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def agent_word(value: Any) -> Optional[str]:
    """The agent's word exactly as the legacy column stores it: no trimming,
    no case change. None for a missing or empty word, or one too long for
    the column."""
    if value is None:
        return None
    text = str(value)
    if not text or len(text) > _AGENT_OUTCOME_MAX:
        return None
    return text


def _clip(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


# Terminal carrier statuses, lower-cased, across Twilio / Plivo / Exotel.
_PROVIDER_STATUS_MAP: Dict[str, Tuple[ConnectionStatus, Optional[ConnectionReason]]] = {
    "completed": (ConnectionStatus.ANSWERED, None),
    "no-answer": (ConnectionStatus.NO_ANSWER, None),
    "busy": (ConnectionStatus.BUSY, None),
    "failed": (ConnectionStatus.FAILED, None),
    "canceled": (ConnectionStatus.CANCELED, None),
    "cancelled": (ConnectionStatus.CANCELED, None),
    "cancel": (ConnectionStatus.CANCELED, None),
    # Plivo: network / carrier timeout before anyone picked up.
    "timeout": (ConnectionStatus.NO_ANSWER, ConnectionReason.TIMEOUT),
}


def connection_from_provider_status(
    provider_status: Optional[str],
) -> Tuple[Optional[ConnectionStatus], Optional[ConnectionReason]]:
    """Map a raw carrier status to (connection_status, connection_reason)."""
    if not provider_status:
        return None, None
    return _PROVIDER_STATUS_MAP.get(provider_status.strip().lower(), (None, None))


# Keys, in order of preference, that carry the carrier's hangup cause.
# Plivo sends HangupCauseName / HangupCause; Twilio sends SipResponseCode /
# ErrorCode on failed legs. Diagnostics only — nothing branches on the value.
_HANGUP_CAUSE_KEYS = ("HangupCauseName", "HangupCause", "SipResponseCode", "ErrorCode")


def hangup_cause_from_callback(form: Mapping[str, Any]) -> Optional[str]:
    """The first hangup-cause field present in a status-callback form."""
    for key in _HANGUP_CAUSE_KEYS:
        value = form.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def record_end_reason(lead: Any, reason: EndReason) -> None:
    """Set how the session ended on the in-memory lead, unless an earlier
    ending already did: the first ending wins, as ``conversation_ended``
    makes it win for the legacy word."""
    if lead is not None and not getattr(lead, "end_reason", None):
        lead.end_reason = reason


def end_reason_from_meta(meta: Mapping[str, Any]) -> Optional[EndReason]:
    """Fallback end reason from the ending keys in ``metaData``.

    Used when no ending path recorded one on the lead — e.g. a mid-call
    outcome write refreshed the in-memory lead after the ending path set it.
    The keys ride in metaData, which that refresh preserves.
    """
    ended_by = meta.get("call_ended_by")
    if meta.get("call_end_reason") == "user_idle_timeout":
        return EndReason.USER_IDLE_TIMEOUT
    if ended_by == "customer":
        return EndReason.CUSTOMER_HANGUP
    if ended_by == "agent":
        # Only the global end_conversation writes call_end_reason for an
        # agent ending (the LLM's reason, or its default).
        if meta.get("call_end_reason"):
            return EndReason.GLOBAL_END
        return EndReason.AGENT_ENDED
    if ended_by == "system":
        return EndReason.PIPELINE_ERROR
    return None


class CallOutcome(BaseModel):
    """The facts one write records. Every field is optional and only non-None
    fields are written, so a later write never clears an earlier fact."""

    connection_status: Optional[ConnectionStatus] = None
    connection_reason: Optional[ConnectionReason] = None
    provider_status: Optional[str] = None
    hangup_cause: Optional[str] = None
    end_reason: Optional[EndReason] = None
    agent_outcome: Optional[str] = None
    outcome_source: Optional[OutcomeSource] = None

    def columns(self) -> Dict[str, str]:
        """Non-None values as ``{column: value}``, ready for storage."""
        values: Dict[str, Optional[str]] = {
            "connection_status": (
                self.connection_status.value if self.connection_status else None
            ),
            "connection_reason": (
                self.connection_reason.value if self.connection_reason else None
            ),
            "provider_status": _clip(self.provider_status, _PROVIDER_STATUS_MAX),
            "hangup_cause": _clip(self.hangup_cause, _HANGUP_CAUSE_MAX),
            "end_reason": self.end_reason.value if self.end_reason else None,
            "agent_outcome": agent_word(self.agent_outcome),
            "outcome_source": (
                self.outcome_source.value if self.outcome_source else None
            ),
        }
        return {column: value for column, value in values.items() if value}


def call_outcome_from_lead(lead: Any, **overrides: Any) -> CallOutcome:
    """The in-memory lead's facts, with ``overrides`` applied on top.

    Tolerant by construction: a value the vocabulary does not know is
    dropped, never raised, because this runs on the call's terminal path.
    """
    values: Dict[str, Any] = {
        "connection_status": parse_enum(
            ConnectionStatus, getattr(lead, "connection_status", None)
        ),
        "connection_reason": parse_enum(
            ConnectionReason, getattr(lead, "connection_reason", None)
        ),
        "provider_status": getattr(lead, "provider_status", None),
        "hangup_cause": getattr(lead, "hangup_cause", None),
        "end_reason": parse_enum(EndReason, getattr(lead, "end_reason", None)),
        "agent_outcome": agent_word(getattr(lead, "agent_outcome", None)),
        "outcome_source": parse_enum(
            OutcomeSource, getattr(lead, "outcome_source", None)
        ),
    }
    values.update(overrides)
    return CallOutcome(**values)


def ended_session_call_outcome(lead: Any) -> CallOutcome:
    """The facts ``end_conversation`` hands the completion: the agent's word
    as the hooks recorded it, and how the session ended — the reason an
    ending path recorded, else what ``metaData``'s ending keys say."""
    meta = getattr(lead, "metaData", None) or {}
    return call_outcome_from_lead(
        lead,
        end_reason=(
            parse_enum(EndReason, getattr(lead, "end_reason", None))
            or end_reason_from_meta(meta)
        ),
    )


def completed_call_outcome(
    call_outcome: Optional[CallOutcome], is_transfer: bool = False
) -> CallOutcome:
    """The facts a pipeline's completion writes.

    Only a pipeline that ran reaches completion, so the call was answered
    unless the caller says otherwise. A successful transfer overrides how the
    session ended, as it overrides the legacy word; the agent's own word is
    kept as the agent set it.
    """
    result = call_outcome or CallOutcome()
    updates: Dict[str, Any] = {}
    if result.connection_status is None:
        updates["connection_status"] = ConnectionStatus.ANSWERED
    if is_transfer:
        updates["end_reason"] = EndReason.TRANSFERRED
    return result.model_copy(update=updates)


# Per-call columns a widget voice lead clears when it is reused for the next
# attachment (the legacy outcome is cleared by the same query).
CALL_OUTCOME_COLUMNS = tuple(CallOutcome.model_fields) + (
    "eval_outcome",
    "eval_status",
    "eval_result_id",
)


# ── the word ────────────────────────────────────────────────────────────────

# Carrier failures: every one is NO_ANSWER in the legacy word.
_CARRIER_FAILURES = frozenset(
    {
        ConnectionStatus.NO_ANSWER,
        ConnectionStatus.BUSY,
        ConnectionStatus.FAILED,
        ConnectionStatus.CANCELED,
    }
)

# Endings that replace the agent's word, as the old writers overwrite it.
_OVERRIDING_ENDINGS: Dict[EndReason, str] = {
    EndReason.TRANSFERRED: "TRANSFERRED",
    EndReason.USER_IDLE_TIMEOUT: "BUSY",
    EndReason.PIPELINE_ERROR: "UNKNOWN",
    EndReason.IVR_ERROR: "IVR_ERROR",
    EndReason.IVR_LOOP_GUARD: "IVR_LOOP_GUARD",
    EndReason.IVR_NODE_MISSING: "IVR_NODE_MISSING",
}

# Endings with no agent word: the default the old writers fill in. An
# ending missing here (AGENT_ENDED: the flow's own end with no outcome)
# leaves the word empty, as today.
_NO_WORD_ENDINGS: Dict[EndReason, str] = {
    EndReason.EARLY_HANGUP: "EARLY_HANGUP",
    EndReason.WIDGET_ENDED: "ended_by_widget",
    EndReason.CUSTOMER_HANGUP: "BUSY",
    EndReason.IDLE_TIMEOUT: "BUSY",
    EndReason.GLOBAL_END: "BUSY",
    EndReason.IVR_ENDED: "BUSY",
    EndReason.IVR_NO_INPUT: "BUSY",
    EndReason.IVR_EXCEPTION: "BUSY",
    EndReason.REAPED: "UNKNOWN",
}


def legacy_outcome(facts: Any) -> Optional[str]:
    """PURE: a lead's facts -> the ``outcome`` word the old writers produce.

    ``facts`` is anything with the fact attributes (a LeadCallTracker, a
    CallOutcome). Values may be enum members or their strings. The eval's
    word, when it decides, replaces this at completion; nothing here reads it.
    """
    status = parse_enum(ConnectionStatus, getattr(facts, "connection_status", None))
    reason = parse_enum(ConnectionReason, getattr(facts, "connection_reason", None))
    ending = parse_enum(EndReason, getattr(facts, "end_reason", None))
    word = agent_word(getattr(facts, "agent_outcome", None))

    if status in (ConnectionStatus.NOT_DIALED, ConnectionStatus.REJECTED):
        return reason.value if reason else None
    if status in _CARRIER_FAILURES:
        return "NO_ANSWER"
    if status is ConnectionStatus.UNKNOWN:
        return "UNKNOWN"
    if status is None and word is None and ending is None:
        return None
    # Answered (or a chat / a row whose status is not recorded): the agent's
    # word, unless the ending overrides it; with no word, the ending's default.
    override = _OVERRIDING_ENDINGS.get(ending) if ending is not None else None
    if override is not None:
        return override
    if word is not None:
        return word
    if ending is None:
        # Answered with nothing recorded: the carrier said completed and no
        # pipeline finished the lead (reconcile).
        return "UNKNOWN" if status is ConnectionStatus.ANSWERED else None
    return _NO_WORD_ENDINGS.get(ending)
