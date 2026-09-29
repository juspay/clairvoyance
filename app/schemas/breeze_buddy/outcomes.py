"""Call outcome facts, and the one function that turns them into ``outcome``.

``lead_call_tracker.outcome`` is written today by about thirty places — the
dispatcher, the carrier callback, the pipeline's own fallbacks, the agent —
and the last writer wins. The replacement records FACTS instead, each column
named by who writes it:

  platform  platform_status, platform_reason — was the call set up, and how,
            or why not (dispatcher, inbound policy, Daily session start)
  provider  provider_status, provider_reason, provider_hangup_cause — what
            happened on the phone line (carrier callback, completion,
            reconcile, reaper); empty on a web session, which has no line
  session   session_end_reason — how an answered session ended (pipeline,
            IVR walker, reaper)
  agent     agent_outcome, agent_outcome_source — what the agent decided, as
            the agent said it (LLM functions, observers, IVR options)
  eval      eval_outcome, eval_status, eval_result_id — the post-call eval

and ``legacy_outcome(facts)`` computes the word, byte-for-byte the word the
old writers produce. PR 1 records the facts beside the old writes and
compares the two on every terminal write (the shadow check); PR 2 deletes
the old writes and writes ``outcome`` only from this function (or the eval).

The vocabulary lives here, never in CHECKs (repo rule): a value the code does
not know is dropped at the writer, so it can never fail the write it rides
with. Plan: docs/CALL_OUTCOMES.md.
"""

from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel


class PlatformStatus(str, Enum):
    """Was the call set up, in either direction? Written by our platform."""

    # Placed with the provider, an inbound call accepted, or a web session
    # started.
    INITIATED = "INITIATED"
    # Skipped, refused or cancelled before a session started.
    NOT_INITIATED = "NOT_INITIATED"


class PlatformReason(str, Enum):
    """How the call was set up (INITIATED), or why it was not (NOT_INITIATED).
    The NOT_INITIATED reasons are today's exact words, so ``outcome`` for a
    refused or cancelled attempt is a straight copy."""

    # INITIATED
    DIALED = "DIALED"  # a phone call through the provider, either direction
    WEB_SESSION = "WEB_SESSION"  # a widget / Daily voice session (no line)
    # NOT_INITIATED: outbound skipped or cancelled
    PRECHECK_FAILED = "PRECHECK_FAILED"
    BLACKLISTED = "BLACKLISTED"
    NUMBER_UNAVAILABLE = "NUMBER_UNAVAILABLE"
    INVALID_PHONE = "INVALID_PHONE"
    NO_CONFIG = "NO_CONFIG"
    CALL_LIMIT_REACHED = "CALL_LIMIT_REACHED"
    ABORT = "ABORT"  # lead API, campaign, widget, demo (handle_lead_abort)
    ABORTED = "ABORTED"  # WooCommerce order cancel, CRM call-node daily cap
    # NOT_INITIATED: inbound refused
    BLOCKED_REJECT = "BLOCKED_REJECT"
    BLOCKED_REDIRECT = "BLOCKED_REDIRECT"
    CAPACITY_REJECTED = "CAPACITY_REJECTED"


class ProviderStatus(str, Enum):
    """What happened on the phone line. Empty on a web session."""

    ANSWERED = "ANSWERED"
    NOT_ANSWERED = "NOT_ANSWERED"  # provider_reason says why
    UNKNOWN = "UNKNOWN"  # no provider signal survived (stuck-call reaper)


class ProviderReason(str, Enum):
    """One spelling for every provider: COMPLETED with ANSWERED, and why the
    call went unanswered with NOT_ANSWERED."""

    COMPLETED = "COMPLETED"  # picked up; completed normally on the line
    NO_ANSWER = "NO_ANSWER"  # it rang out
    BUSY = "BUSY"  # the line was busy — never the agent's "call later"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMEOUT = "TIMEOUT"  # Plivo: network / carrier timeout


class SessionEndReason(str, Enum):
    """How an answered session ended. The first ending recorded wins
    (``record_session_end_reason``), except where noted."""

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
    REAPED = "REAPED"  # stuck-call reaper closed the lead


