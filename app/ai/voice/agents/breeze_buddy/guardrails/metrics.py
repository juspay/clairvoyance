"""Bounded, content-free Guardrail metrics for one conversation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .evaluator import GuardrailVerdict
from .types import GuardrailsConfig

GuardrailMetricsDirection = Literal["input", "output"]
_MAX_EVIDENCE_TURNS = 20


@dataclass
class DirectionMetrics:
    evaluated: int = 0
    allowed: int = 0
    blocked: int = 0
    failed_closed: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    reason_counts: Counter[str] = field(default_factory=Counter)
    evidence_turns: list[int] = field(default_factory=list)

    def record(self, verdict: GuardrailVerdict, turn_number: int) -> None:
        self.evaluated += 1
        self.total_latency_ms += max(0.0, verdict.latency_ms)
        self.max_latency_ms = max(self.max_latency_ms, verdict.latency_ms)
        if verdict.blocked:
            self.blocked += 1
            if verdict.evaluation_failed:
                self.failed_closed += 1
                reason_code = "evaluation_unavailable"
            elif verdict.deterministic_rule_id:
                reason_code = verdict.deterministic_rule_id
            else:
                # Model-authored reasons can contain candidate content. Store
                # only a stable code; the raw reason remains in runtime logs.
                reason_code = "configured_policy"
            self.reason_counts[reason_code] += 1
            if (
                turn_number not in self.evidence_turns
                and len(self.evidence_turns) < _MAX_EVIDENCE_TURNS
            ):
                self.evidence_turns.append(turn_number)
        else:
            self.allowed += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "failed_closed": self.failed_closed,
            "total_latency_ms": round(self.total_latency_ms, 3),
            "max_latency_ms": round(self.max_latency_ms, 3),
            "reason_counts": dict(sorted(self.reason_counts.items())),
            "evidence_turns": list(self.evidence_turns),
        }


@dataclass
class GuardrailSessionMetrics:
    evaluation_config_id: str
    configuration_revision: str
    template_id: str
    channel: Literal["CHAT", "VOICE"]
    focus_enabled: bool
    input: DirectionMetrics = field(default_factory=DirectionMetrics)
    output: DirectionMetrics = field(default_factory=DirectionMetrics)
    last_turn_number: int = 0

    @property
    def has_evaluations(self) -> bool:
        return bool(self.input.evaluated or self.output.evaluated)

    @property
    def should_persist(self) -> bool:
        return self.focus_enabled or self.has_evaluations

    def record(
        self,
        direction: GuardrailMetricsDirection,
        verdict: GuardrailVerdict,
        turn_number: int,
    ) -> None:
        self.last_turn_number = max(self.last_turn_number, turn_number)
        getattr(self, direction).record(verdict, turn_number)

    def as_result(self) -> dict[str, Any]:
        return {
            "type": "SESSION_SUMMARY",
            "schema_version": 1,
            "evaluation_config_id": self.evaluation_config_id,
            "configuration_revision": self.configuration_revision,
            "template_id": self.template_id,
            "channel": self.channel,
            "focus_enabled": self.focus_enabled,
            "input": self.input.as_dict(),
            "output": self.output.as_dict(),
        }


def build_session_metrics(
    *,
    evaluation_config_id: Optional[str],
    template_id: str,
    channel: Literal["CHAT", "VOICE"],
    focus_enabled: bool,
    configuration_revision: Optional[str] = None,
) -> Optional[GuardrailSessionMetrics]:
    if evaluation_config_id is None:
        return None
    return GuardrailSessionMetrics(
        evaluation_config_id=evaluation_config_id,
        configuration_revision=(
            configuration_revision or f"legacy:{evaluation_config_id}"
        ),
        template_id=template_id,
        channel=channel,
        focus_enabled=focus_enabled,
    )


def resolve_session_metrics(
    guardrails: GuardrailsConfig,
    *,
    template_id: str,
    channel: Literal["CHAT", "VOICE"],
    existing: Optional[GuardrailSessionMetrics] = None,
) -> Optional[GuardrailSessionMetrics]:
    """Reuse call-owned metrics or create the channel's session accumulator."""
    if existing is not None:
        return existing
    return build_session_metrics(
        evaluation_config_id=guardrails.evaluation_config_id,
        configuration_revision=guardrails.configuration_revision,
        template_id=template_id,
        channel=channel,
        focus_enabled=guardrails.focus.enabled,
    )


__all__ = [
    "GuardrailMetricsDirection",
    "GuardrailSessionMetrics",
    "build_session_metrics",
    "resolve_session_metrics",
]
