"""Plan enforcement — the harness holds the plan (RFC-002 Decision 4).

Production consensus (research/plan-enforcement-20260729-1403): deviation is
prevented STRUCTURALLY, never by prompting harder. When the model declares a
``<plan>`` (2+ steps), the parsed step list becomes harness-held turn state:

- Per cycle, the Gemini function-calling config is constrained to
  ``{current step's tool, revise_plan}`` — off-plan calls are impossible at
  the API layer.
- ``revise_plan(steps, reason)`` is the ONLY way off the plan → an explicit,
  observable event (plan_updated SSE; the step rail updates honestly).
- A step completes only when its (deterministic) verifiers passed — i.e. the
  result was not an error envelope. Failure → one retry of the same step →
  then ``revise_plan`` is REQUIRED.
- Single-tool turns never construct a plan, so they skip this machinery
  entirely — no rigidity tax on simple asks.

The enforcer never raises and fails open: a plan naming unknown tools stays
advisory (UX skeleton lines only, no constraint) — exactly today's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from app.core.logger import logger


@dataclass
class PlanEnforcer:
    """Turn-scoped plan state. Construct per turn; ``start()`` on plan
    emission; consult ``allowed_names()`` before each LLM cycle."""

    steps: List[str] = field(default_factory=list)
    cursor: int = 0
    active: bool = False
    retried_current: bool = False
    revise_required: bool = False

    def start(self, steps: List[str], known_tools: Set[str]) -> bool:
        """Arm enforcement for ``steps``. Returns True when armed.

        Not armed (plan stays advisory) when: fewer than 2 steps (single-tool
        turns stay free), or any step names a tool that doesn't exist this
        turn (a hallucinated plan must not brick the turn by constraining to
        an uncallable name).
        """
        if len(steps) < 2:
            return False
        unknown = [s for s in steps if s not in known_tools]
        if unknown:
            logger.warning(
                f"plan_enforcer: plan names unknown tools {unknown}; "
                "staying advisory"
            )
            return False
        self.steps = list(steps)
        self.cursor = 0
        self.active = True
        self.retried_current = False
        self.revise_required = False
        return True

    @property
    def constraining(self) -> bool:
        """True while a cycle must be constrained (plan armed, steps left)."""
        return self.active and self.cursor < len(self.steps)

    @property
    def current_step(self) -> Optional[str]:
        if self.active and self.cursor < len(self.steps):
            return self.steps[self.cursor]
        return None

    def allowed_names(self, revise_tool: str) -> List[str]:
        """The function names this cycle may call."""
        if self.revise_required:
            return [revise_tool]
        current = self.current_step
        if current is None:
            return []
        # Dedup while preserving "current first" for readability in logs.
        return [current] + ([revise_tool] if revise_tool != current else [])

    def on_tool_result(self, tool_name: str, success: bool) -> None:
        """Advance / retry bookkeeping after one dispatched call.

        ``success`` = the post-pipeline result passed verification (not an
        error envelope) — the deterministic gate owns "step complete", not
        the model's say-so.
        """
        if not self.constraining or tool_name != self.current_step:
            return
        if success:
            self.cursor += 1
            self.retried_current = False
            self.revise_required = False
        elif self.retried_current:
            self.revise_required = True
        else:
            self.retried_current = True

    def revise(self, new_steps: List[str], known_tools: Set[str]) -> List[str]:
        """Replace the REMAINING steps. Returns the full effective plan.

        Unknown tool names are dropped (fail-open). An empty remainder ends
        the plan (the model finishes with prose / the forced UI think-step).
        """
        remaining = [s for s in new_steps if s in known_tools]
        self.steps = self.steps[: self.cursor] + remaining
        self.retried_current = False
        self.revise_required = False
        if not self.constraining:
            self.active = bool(self.steps[: self.cursor])
        return list(self.steps)


__all__ = ["PlanEnforcer"]