class AgentOutcomeSource(str, Enum):
    """Which part of the agent decided agent_outcome."""

    LLM = "LLM"
    IVR = "IVR"
    OBSERVER = "OBSERVER"


class EvalStatus(str, Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Column widths (migration 083). The provider's hangup cause is clipped to
# fit; an agent word that does not fit is dropped (the legacy varchar(50)
# refuses the same value).
_AGENT_OUTCOME_MAX = 50
_HANGUP_CAUSE_MAX = 100

# Execution modes that run on a Daily web room: a widget / Daily voice
# session, never a phone line. By value, so the vocabulary imports nothing
# from the schemas it describes.
_WEB_SESSION_MODES = frozenset({"DAILY", "DAILY_TEST", "DAILY_STREAM"})

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


def is_web_session(execution_mode: Any) -> bool:
    """Whether a lead in ``execution_mode`` is a widget / Daily voice session."""
    return str(getattr(execution_mode, "value", execution_mode)) in _WEB_SESSION_MODES


def _clip(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _value(member: Optional[Enum]) -> Optional[str]:
    return member.value if member is not None else None


# Terminal carrier statuses, lower-cased, across Twilio / Plivo / Exotel.
_PROVIDER_STATUS_MAP: Dict[str, Tuple[ProviderStatus, ProviderReason]] = {
    "completed": (ProviderStatus.ANSWERED, ProviderReason.COMPLETED),
    "no-answer": (ProviderStatus.NOT_ANSWERED, ProviderReason.NO_ANSWER),
    "busy": (ProviderStatus.NOT_ANSWERED, ProviderReason.BUSY),
    "failed": (ProviderStatus.NOT_ANSWERED, ProviderReason.FAILED),
    "canceled": (ProviderStatus.NOT_ANSWERED, ProviderReason.CANCELED),
    "cancelled": (ProviderStatus.NOT_ANSWERED, ProviderReason.CANCELED),
    "cancel": (ProviderStatus.NOT_ANSWERED, ProviderReason.CANCELED),
    # Plivo: network / carrier timeout before anyone picked up.
    "timeout": (ProviderStatus.NOT_ANSWERED, ProviderReason.TIMEOUT),
}


def provider_from_status(
    status: Optional[str],
) -> Tuple[Optional[ProviderStatus], Optional[ProviderReason]]:
    """Map a raw carrier status to (provider_status, provider_reason)."""
    if not status:
        return None, None
    return _PROVIDER_STATUS_MAP.get(status.strip().lower(), (None, None))


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


def record_session_end_reason(lead: Any, reason: SessionEndReason) -> None:
    """Set how the session ended on the in-memory lead, unless an earlier
    ending already did: the first ending wins, as ``conversation_ended``
    makes it win for the legacy word."""
    if lead is not None and not getattr(lead, "session_end_reason", None):
        lead.session_end_reason = reason


def session_end_reason_from_meta(
    meta: Mapping[str, Any],
) -> Optional[SessionEndReason]:
    """Fallback session end reason from the ending keys in ``metaData``.

    Used by ``end_conversation`` when no ending path recorded one on the lead
    — e.g. a mid-call outcome write refreshed the in-memory lead after the
    ending path set it. The keys ride in metaData, which that refresh
    preserves.
    """
    ended_by = meta.get("call_ended_by")
    if meta.get("call_end_reason") == "user_idle_timeout":
        return SessionEndReason.USER_IDLE_TIMEOUT
    if ended_by == "customer":
        return SessionEndReason.CUSTOMER_HANGUP
    if ended_by == "agent":
        # Only the global end_conversation writes call_end_reason for an
        # agent ending (the LLM's reason, or its default).
        if meta.get("call_end_reason"):
            return SessionEndReason.GLOBAL_END
        return SessionEndReason.AGENT_ENDED
    if ended_by == "system":
        # Of the "system" endings, only the pipeline's idle disconnect relies
        # on this fallback: user idle is caught above by its call_end_reason,
        # a setup error passes PIPELINE_ERROR to its completion itself, and
        # the IVR walker records its own ending.
        return SessionEndReason.IDLE_TIMEOUT
    return None


class CallOutcome(BaseModel):
    """The facts one write records. Every field is optional and only non-None
    fields are written, so a later write never clears an earlier fact."""

    platform_status: Optional[PlatformStatus] = None
    platform_reason: Optional[PlatformReason] = None
    provider_status: Optional[ProviderStatus] = None
    provider_reason: Optional[ProviderReason] = None
    provider_hangup_cause: Optional[str] = None
    session_end_reason: Optional[SessionEndReason] = None
    agent_outcome: Optional[str] = None
    agent_outcome_source: Optional[AgentOutcomeSource] = None

    def columns(self) -> Dict[str, str]:
        """Non-None values as ``{column: value}``, ready for storage."""
        values: Dict[str, Optional[str]] = {
            "platform_status": _value(self.platform_status),
            "platform_reason": _value(self.platform_reason),
            "provider_status": _value(self.provider_status),
            "provider_reason": _value(self.provider_reason),
            "provider_hangup_cause": _clip(
                self.provider_hangup_cause, _HANGUP_CAUSE_MAX
            ),
            "session_end_reason": _value(self.session_end_reason),
            "agent_outcome": agent_word(self.agent_outcome),
            "agent_outcome_source": _value(self.agent_outcome_source),
        }
        return {column: value for column, value in values.items() if value}


def initiated_call_outcome(web_session: bool = False) -> CallOutcome:
    """The facts a call's set-up records: INITIATED, as a phone call through
    the provider (DIALED, either direction) or a widget / Daily voice session
    (WEB_SESSION)."""
    return CallOutcome(
        platform_status=PlatformStatus.INITIATED,
        platform_reason=(
            PlatformReason.WEB_SESSION if web_session else PlatformReason.DIALED
        ),
    )


def not_initiated_call_outcome(reason: Optional[PlatformReason]) -> CallOutcome:
    """The facts of an attempt our platform skipped, refused or cancelled."""
    return CallOutcome(
        platform_status=PlatformStatus.NOT_INITIATED, platform_reason=reason
    )


def call_outcome_from_lead(lead: Any, **overrides: Any) -> CallOutcome:
    """The in-memory lead's facts, with ``overrides`` applied on top.

    Tolerant by construction: a value the vocabulary does not know is
    dropped, never raised, because this runs on the call's terminal path.
    """
    values: Dict[str, Any] = {
        "platform_status": parse_enum(
            PlatformStatus, getattr(lead, "platform_status", None)
        ),
        "platform_reason": parse_enum(
            PlatformReason, getattr(lead, "platform_reason", None)
        ),
        "provider_status": parse_enum(
            ProviderStatus, getattr(lead, "provider_status", None)
        ),
        "provider_reason": parse_enum(
            ProviderReason, getattr(lead, "provider_reason", None)
        ),
        "provider_hangup_cause": getattr(lead, "provider_hangup_cause", None),
        "session_end_reason": parse_enum(
            SessionEndReason, getattr(lead, "session_end_reason", None)
        ),
        "agent_outcome": agent_word(getattr(lead, "agent_outcome", None)),
        "agent_outcome_source": parse_enum(
            AgentOutcomeSource, getattr(lead, "agent_outcome_source", None)
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
        session_end_reason=(
            parse_enum(SessionEndReason, getattr(lead, "session_end_reason", None))
            or session_end_reason_from_meta(meta)
        ),
    )


def completed_call_outcome(
    call_outcome: Optional[CallOutcome],
    is_transfer: bool = False,
    web_session: bool = False,
) -> CallOutcome:
    """The facts a pipeline's completion writes.

    Only a call that was set up reaches completion: INITIATED unless the row
    already says otherwise. A phone call whose pipeline ran was answered; a
    web session has no line, so its provider columns stay empty. A successful
    transfer overrides how the session ended, as it overrides the legacy
    word; the agent's own word is kept as the agent set it.
    """
    result = call_outcome or CallOutcome()
    updates: Dict[str, Any] = {}
    if result.platform_status is None:
        updates.update(
            initiated_call_outcome(web_session).model_dump(exclude_none=True)
        )
    if not web_session and result.provider_status is None:
        updates["provider_status"] = ProviderStatus.ANSWERED
        updates["provider_reason"] = ProviderReason.COMPLETED
    if is_transfer:
        updates["session_end_reason"] = SessionEndReason.TRANSFERRED
    return result.model_copy(update=updates)


# Per-call columns a widget voice lead clears when it is reused for the next
# attachment (the legacy outcome is cleared by the same query).
CALL_OUTCOME_COLUMNS = tuple(CallOutcome.model_fields) + (
    "eval_outcome",
    "eval_status",
    "eval_result_id",
)


# ── the word ────────────────────────────────────────────────────────────────

# The NOT_INITIATED reasons: each is also the outcome word.
_NOT_INITIATED_REASONS = frozenset(PlatformReason) - {
    PlatformReason.DIALED,
    PlatformReason.WEB_SESSION,
}

# Endings that replace the agent's word, as the old writers overwrite it.
_OVERRIDING_ENDINGS: Dict[SessionEndReason, str] = {
    SessionEndReason.TRANSFERRED: "TRANSFERRED",
    SessionEndReason.USER_IDLE_TIMEOUT: "BUSY",
    SessionEndReason.PIPELINE_ERROR: "UNKNOWN",
    SessionEndReason.IVR_ERROR: "IVR_ERROR",
    SessionEndReason.IVR_LOOP_GUARD: "IVR_LOOP_GUARD",
    SessionEndReason.IVR_NODE_MISSING: "IVR_NODE_MISSING",
}

# Endings with no agent word: the default the old writers fill in. An
# ending missing here (AGENT_ENDED: the flow's own end with no outcome)
# leaves the word empty, as today.
_NO_WORD_ENDINGS: Dict[SessionEndReason, str] = {
    SessionEndReason.EARLY_HANGUP: "EARLY_HANGUP",
    SessionEndReason.WIDGET_ENDED: "ended_by_widget",
    SessionEndReason.CUSTOMER_HANGUP: "BUSY",
    SessionEndReason.IDLE_TIMEOUT: "BUSY",
    SessionEndReason.GLOBAL_END: "BUSY",
    SessionEndReason.IVR_ENDED: "BUSY",
    SessionEndReason.IVR_NO_INPUT: "BUSY",
    SessionEndReason.IVR_EXCEPTION: "BUSY",
    SessionEndReason.REAPED: "UNKNOWN",
}


def legacy_outcome(facts: Any) -> Optional[str]:
    """PURE: a lead's facts -> the ``outcome`` word the old writers produce.

    ``facts`` is anything with the fact attributes (a LeadCallTracker, a
    CallOutcome). Values may be enum members or their strings. The eval's
    word, when it decides, replaces this at completion; nothing here reads it.
    """
    platform = parse_enum(PlatformStatus, getattr(facts, "platform_status", None))
    reason = parse_enum(PlatformReason, getattr(facts, "platform_reason", None))
    provider = parse_enum(ProviderStatus, getattr(facts, "provider_status", None))
    ending = parse_enum(SessionEndReason, getattr(facts, "session_end_reason", None))
    word = agent_word(getattr(facts, "agent_outcome", None))

    if platform is PlatformStatus.NOT_INITIATED:
        if reason is not None and reason in _NOT_INITIATED_REASONS:
            return reason.value
        return None
    if provider is ProviderStatus.NOT_ANSWERED:
        return "NO_ANSWER"
    if provider is ProviderStatus.UNKNOWN:
        return "UNKNOWN"
    if provider is None and word is None and ending is None:
        return None
    # Answered (or a web session / chat, which has no provider status): the
    # agent's word, unless the ending overrides it; with no word, the
    # ending's default.
    override = _OVERRIDING_ENDINGS.get(ending) if ending is not None else None
    if override is not None:
        return override
    if word is not None:
        return word
    if ending is None:
        # Answered with nothing recorded: the carrier said completed and no
        # pipeline finished the lead (reconcile).
        return "UNKNOWN" if provider is ProviderStatus.ANSWERED else None
    return _NO_WORD_ENDINGS.get(ending)
