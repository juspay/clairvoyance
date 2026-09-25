"""Call outcome vocabulary: the connection / agent / eval layers.

``lead_call_tracker.outcome`` is one free-text column with four writers — the
carrier, the dispatcher, the pipeline's own fallbacks and the agent — so one
word means different things depending on who wrote it last (``BUSY`` is the
idle-timeout fallback AND the LLM's "customer is busy"; a carrier ``busy`` is
stored as ``NO_ANSWER``). This splits it into three layers, each with ONE owner:

  connection  connection_status, connection_reason, provider_status,
              hangup_cause, end_reason — the system: dispatcher, inbound
              policy, carrier status callback, pipeline. Never an LLM.
  agent       agent_outcome, outcome_source — the agent: LLM functions, IVR
              options, observers.
  eval        eval_outcome, eval_status, eval_result_id — the post-call eval.

Phase 1 writes these beside the legacy column, which stays byte-for-byte as
today. The vocabulary lives here, never in CHECKs (repo rule): a value the
code does not know is dropped at the writer, so it can never fail the legacy
write it rides with.

Plan, decisions and phases: docs/CALL_OUTCOMES.md.
"""

from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel


class ConnectionStatus(str, Enum):
    """How far the attempt got. One per attempt row, written by the system."""

    NOT_DIALED = "NOT_DIALED"  # dispatcher refused before dialing
    REJECTED = "REJECTED"  # inbound call turned away by policy / capacity
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"  # the carrier's busy tone — never the agent's "call later"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    ANSWERED = "ANSWERED"
    UNKNOWN = "UNKNOWN"  # no signal survived (stuck-call reaper)


class ConnectionReason(str, Enum):
    """Why the attempt stopped where it did."""

    # NOT_DIALED
    PRECHECK_FAILED = "PRECHECK_FAILED"
    BLACKLISTED = "BLACKLISTED"
    NUMBER_UNAVAILABLE = "NUMBER_UNAVAILABLE"
    INVALID_PHONE = "INVALID_PHONE"
    NO_CONFIG = "NO_CONFIG"
    ABORTED = "ABORTED"
    CALL_LIMIT = "CALL_LIMIT"  # merchant's per-customer call cap reached
    # REJECTED
    BLOCKED = "BLOCKED"
    CAPACITY = "CAPACITY"
    # carrier
    TIMEOUT = "TIMEOUT"


class EndReason(str, Enum):
    """How an answered session ended. Only set when the call was answered."""

    AGENT_ENDED = "AGENT_ENDED"
    CUSTOMER_HANGUP = "CUSTOMER_HANGUP"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    TRANSFERRED = "TRANSFERRED"
    EARLY_HANGUP = "EARLY_HANGUP"
    IVR_NO_INPUT = "IVR_NO_INPUT"
    IVR_ERROR = "IVR_ERROR"
    PIPELINE_ERROR = "PIPELINE_ERROR"
    PIPELINE_NOT_STARTED = "PIPELINE_NOT_STARTED"


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


# Accepted by every template without being declared: "reached a human" and
# the connection-driven retry rule read these across all agents.
RESERVED_OUTCOMES = frozenset({"VOICEMAIL", "CALLBACK_REQUESTED"})

# Column widths (migration 080). A raw provider string is clipped to fit; an
# agent outcome that does not fit is dropped (the legacy varchar(50) would
# have refused the same value anyway).
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


def normalize_agent_outcome(value: Any) -> Optional[str]:
    """The agent's word, trimmed and upper-cased (normalize at every writer).

    ``confirmed`` and ``CONFIRMED`` are one outcome. The legacy column keeps
    the agent's original casing; only agent_outcome is normalized.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
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
# ErrorCode on failed legs. Best-effort diagnostics only — nothing branches
# on the value.
_HANGUP_CAUSE_KEYS = ("HangupCauseName", "HangupCause", "SipResponseCode", "ErrorCode")


def hangup_cause_from_callback(form: Mapping[str, Any]) -> Optional[str]:
    """The first hangup-cause field present in a status-callback form."""
    for key in _HANGUP_CAUSE_KEYS:
        value = form.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def end_reason_from_ended_by(call_ended_by: Optional[str]) -> Optional[EndReason]:
    """Fallback end reason from ``metaData.call_ended_by``.

    Used when no path set an explicit end_reason — e.g. a late
    fire-and-forget outcome write refreshed the in-memory lead after the
    ending path set it. ``call_ended_by`` rides in metaData, which that
    refresh preserves.
    """
    if call_ended_by == "customer":
        return EndReason.CUSTOMER_HANGUP
    if call_ended_by == "agent":
        return EndReason.AGENT_ENDED
    if call_ended_by == "system":
        return EndReason.PIPELINE_ERROR
    return None


class CallOutcome(BaseModel):
    """The call outcome columns one write carries beside the legacy outcome.

    Every field is optional and only non-None fields are written, so a
    mid-call write never clears what an earlier write recorded.
    """

    connection_status: Optional[ConnectionStatus] = None
    connection_reason: Optional[ConnectionReason] = None
    provider_status: Optional[str] = None
    hangup_cause: Optional[str] = None
    end_reason: Optional[EndReason] = None
    agent_outcome: Optional[str] = None
    outcome_source: Optional[OutcomeSource] = None

    def columns(self) -> Dict[str, str]:
        """Non-None values as ``{column: value}``, normalized for storage."""
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
            "agent_outcome": normalize_agent_outcome(self.agent_outcome),
            "outcome_source": (
                self.outcome_source.value if self.outcome_source else None
            ),
        }
        return {column: value for column, value in values.items() if value}


def ended_session_call_outcome(lead: Any) -> CallOutcome:
    """The call outcome columns ``end_conversation`` hands the completion.

    The agent outcome as the hooks recorded it, and how the session ended:
    the reason an ending path set explicitly (idle timeout, disconnect, IVR),
    else what ``metaData.call_ended_by`` says — set on every ending path, and
    carried through a mid-call refresh of the lead where end_reason is not.
    """
    meta = getattr(lead, "metaData", None) or {}
    return call_outcome_from_lead(
        lead,
        end_reason=(
            parse_enum(EndReason, getattr(lead, "end_reason", None))
            or end_reason_from_ended_by(meta.get("call_ended_by"))
        ),
    )


def completed_call_outcome(
    call_outcome: Optional[CallOutcome], is_transfer: bool = False
) -> CallOutcome:
    """The call outcome columns a pipeline's completion writes.

    Only a pipeline that ran reaches completion, so the call was answered
    unless the caller says otherwise. A successful transfer is how the
    session ended; the agent's own outcome is left exactly as the agent set
    it (the legacy column's TRANSFERRED override does not apply here).
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


def call_outcome_from_lead(lead: Any, **overrides: Any) -> CallOutcome:
    """The in-memory lead's call outcome columns, with ``overrides`` applied on top.

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
        "agent_outcome": normalize_agent_outcome(getattr(lead, "agent_outcome", None)),
        "outcome_source": parse_enum(
            OutcomeSource, getattr(lead, "outcome_source", None)
        ),
    }
    values.update(overrides)
    return CallOutcome(**values)
