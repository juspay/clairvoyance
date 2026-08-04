"""Per-turn UI-generation metrics for chat mode.

Phase 0 of ``docs/widget/UI_FAST_RELIABLE_GENERIC_PLAN.md`` — the measurement
foundation. A :class:`TurnMetrics` *observes* the SSE event stream a chat turn
already emits (it never changes it) and logs one structured ``[CHAT_METRICS]``
line at turn end. Everything it records is structural — timings, counts,
op/drop classifications, byte sizes — and never user or tool payload content,
so it respects the privacy-by-design logging contract.

The two signals that drive the reliability + speed work:

  * ``ui_dropped`` / ``drop_reasons`` — how often the healer/validator
    discards an LLM-emitted op (the "missing Handoff" class of failure).
  * ``ttfui_ms`` / ``ttlui_ms`` — time to first / last rendered op
    (perceived + total UI latency).

Query in OpenObserve with ``match_all_raw('[CHAT_METRICS]')`` and parse the
``key=value`` tokens; tag before/after comparisons by the ``phase=`` field as
each optimisation lands.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.core.logger import logger

# Cap distinct drop reasons so a pathological turn can't grow the log line
# unbounded.
_MAX_DROP_REASONS = 20

# Cap persisted per-drop evidence entries (chat_turn_metrics.drops). Sized to
# the real distribution — observed turns drop 0-1 ops; 10 already means the
# turn is pathological and the first 10 tell the story.
_MAX_DROP_DETAILS = 10


class TurnMetrics:
    """Accumulates structural metrics for one chat turn.

    Construct with a monotonic ``t0`` (the caller picks the start point —
    today: the moment ``agent.run_turn`` begins, i.e. UI-generation latency).
    Feed every SSE event through :meth:`observe`, then call :meth:`emit` once
    (idempotent) in the request's ``finally`` so it fires on the normal,
    cancelled, and crashed paths alike.
    """

    def __init__(
        self,
        *,
        session_id: str,
        template_id: Optional[str],
        t0: float,
        phase: str = "baseline",
    ) -> None:
        self.session_id = session_id
        self.template_id = template_id
        self.t0 = t0
        self.phase = phase
        self.ttft_ms: Optional[float] = None  # first assistant_token (prose)
        self.ttfui_ms: Optional[float] = None  # first ui_op (rendered UI)
        self.ttlui_ms: Optional[float] = None  # last ui_op
        self.total_ms: Optional[float] = None  # turn total, stamped at emit()
        self.ui_ops = 0
        self.ui_dropped = 0
        self.healer_applied = 0
        self.tool_calls = 0
        self.prose_chars = 0
        self.ui_chars = 0
        self.drop_reasons: List[str] = []
        # Per-drop evidence for chat_turn_metrics.drops (migration 041):
        # [{"sig": {...}, "reason": str, "raw": str}]. ``raw`` is the dropped
        # line itself — transcript-class content, persisted to the DB beside
        # the session, NEVER echoed into the [CHAT_METRICS] log line.
        self.drops: List[dict] = []
        # Step-progress / plan exposure (Phase 2 A/B instrumentation):
        # which turns SHOWED progress affordances and how they resolved.
        # Join against session outcomes (abandonment, add-to-cart) in the
        # analytics layer — the metrics line is the per-turn exposure
        # record.
        self.steps_started = 0
        self.steps_completed = 0
        self.steps_errored = 0
        self.first_step_ms: Optional[float] = None
        self.last_step_ms: Optional[float] = None
        self.plans_emitted = 0
        self.plan_steps = 0  # declared length of the LAST plan
        # RFC-002 render_ui observability: every UI decision is an explicit
        # event now — calls, reasoned no-renders, and forced-cycle fallbacks
        # (MALFORMED_FUNCTION_CALL retry) are all countable per turn.
        self.render_ui_calls = 0
        self.ui_no_ui = 0
        self.force_fallbacks = 0
        self.status: Optional[str] = None
        # The assistant chat_message.idx this turn produced (from the
        # turn_end event). Keys the persisted chat_turn_metrics row
        # (migration 032) so the conversational-log UI can join latency to
        # the message. None on turns with no assistant row (failed/canceled
        # before any reply).
        self.assistant_idx: Optional[int] = None
        self._emitted = False

    def _ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000, 1)

    def observe(self, event: SSEEvent) -> None:
        """Record one SSE event. Never raises — telemetry must not break a turn."""
        try:
            name = event.event
            data = event.data if isinstance(event.data, dict) else {}
            if name == "assistant_token":
                if self.ttft_ms is None:
                    self.ttft_ms = self._ms()
                delta = data.get("delta")
                if isinstance(delta, str):
                    self.prose_chars += len(delta)
            elif name == "ui_op":
                now = self._ms()
                if self.ttfui_ms is None:
                    self.ttfui_ms = now
                self.ttlui_ms = now
                self.ui_ops += 1
                op = data.get("op")
                if op is not None:
                    self.ui_chars += len(json.dumps(op, default=str))
            elif name == "ui_op_dropped":
                self.ui_dropped += 1
                reason = data.get("reason")
                if len(self.drops) < _MAX_DROP_DETAILS:
                    raw = data.get("raw")
                    self.drops.append(
                        {
                            "sig": data.get("op"),
                            "reason": reason if isinstance(reason, str) else None,
                            "raw": raw if isinstance(raw, str) else None,
                        }
                    )
                if (
                    isinstance(reason, str)
                    and len(self.drop_reasons) < _MAX_DROP_REASONS
                ):
                    # First line only + hard truncate: validation errors lead
                    # with a structural "N validation errors for <Model>" line;
                    # any echoed input_value lives on later lines we drop, so
                    # no payload content reaches the log. Whitespace is
                    # collapsed to "_" so the value stays a single space-free
                    # token in the key=value [CHAT_METRICS] line (parseable).
                    first_line = reason.split("\n", 1)[0]
                    self.drop_reasons.append("_".join(first_line.split())[:80])
            elif name == "healer_applied":
                self.healer_applied += 1
            elif name == "function_call_started":
                self.tool_calls += 1
                if data.get("name") == "render_ui":
                    self.render_ui_calls += 1
            elif name == "ui_decision":
                if data.get("decision") == "no_ui":
                    self.ui_no_ui += 1
            elif name == "force_fallback":
                self.force_fallbacks += 1
            elif name == "step_started":
                if self.first_step_ms is None:
                    self.first_step_ms = self._ms()
                self.steps_started += 1
            elif name == "step_completed":
                self.last_step_ms = self._ms()
                self.steps_completed += 1
                if data.get("status") == "error":
                    self.steps_errored += 1
            elif name in ("plan_started", "plan_updated"):
                self.plans_emitted += 1
                steps_val = data.get("steps")
                if isinstance(steps_val, list):
                    self.plan_steps = len(steps_val)
            elif name == "turn_end":
                self.status = data.get("session_status")
                idx_val = data.get("assistant_idx")
                if isinstance(idx_val, int):
                    self.assistant_idx = idx_val
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass

    def emit(self) -> None:
        """Log the structured metrics line once. Idempotent + non-raising."""
        if self._emitted:
            return
        self._emitted = True
        try:
            self.total_ms = self._ms()
            reasons = ";".join(self.drop_reasons) if self.drop_reasons else "-"
            logger.info(
                "[CHAT_METRICS] "
                f"session={self.session_id} template={self.template_id} "
                f"phase={self.phase} status={self.status} "
                f"ttft_ms={self.ttft_ms} ttfui_ms={self.ttfui_ms} "
                f"ttlui_ms={self.ttlui_ms} total_ms={self.total_ms} "
                f"ui_ops={self.ui_ops} ui_dropped={self.ui_dropped} "
                f"healer_applied={self.healer_applied} tool_calls={self.tool_calls} "
                f"prose_chars={self.prose_chars} ui_chars={self.ui_chars} "
                f"steps_started={self.steps_started} "
                f"steps_completed={self.steps_completed} "
                f"steps_errored={self.steps_errored} "
                f"first_step_ms={self.first_step_ms} "
                f"last_step_ms={self.last_step_ms} "
                f"plans_emitted={self.plans_emitted} plan_steps={self.plan_steps} "
                f"render_ui_calls={self.render_ui_calls} ui_no_ui={self.ui_no_ui} "
                f"force_fallbacks={self.force_fallbacks} "
                f"drop_reasons={reasons}"
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = ["TurnMetrics"]
