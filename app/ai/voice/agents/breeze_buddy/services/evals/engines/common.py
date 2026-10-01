"""What every engine shares: the state projection a judge sees
(``build_state``) and the stored structure a judgment produces
(``Verdict`` of typed results). No I/O, no engine or provider imports.
Configuration types and question vocabularies are NOT common — each
engine owns its own, decoded in its ``validate_configuration``.
"""

from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import BaseModel, Field

_CONTACT_KEY_MARKERS = ("phone", "mobile", "email", "msisdn", "whatsapp", "contact")


def _is_contact_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _CONTACT_KEY_MARKERS)


def _strip_contacts(value: Any) -> Any:
    """Drop contact-named keys from nested dicts and lists too."""
    if isinstance(value, dict):
        return {
            key: _strip_contacts(item)
            for key, item in value.items()
            if not (isinstance(key, str) and _is_contact_key(key))
        }
    if isinstance(value, list):
        return [_strip_contacts(item) for item in value]
    return value


def build_state(context: Mapping[str, Any]) -> Dict[str, Any]:
    """PURE gather: the projection a judge sees, nothing more — the same
    shape for a call and for a chat (WhatsApp, widget).

    ``context`` is the worker's: ``channel``, ``transcript`` (the normalized
    turn list, the same source for VOICE and CHAT), ``payload`` (a lead's
    payload or a chat session's render-time variables), ``meta_data`` (a
    lead's metadata or a chat session's), ``recorded_outcome``. Turns are
    projected to role + content only and the system-role entries are
    dropped (they duplicate payload.instructions). From the raw payload,
    contact-named keys are dropped at any depth and ``facts_quiet*`` keys
    (byte-for-byte duplicates) at the top; the rest — script, order
    fields, names — is what the rubrics judge against and is sent as is.
    Voice-only extras (per-turn pipecat metrics collapsed to the aggregate
    a latency rubric can cite, errors, node traversal) come out empty for
    a chat; ``ended_by`` is the call's ``call_ended_by`` or the chat
    session's ``ended_reason``.
    """
    channel = context.get("channel")
    payload = context.get("payload") or {}
    meta = context.get("meta_data") or {}
    transcript = context.get("transcript") or []

    clean_payload = {
        key: _strip_contacts(value)
        for key, value in payload.items()
        if not key.startswith("facts_quiet") and not _is_contact_key(key)
    }
    transcription = [
        {"role": message.get("role"), "content": message.get("content")}
        for message in transcript
        if isinstance(message, dict) and message.get("role") != "system"
    ]

    raw_metrics = meta.get("pipecat_metrics") or []
    llm_totals = sorted(
        turn["summary"]["llm_total_ms"]
        for turn in raw_metrics
        if isinstance(turn, dict)
        and isinstance(turn.get("summary"), dict)
        and turn["summary"].get("llm_total_ms") is not None
    )

    return {
        "channel": channel,
        "recorded_outcome": context.get("recorded_outcome"),
        "extracted_payload": clean_payload,
        "conversation": {
            "transcription": transcription,
            "ended_by": meta.get("call_ended_by") or meta.get("ended_reason"),
            "errors": meta.get("errors") or [],
            "node_traversal": meta.get("node_traversal"),
            "metrics": {
                "turns": len(raw_metrics),
                "llm_total_ms_median": (
                    llm_totals[len(llm_totals) // 2] if llm_totals else None
                ),
                "llm_total_ms_max": llm_totals[-1] if llm_totals else None,
            },
        },
    }


# --- the stored structure: one shape for every engine ------------------------
# A verdict is a list of typed results, one per question, discriminated on
# ``kind``. ``key`` is the question's key in the configuration, ``label`` its
# display name from the same configuration. A reader does ``result[*].value``
# for everything and looks at ``kind`` only when the scale matters. ``value``
# is None when the judge gave nothing usable — never a guess, never a missing
# key.


class ScoreResult(BaseModel):
    kind: Literal["score"] = "score"
    key: str
    label: str
    value: Optional[float]
    # the scale, so a value reads good or bad without the configuration
    min: float
    max: float
    confidence: Optional[float] = None


class ChoiceResult(BaseModel):
    kind: Literal["choice"] = "choice"
    key: str
    label: str
    # one of the rubric's options
    value: Optional[str]
    confidence: Optional[float] = None


class NoulResult(BaseModel):
    # a yes/no question; the same word as the question primitive
    kind: Literal["noul"] = "noul"
    key: str
    label: str
    # P(yes) in 0..1 — the value is the answer, there is no separate confidence
    value: Optional[float]


Result = Annotated[
    Union[ScoreResult, ChoiceResult, NoulResult], Field(discriminator="kind")
]


class Verdict(BaseModel):
    """What an engine's ``transform`` returns and the adapter stores (plus
    ``type``, which the result column and its identity CHECK require).
    One shape for every engine, structured or prompt-based."""

    engine: str
    provider: str
    model: str
    result: List[Result]
