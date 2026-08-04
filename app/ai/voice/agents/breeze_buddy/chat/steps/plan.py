"""Plan-as-emission — the ``<plan>`` marker parser (Phase 2).

The model may declare its tool plan for a multi-step turn by emitting

    <plan>["search_catalog","update_cart"]</plan>

on its own line before its first tool call — a JSON array of tool names
in intended execution order. The stream extractor below strips the block
from the user-facing prose (like ``<ui_stream>``) and surfaces the parsed
plan so the agent emits ``plan_started`` / ``plan_updated`` SSE events;
the widget renders pending skeleton lines that real step events then
claim and check off.

Tolerant by design: a malformed block (bad JSON, non-string entries,
absurd length) is DROPPED silently — a broken plan must never damage the
prose stream or the turn. The model's plan is UX-advisory only; execution
truth stays with the step events.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

_OPEN = "<plan>"
_CLOSE = "</plan>"
_MAX_PLAN_STEPS = 8
_MAX_BLOCK_CHARS = 2000  # runaway-open guard: past this, flush as prose


class PlanExtractor:
    """Streaming scanner for ``<plan>…</plan>`` blocks in assistant text.

    ``feed(chunk)`` returns ``(visible_text, plans)`` — the chunk with any
    complete plan blocks removed (a partial marker at the chunk boundary
    is held back, same carry idiom as ``UiStreamExtractor``), plus every
    complete, well-formed plan parsed from it. ``flush()`` releases any
    held tail (an unterminated open block is dropped with its content —
    it never reaches the user).
    """

    def __init__(self) -> None:
        self._carry = ""
        self._in_block = False

    def feed(self, chunk: str) -> Tuple[str, List[List[str]]]:
        text = self._carry + chunk
        self._carry = ""
        visible: List[str] = []
        plans: List[List[str]] = []

        while text:
            if self._in_block:
                close = text.find(_CLOSE)
                if close == -1:
                    if len(text) > _MAX_BLOCK_CHARS:
                        # Runaway open — treat the whole thing as prose so
                        # the shopper never loses real output to a stray
                        # marker the model emitted by accident.
                        visible.append(_OPEN + text)
                        self._in_block = False
                        text = ""
                    else:
                        self._carry = text
                        text = ""
                    continue
                body, text = text[:close], text[close + len(_CLOSE) :]
                self._in_block = False
                plan = _parse_plan(body)
                if plan is not None:
                    plans.append(plan)
                continue

            open_at = text.find(_OPEN)
            if open_at == -1:
                keep, held = _split_partial_marker(text, (_OPEN,))
                visible.append(keep)
                self._carry = held
                text = ""
                continue
            visible.append(text[:open_at])
            text = text[open_at + len(_OPEN) :]
            self._in_block = True

        return "".join(visible), plans

    def flush(self) -> str:
        """End of stream: release held prose. An unterminated open block's
        body is dropped (it was never valid output)."""
        if self._in_block:
            self._in_block = False
            self._carry = ""
            return ""
        carry, self._carry = self._carry, ""
        return carry


def _split_partial_marker(text: str, markers: Tuple[str, ...]) -> Tuple[str, str]:
    """Hold back a chunk-boundary prefix of any marker (e.g. ``<pl``)."""
    longest = max(len(m) for m in markers)
    for i in range(min(longest - 1, len(text)), 0, -1):
        tail = text[-i:]
        if any(m.startswith(tail) for m in markers):
            return text[:-i], tail
    return text, ""


def _parse_plan(body: str) -> Optional[List[str]]:
    try:
        parsed = json.loads(body.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    steps = [s for s in parsed if isinstance(s, str) and 0 < len(s) <= 64]
    if not steps or len(steps) != len(parsed):
        return None
    return steps[:_MAX_PLAN_STEPS]


__all__ = ["PlanExtractor"]
