"""Structured-output schema for the single-turn handler.

The turn LLM emits exactly one :class:`TurnDecision` per user turn.
No specialists — the LLM writes flow, prompts, payload schema, and
configs directly via ``draft_patch``.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, create_model

from app.ai.text.agents.blueprint.agent.schema_view import build_schema_view


class TurnDecision(BaseModel):
    """One turn's worth of decisions from the single-agent LLM."""

    message_to_user: str = Field(
        default="",
        description=(
            "Plain-text reply shown to the user. Empty string = silent turn "
            "(the handler skips emitting a message)."
        ),
    )
    draft_patch: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Partial draft to deep-merge into the template. Use nested "
            "dicts, NOT dotted string keys. Example: "
            '{"configurations": {"stt_configuration": {"provider": "soniox"}}}. '
            "Can include flow, expected_payload_schema, or any template field."
        ),
    )
    completed_groups: list[str] = Field(
        default_factory=list,
        description=(
            "Askable groups now done — you got a meaningful answer (or applied "
            "a default the user accepted). Appended to state; duplicates de-duped."
        ),
    )
    skipped_groups: list[str] = Field(
        default_factory=list,
        description=(
            "Askable groups the user has no requirement for (e.g., outbound-only "
            "template → skip 'warm_transfer'; user declined idle handling → skip "
            "'user_idle'). Use this INSTEAD of completed_groups when the user is "
            "opting out, not opting in. Appended to state; duplicates de-duped."
        ),
    )
    pending_approval_for: Optional[str] = Field(
        default=None,
        description=(
            "Group name for the UI approval bar. null = chat input shows. "
            "Only set when you wrote values in draft_patch AND want explicit "
            "sign-off on a non-trivial choice."
        ),
    )
    finalize: bool = Field(
        default=False,
        description=(
            "Run the linter + assembler. On success template_json is set. "
            "On failure validation_issues are populated for the next turn."
        ),
    )
    terminal: bool = Field(
        default=False,
        description=(
            "Session is done. Set after finalize succeeds and you've shown "
            "the success message."
        ),
    )


def build_turn_decision_schema() -> type[BaseModel]:
    """Build a TurnDecision with group-name enums so the LLM can't hallucinate."""
    view = build_schema_view()
    group_names = tuple(g.name for g in view.groups)
    if not group_names:
        return TurnDecision

    group_enum = Literal[group_names]  # type: ignore[valid-type]

    return create_model(
        "TurnDecisionConstrained",
        message_to_user=(
            str,
            Field(
                default="",
                description=TurnDecision.model_fields["message_to_user"].description,
            ),
        ),
        draft_patch=(
            dict[str, Any],
            Field(
                default_factory=dict,
                description=TurnDecision.model_fields["draft_patch"].description,
            ),
        ),
        completed_groups=(
            list[group_enum],  # type: ignore[valid-type]
            Field(
                default_factory=list,
                description=TurnDecision.model_fields["completed_groups"].description,
            ),
        ),
        skipped_groups=(
            list[group_enum],  # type: ignore[valid-type]
            Field(
                default_factory=list,
                description=TurnDecision.model_fields["skipped_groups"].description,
            ),
        ),
        pending_approval_for=(
            Optional[group_enum],  # type: ignore[valid-type]
            Field(
                default=None,
                description=TurnDecision.model_fields[
                    "pending_approval_for"
                ].description,
            ),
        ),
        finalize=(
            bool,
            Field(
                default=False,
                description=TurnDecision.model_fields["finalize"].description,
            ),
        ),
        terminal=(
            bool,
            Field(
                default=False,
                description=TurnDecision.model_fields["terminal"].description,
            ),
        ),
    )


def coerce_to_decision(raw: Union[BaseModel, dict[str, Any]]) -> TurnDecision:
    if isinstance(raw, TurnDecision):
        return raw
    data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
    return TurnDecision.model_validate(data)


__all__ = ["TurnDecision", "build_turn_decision_schema", "coerce_to_decision"]
