"""JevEngine — CONVERSATION_EVALS judgment through the TypeSafe System One API.

The engine is a thin sandwich around two PURE functions: ``build_state``
(the projection the model sees — nothing more) and ``decide`` (answers in,
verdict out). The questions and pinned model arrive in the
``configuration`` argument from the agent's evaluation_config row; this
module hardcodes no definition and names no question. Both pure halves
are golden-tested with recorded data — the model is never in a unit test.
"""

from typing import Any, Dict, List, Mapping, Optional

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.base import (
    RESULT_TYPE,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
    PROVIDERS,
    ConversationEvalsProvider,
)
from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel

# Payload keys whose lowercased name contains one of these never leave the
# process, at any depth — the judgment never needs a contact value.
_CONTACT_KEY_MARKERS = ("phone", "mobile", "email", "msisdn", "whatsapp", "contact")

# System One's question primitives — Jev's vocabulary, so validated here
# and not by the type-level validator.
_ALLOWED_QUESTION_TYPES = {"score", "choice", "noul"}


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


def build_state(
    payload: Optional[Dict[str, Any]],
    meta_data: Optional[Dict[str, Any]],
    recorded_outcome: Optional[str],
    transcript: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """PURE gather: the projection Jev sees, nothing more.

    ``transcript`` is the worker's normalized turn list — the same source
    for VOICE and CHAT. Turns are projected to role + content only and the
    system-role entries are dropped (they duplicate payload.instructions).
    From the raw lead payload, contact-named keys are dropped at any depth
    and ``facts_quiet*`` keys (byte-for-byte duplicates) at the top; the
    rest of the payload — script, order fields, names — is what the rubrics
    judge the call against and is sent as is. Raw per-turn pipecat metrics
    collapse to the aggregate a latency or loop rubric can cite. Voice-only
    extras (metrics, call_ended_by, errors, node_traversal) simply come out
    empty for chat.
    """
    clean_payload = {
        key: _strip_contacts(value)
        for key, value in (payload or {}).items()
        if not key.startswith("facts_quiet") and not _is_contact_key(key)
    }

    meta = meta_data or {}
    transcription = [
        {"role": message.get("role"), "content": message.get("content")}
        for message in (transcript or [])
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
        "recorded_outcome": recorded_outcome,
        "extracted_payload": clean_payload,
        "call_logs": {
            "transcription": transcription,
            "call_ended_by": meta.get("call_ended_by"),
            "errors": meta.get("errors") or [],
            "node_traversal": meta.get("node_traversal"),
            "pipecat_metrics": {
                "turns": len(raw_metrics),
                "llm_total_ms_median": (
                    llm_totals[len(llm_totals) // 2] if llm_totals else None
                ),
                "llm_total_ms_max": llm_totals[-1] if llm_totals else None,
            },
        },
    }


def decide(
    answers: Dict[str, Dict[str, Any]],
    configuration: Dict[str, Any],
) -> Dict[str, Any]:
    """PURE: answers in, the verdict row's metadata out. No I/O.

    Names no question. Every question in the configuration gets one
    concise entry, by its primitive:
      score  -> {score, confidence}   (a 2-level rubric is binary and stays
                                       0-1; longer rubrics map level+1 onto
                                       the historical 1-10 scale)
      choice -> {choice, confidence}  (the option picked)
      noul   -> {noul}                (P(yes) in 0-1; a noul has no separate
                                       confidence — the value is the answer)
    Everything else Jev returns (legend, raw position, probability
    distribution) is dropped: the legend is the config's own text, the
    rest is summarised by the value + confidence. ``type`` must equal
    ``RESULT_TYPE`` — the evaluation_result identity CHECK compares it
    against ``result``.
    """
    stored: Dict[str, Dict[str, Any]] = {}
    for question_id, question in configuration["questions"].items():
        answer = answers.get(question_id) or {}
        question_type = question.get("type")
        if question_type == "noul":
            stored[question_id] = {"noul": answer.get("noul")}
            continue
        if question_type == "choice":
            entry: Dict[str, Any] = {"choice": answer.get("choice")}
        else:
            raw = answer.get("score")
            binary = len(question.get("criteria") or []) == 2
            entry = {
                "score": (
                    round(raw if binary else raw + 1, 2) if raw is not None else None
                )
            }
        entry["confidence"] = answer.get("confidence")
        stored[question_id] = entry

    return {
        "type": RESULT_TYPE,
        "engine": configuration.get("engine"),
        "provider": configuration.get("provider"),
        "model": configuration.get("model"),
        "answers": stored,
    }


class JevEngine:
    name = "jev"
    channels = frozenset({ConversationChannel.VOICE, ConversationChannel.CHAT})
    providers: Mapping[str, ConversationEvalsProvider] = {
        "typesafe": PROVIDERS["typesafe"]
    }

    def validate_configuration(self, configuration: Dict[str, Any]) -> None:
        """Jev's rules: questions in System One's primitives — score rubrics
        of 2-10 described levels, choice maps with every option described
        (undescribed options measurably collapse), noul (a yes/no question;
        optional criteria = {"true": ..., "false": ...} descriptions). No
        question is known by name. Raises ``ValueError``."""
        questions = configuration.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
        for question_id, question in questions.items():
            if not isinstance(question, dict):
                raise ValueError(f"question {question_id!r} must be an object")
            question_type = question.get("type")
            if (
                not isinstance(question_type, str)
                or question_type not in _ALLOWED_QUESTION_TYPES
            ):
                raise ValueError(
                    f"question {question_id!r}: type must be one of "
                    f"{sorted(_ALLOWED_QUESTION_TYPES)}"
                )
            instructions = question.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                raise ValueError(
                    f"question {question_id!r}: instructions must be a "
                    f"non-empty string"
                )
            criteria = question.get("criteria")
            if question_type == "score":
                if (
                    not isinstance(criteria, list)
                    or not 2 <= len(criteria) <= 10
                    or not all(
                        isinstance(level, str) and level.strip() for level in criteria
                    )
                ):
                    raise ValueError(
                        f"question {question_id!r}: score criteria must be "
                        f"2-10 non-empty strings"
                    )
            elif question_type == "choice":
                if not isinstance(criteria, dict) or not criteria:
                    raise ValueError(
                        f"question {question_id!r}: choice criteria must be "
                        f"a non-empty object of option -> description"
                    )
                for option, description in criteria.items():
                    if not isinstance(description, str) or not description.strip():
                        raise ValueError(
                            f"question {question_id!r}: option {option!r} "
                            f"needs a description"
                        )
            elif question_type == "noul" and criteria is not None:
                if (
                    not isinstance(criteria, dict)
                    or set(criteria) - {"true", "false"}
                    or not all(
                        isinstance(text, str) and text.strip()
                        for text in criteria.values()
                    )
                ):
                    raise ValueError(
                        f"question {question_id!r}: noul criteria, if given, must "
                        f"be an object with 'true'/'false' descriptions"
                    )

    async def evaluate(
        self,
        context: Dict[str, Any],
        configuration: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = build_state(
            context.get("payload"),
            context.get("meta_data"),
            context.get("recorded_outcome"),
            context.get("transcript"),
        )
        # the row picked the vendor; the validator guaranteed it is one of ours
        provider = self.providers[configuration["provider"]]
        body = await provider.judge(
            state, configuration["questions"], configuration["model"]
        )
        verdict = decide(body.get("answers") or {}, configuration)
        # the model that actually served (the requested one is in the config row)
        verdict["model"] = body.get("model") or verdict["model"]
        return verdict
