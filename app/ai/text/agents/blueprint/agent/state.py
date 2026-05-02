"""Graph state schema — the single source of truth for what flows
between nodes and what the router/handlers can read.

v2 (single-turn architecture): replaced ``current_group`` + rich
``pending_approval`` object with a thin ``pending_approval_for``
group-name flag. The turn LLM tracks "what we're talking about"
implicitly through the transcript; the UI only needs to know whether
to render the approval bar.

v2.1 (runtime context): the four session-fixed fields (``mode``,
``reseller_id``, ``existing_template_id``, ``available_outbound_numbers``)
were lifted out of state into :class:`BlueprintContext` — they're set
once per session and shouldn't bloat every checkpoint snapshot.

NOTE: changing the state schema is a one-way migration. In-flight
sessions persisted under the old ``BlueprintState`` shape (with the
four lifted fields still present as channels) will fail to resume. The
Blueprint feature is gated behind an admin flag, so the cost is bounded
— operators should expect to start any open chats over after the rollout.
We deliberately do NOT dual-read the old fields off state.
"""

from dataclasses import dataclass, field
from typing import Annotated, Any, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from pydantic import BaseModel, Field


class BlueprintState(BaseModel):
    """Blueprint graph state.

    All fields default to an empty / ``None`` value so partial dicts can be
    used as updates and the first turn can pass only the minimum seed state.
    """

    # --- Conversation ---
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)

    # --- Draft being built ---
    draft: dict[str, Any] = Field(default_factory=dict)

    # --- Progress / UI flags ---
    completed_groups: list[str] = Field(default_factory=list)
    """Askable groups the LLM has gathered a meaningful answer for."""
    skipped_groups: list[str] = Field(default_factory=list)
    """Askable groups the user has no requirement for (e.g., outbound-only
    template skipping warm_transfer). Distinct from ``completed_groups`` so
    validation can tell ``user explicitly declined`` apart from ``we forgot
    to ask``. Auto-skips driven by structural rules (``should_skip_group``)
    do NOT land here — they stay invisible to the user."""
    pending_approval_for: Optional[str] = None
    """Group name the UI approval bar should render for. None hides it."""

    # --- Validation (refreshed every tick before the LLM call) ---
    validation_issues: list[str] = Field(default_factory=list)

    # --- Final output (only set on successful finalize) ---
    template_json: Optional[dict[str, Any]] = None

    # --- Transient: number of consecutive failed finalize attempts ---
    finalize_retries: int = 0
    """The handler caps automatic finalize retries at 1; on a second
    failure we surface errors to the user and stop trying.
    """


@dataclass
class BlueprintContext:
    """Session-fixed runtime context — set once per Blueprint session.

    Lives outside ``BlueprintState`` so the LangGraph checkpointer doesn't
    persist these on every tick. Wired to the graph via
    ``StateGraph(..., context_schema=BlueprintContext)`` and passed at
    invocation time as ``agent.ainvoke(..., context=BlueprintContext(...))``
    (added in langgraph 0.6, see ``langgraph.runtime.Runtime``).

    Each field mirrors the per-session value the handler used to seed onto
    the state's first turn — read inside the node via ``runtime.context``.
    """

    mode: str = "create"  # "create" | "edit"
    reseller_id: str = ""
    existing_template_id: Optional[str] = None
    available_outbound_numbers: list[dict[str, Any]] = field(default_factory=list)
    """Populated on session creation from the DB. Each entry is
    ``{"id": "<uuid>", "number": "+91...", "provider": "twilio"}``.
    The LLM auto-picks if there's exactly one; asks if multiple.
    """
