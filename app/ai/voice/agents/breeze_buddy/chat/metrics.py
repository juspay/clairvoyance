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
                f"drop_reasons={reasons}"
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = ["TurnMetrics"]
