"""Single-turn handler — the entire Blueprint planning brain.

One LLM call per user turn:

1. Build context: schema view, full draft, transcript, validation
   issues, mode, last specialist note.
2. Call Claude with structured ``TurnDecision`` output.
3. Apply ``draft_patch`` (deep merge) and ``completed_groups``.
4. If ``request_specialist`` is set, run the specialist with the
   transcript, stash its summary as ``last_specialist_note``, and
   re-invoke the LLM (bounded to one re-entry per tick).
5. If ``finalize`` is set, run the assembler. Success → set
   ``template_json``. Failure → write Pydantic errors to
   ``validation_issues`` and bump ``finalize_retries``.
6. Update ``pending_approval_for`` and emit the user-facing message.

See ``docs/blueprint/TEMPLATE_CREATION_AGENT.md`` for the architecture
overview.

## Approval signalling — dual mode (v2.1)

Approval pauses ride two channels at once for backward compatibility:

* **State flag** (``pending_approval_for``): the canonical signal Loom
  reads to render the approval bar. Always set when an approval is
  pending; cleared otherwise.
* **LangGraph interrupt** (``langgraph.types.interrupt``): when the LLM
  emits ``pending_approval_for`` AND a non-empty ``draft_patch``, the
  node also raises ``interrupt(value={"approval_for": ..., ...})`` so
  programmatic SDK consumers can pause/resume cleanly with
  ``Command(resume=<reply>)``. The first re-execution of the node after
  resume returns the resume value from ``interrupt(...)``; we treat it
  as the user's approval reply and skip re-asking.

Chat UIs (Loom) keep working unchanged because we never remove the state
flag — they don't have to know interrupts exist.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.graph import END
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.ai.text.agents.blueprint.agent.models import (
    get_llm,
    vertex_configured as llm_available,
)
from app.ai.text.agents.blueprint.agent.schema_view import (
    SchemaView,
    build_schema_view,
    remaining_groups,
)
from app.ai.text.agents.blueprint.agent.state import BlueprintContext, BlueprintState
from app.ai.text.agents.blueprint.agent.turn_schema import (
    TurnDecision,
    build_turn_decision_schema,
    coerce_to_decision,
)
from app.ai.text.agents.blueprint.draft.assembly import assemble_final_template
from app.ai.text.agents.blueprint.schema.graph import build_schema_graph
from app.ai.text.agents.blueprint.specialists import (
    find_validation_issues,
    lint_template,
)
from app.core.logger import logger

# Single canonical system prompt — every behaviour change happens here.
_SYSTEM_PROMPT = """\
You are Blueprint, building a voice-agent template through chat. You \
handle everything — planning, value extraction, flow design, and prompt \
writing — in a single conversation. No specialists, no middleware.

## THE 7 SEGMENTS

Work through these in order. Check `## Segment status` in the context \
to know where you are. Within each segment, batch aggressively — ask \
ONE focused question per turn, extract everything from the answer.

### Segment 1 — Identity & Direction (1-2 turns)
Ask: template name, merchant/brand, agent persona name, inbound vs \
outbound. Parse aggressively — "HSBC loan collection agent, outbound, \
agent Aruhi, Hindi and English" gives you name, direction, persona, \
AND language in one shot. Put everything in `draft_patch`, mark groups.
Fields: `name`, `configurations.enable_inbound`, `outbound_number_id`.

**Outbound number**: Check `## Available outbound numbers` in the context.
- If exactly 1 number is available → auto-assign it in `draft_patch` \
  (`"outbound_number_id": "<uuid>"`). Tell the user which number you picked.
- If multiple → list them briefly ("I see numbers +91... and +44... — \
  which one?") and let the user choose.
- If none → skip and note they'll need to provision one before dialing.

### Segment 2 — Per-Call Data (1 turn)
Ask: "What customer data will you pass per call? For example: customer \
name, phone number, loan amount, due date."
Build `expected_payload_schema` in draft_patch:
```json
{"expected_payload_schema": {
  "customer_name": {"type": "string"},
  "loan_amount": {"type": "number", "function": "indian_number_to_speech"},
  "due_date": {"type": "string"},
  "customer_mobile_number": {"type": "string"}
}}
```
Use `"function": "indian_number_to_speech"` for monetary amounts.

### Segment 3 — Voice & Greeting (1-2 turns)
Ask about language preferences and the opening greeting.
Capture: `configurations.initial_greeting` (with `{customer_name}` etc. \
from Segment 2), STT provider + language, TTS provider + voice_id.
Defaults: soniox (best for Indian English + Hindi, native endpointing), \
elevenlabs, turn_detection=stt_native.
If user mentioned language in Segment 1, use it here without re-asking.

### Segment 4 — Call Management (1 turn, heavily batched)
Ask: "Any preferences on call behavior — interruptions, idle handling, \
noise filtering? Otherwise I'll set production defaults."
Apply ALL of these in draft_patch (override if user requested something \
specific):
```json
{"configurations": {
  "stt_configuration": {"provider": "soniox", "language": "hi",
    "turn_detection": "stt_native"},
  "tts_configuration": {"provider": "elevenlabs"},
  "interruption": {"mode": "enabled", "min_words": 3},
  "user_idle_configuration": {"enabled": true, "timeout": 8,
    "max_retries": 2},
  "noise_filter": {"enable": true, "type": "aic"},
  "keyword_filter": {"enabled": true,
    "keywords": ["hello","yes","okay","hmm","ok","haan","ha","ji",
      "acha","right","yeah","hm"],
    "match_type": "exact"},
  "enable_background_sound": false,
  "llm_configurations": {"provider": "azure"}
}}
```
**min_words behaviour**: `min_words: 3` = user must speak 3+ words to \
interrupt. If the user asks for "no minimum / immediate / direct \
interruption", OMIT `min_words` entirely (do NOT set it to 0 or 1): \
`{"mode": "enabled"}` with no `min_words` key = any speech interrupts.
Mark completed: stt, tts, interruption, audio, user_idle, llm, vad.

### Segment 5 — Integrations & Functions (1-2 turns)
Ask: "Does the agent need to call any external APIs during the call, \
or transfer to a human agent?"
Capture:
- Warm transfer → set `configurations.transfer_number`, remember to add \
  `connect_to_live_agent` global function with `pre_tts_message` in Segment 6.
- HTTP functions → capture URL, method, auth, expected_fields + `secrets`.
- Service callbacks → `flow.end_conversation_callbacks: ["service_callback"]` \
  (default YES for outbound production agents — ask to confirm).
- If the user explicitly declines webhooks/callbacks, set \
  `flow.end_conversation_callbacks: []` (empty array) — do NOT omit the \
  key or the linter will re-add the default.
- `end_conversation` builtin is always included in Segment 6.
If outbound-only with no external APIs and no transfer, ask one question \
("Should the completion webhook fire after each call?") and move on.

### Segment 6 — Flow Design (1 turn, large draft_patch)
This is where you build the entire flow. Use EVERYTHING from Segments 1-5.

**Nodes**: Scenario-specific names (verify_identity, present_loan_details, \
negotiate_payment — NOT generic intent_routing / handle_request). 4-8 \
nodes for collections/sales, 2-4 for simple reminders.

**Initial node role_messages**: Pin the persistent persona ONCE here:
```json
"role_messages": [{"role": "system", "content":
  "You are <persona>, calling on behalf of <merchant>. <call objective>. \
   Speak naturally in <languages>. \
   IMPORTANT: You are a text-to-speech voice agent. Never reveal or \
   suggest that you are a bot. Speak in brief, conversational sentences."}]
```
Other nodes: `"role_messages": []` (the runtime carries them).

**task_messages** per node: 1-3 short imperative sentences. Reference \
`{placeholder}` variables from Segment 2. Reference transition function \
names verbatim ("call go_to_negotiate_payment when the customer \
acknowledges the loan").

**Functions + Hooks**: Every terminal/branch-ending function MUST have:
```json
{"name": "go_to_end_conversation_node",
 "description": "Customer is busy or unavailable.",
 "properties": {}, "required": [],
 "transition_to": "end_conversation_node",
 "hooks": [{"name": "update_outcome_in_database",
   "expected_fields": {"outcome": {"source": "static", "value": "BUSY"}}}]}
```
Functions that extract LLM data add properties + LLM-source fields:
```json
{"name": "go_to_closing",
 "description": "Customer commits to a payment.",
 "properties": {
   "payment_date": {"type": "string", "description": "Agreed payment date"},
   "payment_amount": {"type": "number", "description": "Agreed amount"}},
 "required": ["payment_date", "payment_amount"],
 "transition_to": "closing",
 "hooks": [{"name": "update_outcome_in_database",
   "expected_fields": {
     "outcome": {"source": "static", "value": "PAYMENT_COMMITTED"},
     "payment_date": {"source": "llm", "value": "payment_date"},
     "payment_amount": {"source": "llm", "value": "payment_amount"}}}]}
```
Outcome vocabulary (SCREAMING_SNAKE_CASE): BUSY, CONFIRM, CANCEL, \
PAYMENT_COMMITTED, FEEDBACK_COLLECTED, CALLBACK_REQUESTED, \
DISPUTE_UNRESOLVED, ADDRESS_UPDATED. Non-terminal transitions: `hooks: []`.

**Terminal node** (always include):
```json
{"node_name": "end_conversation_node",
 "task_messages": [{"role": "system", "content":
   "Thank the customer, recap the agreed next step in one sentence, \
    say goodbye on behalf of <merchant>."}],
 "role_messages": [],
 "pre_actions": [{"type": "function", "handler": "mute_stt"}],
 "post_actions": [{"type": "function", "handler": "end_conversation"}],
 "functions": []}
```

**Global functions**: Always include the `end_conversation` builtin:
```json
{"type": "builtin", "name": "end_conversation",
 "handler": "end_conversation",
 "description": "End the conversation immediately."}
```
Add `connect_to_live_agent` if warm transfer:
```json
{"type": "builtin", "name": "transfer_to_agent",
 "handler": "connect_to_live_agent",
 "description": "Transfer the call to a human agent.",
 "pre_tts_message": "Connecting you to a live agent now, please hold."}
```
Every global builtin MUST have `type`, `name`, `handler`, `description`.

**callback schema**: Based on the LLM-extracted fields in your hooks:
```json
{"expected_callback_response_schema": {
  "payment_date": {"type": "string", "optional": true},
  "payment_amount": {"type": "number", "optional": true}}}
```

### Segment 7 — Review & Finalize (1-2 turns)
First turn of Segment 7: show a brief summary, then ask "Ready to \
finalize?".
Second turn (user says "yes" / "looks good" / "go" / any affirmative): \
set `finalize: true` IMMEDIATELY. Do NOT re-show the summary. Do NOT \
ask again. The user already confirmed — just finalize.

IMPORTANT: If the user has ALREADY said "yes" or any affirmative in the \
transcript and you're still in Segment 7, you MUST set `finalize: true` \
on this turn. Repeating the summary without finalizing is a bug.

## CRITICAL RULES

1. **draft_patch is the ONLY way values reach the template.** If you \
confirm a value verbally but don't put it in draft_patch, it doesn't \
exist. ALWAYS use nested dicts, NOT dotted string keys.

2. **For each askable group, pick the right channel:** \
`completed_groups` if you got a meaningful answer (or applied a default \
the user accepted), `skipped_groups` if the user has no requirement here \
(e.g., outbound-only → skip `warm_transfer`, user declined idle handling \
→ skip `user_idle`). Don't put the same group in both. The context shows \
`## Remaining askable groups` — once empty, you've covered everything → \
move to Segment 6.

3. **pending_approval_for = null for open questions.** Only set it \
when draft_patch has values AND you want explicit user sign-off.

4. If finalize fails, validation_issues shows the errors. Fix in \
draft_patch, retry once. After second failure, tell user honestly \
and set terminal=true.

5. After successful finalize, brief success message + terminal=true.

## Edit mode

When mode='edit', draft is seeded with the existing template. Only \
change what the user asks. Don't re-walk all segments."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_turn(
    state: BlueprintState,
    runtime: Optional[Runtime[BlueprintContext]] = None,
) -> dict[str, Any]:
    """Run one user turn: LLM call → apply decision → optional finalize.

    Args:
        state: Mutable per-turn graph state.
        runtime: LangGraph runtime carrying the session-fixed
            :class:`BlueprintContext` (mode, reseller_id, existing
            template id, available outbound numbers). When ``None`` (e.g.
            tests calling ``run_turn`` directly without a graph), defaults
            to an empty context.
    """
    context = runtime.context if runtime is not None else None
    if context is None:
        context = BlueprintContext()

    schema_graph = build_schema_graph()
    auto_issues = find_validation_issues(state.draft, schema_graph)
    prior_pydantic = [i for i in state.validation_issues if "[pydantic]" in i]
    validation_issues = sorted(set(auto_issues + prior_pydantic))

    if not llm_available():
        return _no_llm_terminal(state, validation_issues)

    view = build_schema_view()
    decision = await _ask_llm(state, context, view, validation_issues, schema_graph)

    # Safety net: if we're in Segment 7 (flow exists, template_json doesn't)
    # and the LLM didn't set finalize=true, but the draft has enough to
    # finalize, force it. This prevents the "yes → re-show summary" loop.
    # However, if finalize already failed (retries exhausted), don't force
    # it again — let the LLM surface the errors to the user instead.
    segment = _detect_segment(state)
    if (
        segment[0] == 7
        and not decision.finalize
        and state.draft.get("flow", {}).get("nodes")
        and not state.template_json
        and state.finalize_retries < 2
    ):
        logger.info(
            "[blueprint-turn] Segment 7 with flow ready but LLM didn't "
            "finalize — forcing finalize=true (retries=%d)",
            state.finalize_retries,
        )
        decision = decision.model_copy(update={"finalize": True})

    return await _apply_decision(
        state=state,
        decision=decision,
        validation_issues=validation_issues,
    )


# ---------------------------------------------------------------------------
# LLM call + retry
# ---------------------------------------------------------------------------


async def _ask_llm(
    state: BlueprintState,
    context: BlueprintContext,
    view: SchemaView,
    validation_issues: list[str],
    schema_graph: Any,
) -> TurnDecision:
    """Single LLM call returning a structured ``TurnDecision``."""
    schema_cls = build_turn_decision_schema()
    llm = get_llm(temperature=0.0).with_structured_output(schema_cls)

    prompt = _build_user_prompt(state, context, view, validation_issues)
    try:
        raw = await llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
    except Exception as exc:
        logger.warning(f"[blueprint-turn] LLM call failed: {exc}")
        return TurnDecision(
            message_to_user=(
                "I hit an internal error trying to plan the next step. "
                "Try sending your message again, or describe what you'd "
                "like to set up."
            )
        )

    return coerce_to_decision(raw)


def _build_user_prompt(
    state: BlueprintState,
    context: BlueprintContext,
    view: SchemaView,
    validation_issues: list[str],
) -> str:
    """Assemble the user-message content for the turn LLM."""
    transcript = _transcript_lines(state.messages, limit=30)
    remaining = remaining_groups(
        view, state.draft, state.completed_groups, state.skipped_groups
    )
    segment = _detect_segment(state)

    sections = [
        f"## Mode\n{context.mode}",
        f"## Reseller\n{context.reseller_id}",
    ]
    if context.existing_template_id:
        sections.append(f"## Editing template\n{context.existing_template_id}")
    if context.available_outbound_numbers:
        nums = json.dumps(context.available_outbound_numbers, default=str)
        sections.append(f"## Available outbound numbers\n{nums}")
    else:
        sections.append("## Available outbound numbers\n(none provisioned)")
    sections.extend(
        [
            f"## Segment status\nYou are in **Segment {segment[0]}** — {segment[1]}.",
            "## Schema view (compact)\n"
            + json.dumps(view.model_dump(), default=str, indent=2),
            "## Current draft\n" + json.dumps(state.draft, default=str, indent=2),
            f"## Completed groups\n{state.completed_groups}",
            f"## Skipped groups (user opted out)\n{state.skipped_groups}",
            f"## Remaining askable groups\n{remaining}",
            f"## Validation issues\n{validation_issues or '(none)'}",
            f"## Finalize retries used\n{state.finalize_retries} / 1",
        ]
    )
    sections.append("## Transcript (last 30 turns)\n" + "\n".join(transcript))
    sections.append(
        "## Your task\n"
        "Emit one TurnDecision. Follow the system prompt for the current segment."
    )
    return "\n\n".join(sections)


def _detect_segment(state: BlueprintState) -> tuple[int, str]:
    """Infer which segment the conversation is in from the draft state."""
    draft = state.draft
    configs = draft.get("configurations") or {}

    if not draft.get("name"):
        return (1, "Identity & Direction — ask name, merchant, inbound/outbound")

    if not draft.get("expected_payload_schema"):
        return (2, "Per-Call Data — ask what customer data the agent receives")

    if not configs.get("initial_greeting"):
        return (3, "Voice & Greeting — ask for the opening greeting + language")

    if not configs.get("stt_configuration"):
        return (4, "Call Management — batch all configuration defaults")

    has_globals_decision = (
        "warm_transfer" in state.completed_groups
        or configs.get("transfer_number") is not None
        or draft.get("flow", {}).get("end_conversation_callbacks") is not None
    )
    if not has_globals_decision:
        return (5, "Integrations & Functions — warm transfer, APIs, service callbacks")

    if not draft.get("flow", {}).get("nodes"):
        return (6, "Flow Design — build the entire flow with nodes, hooks, outcomes")

    if not state.template_json:
        return (7, "Review & Finalize — show summary, get approval, finalize")

    return (7, "Done — template assembled")


# ---------------------------------------------------------------------------
# Decision application
# ---------------------------------------------------------------------------


async def _apply_decision(
    state: BlueprintState,
    decision: TurnDecision,
    validation_issues: list[str],
) -> dict[str, Any]:
    """Translate a ``TurnDecision`` into a LangGraph state update."""
    update: dict[str, Any] = {}

    # Merge draft patch
    new_draft = _deep_merge(dict(state.draft), decision.draft_patch or {})
    if new_draft != state.draft:
        update["draft"] = new_draft

    # Defensive guard: the approval bar should ONLY render when there's
    # actually something for the user to sign off on. If the LLM set
    # ``pending_approval_for`` while also asking an open question with no
    # values landing in the draft this turn, treat it as a free-text
    # question — drop the approval flag so the chat input shows.
    if decision.pending_approval_for and not decision.draft_patch:
        logger.info(
            "[blueprint-turn] dropping spurious pending_approval_for=%r — "
            "no draft_patch this turn so there's nothing to approve",
            decision.pending_approval_for,
        )
        decision = decision.model_copy(update={"pending_approval_for": None})

    # Normalize the draft patch — the LLM sometimes emits dotted paths
    # as flat keys instead of nested dicts. Convert
    # ``{"configurations.stt_configuration.provider": "soniox"}`` into
    # ``{"configurations": {"stt_configuration": {"provider": "soniox"}}}``
    # so the deep merge actually lands the value where ReplaceTemplateRequest
    # can find it.
    if decision.draft_patch:
        normalized = _normalize_draft_patch(decision.draft_patch)
        if normalized != decision.draft_patch:
            decision = decision.model_copy(update={"draft_patch": normalized})

    # Drop any group the LLM put in both buckets — completed wins
    # (a meaningful answer trumps an opt-out).
    skipped_decision = [
        g for g in decision.skipped_groups if g not in decision.completed_groups
    ]
    if skipped_decision != decision.skipped_groups:
        decision = decision.model_copy(update={"skipped_groups": skipped_decision})

    # Append completed groups (de-duped, order preserved)
    if decision.completed_groups:
        merged = list(state.completed_groups)
        for g in decision.completed_groups:
            if g not in merged:
                merged.append(g)
        if merged != state.completed_groups:
            update["completed_groups"] = merged

    # Append skipped groups (de-duped, order preserved). If a group is now
    # in completed, drop it from skipped — the user came back and answered.
    completed_after = update.get("completed_groups", state.completed_groups)
    if decision.skipped_groups or any(
        g in completed_after for g in state.skipped_groups
    ):
        merged_skipped = [g for g in state.skipped_groups if g not in completed_after]
        for g in decision.skipped_groups:
            if g in completed_after:
                continue
            if g not in merged_skipped:
                merged_skipped.append(g)
        if merged_skipped != state.skipped_groups:
            update["skipped_groups"] = merged_skipped

    # UI approval flag
    update["pending_approval_for"] = decision.pending_approval_for

    # Refresh validation issues against the latest draft
    schema_graph = build_schema_graph()
    refreshed = find_validation_issues(update.get("draft", state.draft), schema_graph)
    prior_pydantic = [i for i in validation_issues if "[pydantic]" in i]
    update["validation_issues"] = sorted(set(refreshed + prior_pydantic))

    # Finalize?
    if decision.finalize:
        finalize_update = _finalize(
            draft=update.get("draft", state.draft),
            existing_validation_issues=update["validation_issues"],
            retries_used=state.finalize_retries,
        )
        update.update(finalize_update)

    # User-facing message — only emit when there's text. Empty string =
    # silent turn (e.g. specialist ran and we'll talk on next user input).
    if decision.message_to_user.strip():
        update["messages"] = [AIMessage(content=decision.message_to_user)]

    # Terminal — caller (the graph node) reads this from the returned
    # update. We don't need a state field for it; the absence of an
    # outstanding question is signalled by pending_approval_for=None.
    return update


def _finalize(
    draft: dict[str, Any],
    existing_validation_issues: list[str],
    retries_used: int,
) -> dict[str, Any]:
    """Run the linter, apply auto-fixes, then assemble the template.

    The linter (a) auto-fixes 7 categories of safe defaults (is_active,
    end_conversation_callbacks, terminal node, outcome casing, etc.) and
    (b) surfaces hard errors / warnings before Pydantic ever sees the
    draft. Errors land in ``validation_issues`` so the next turn can
    address them; warnings are logged for the operator.
    """
    lint = lint_template(draft)
    fixed_draft = lint.fixed_draft

    if lint.auto_fixes_applied:
        for fix in lint.auto_fixes_applied:
            logger.info(f"[blueprint-turn] linter auto-fix: {fix}")
    if lint.warnings:
        for warn in lint.warnings:
            logger.warning(f"[blueprint-turn] linter warning: {warn}")

    # Hard linter errors stop finalize; surface them so the next tick fixes them.
    if lint.errors:
        marked = [f"[pydantic] {e}" for e in lint.errors]
        merged_issues = list(existing_validation_issues)
        for e in marked:
            if e not in merged_issues:
                merged_issues.append(e)
        return {
            "draft": fixed_draft,
            "template_json": None,
            "validation_issues": merged_issues,
            "finalize_retries": retries_used + 1,
        }

    template_json, errors = assemble_final_template(fixed_draft)
    if not errors:
        return {
            "draft": fixed_draft,
            "template_json": template_json,
            "validation_issues": [
                i for i in existing_validation_issues if "[pydantic]" not in i
            ],
            "finalize_retries": 0,
        }

    pydantic_marked = [f"[pydantic] {e}" for e in errors]
    merged_issues = list(existing_validation_issues)
    for e in pydantic_marked:
        if e not in merged_issues:
            merged_issues.append(e)

    return {
        "draft": fixed_draft,
        "template_json": None,
        "validation_issues": merged_issues,
        "finalize_retries": retries_used + 1,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_llm_terminal(
    state: BlueprintState, validation_issues: list[str]
) -> dict[str, Any]:
    """Used when Vertex creds aren't configured — return a clean terminal."""
    return {
        "validation_issues": validation_issues,
        "pending_approval_for": None,
        "messages": [
            AIMessage(
                content=(
                    "Blueprint needs Vertex credentials to plan turns. Set "
                    "BLUE_PRINT_GOOGLE_CREDENTIALS_JSON and restart."
                )
            )
        ],
    }


def _transcript_lines(messages: list[AnyMessage], limit: int) -> list[str]:
    """Render the last ``limit`` messages as ``[role] text`` lines.

    Prefers the LangChain 1.x ``content_blocks`` accessor so typed blocks
    (text, thinking, tool_use, citations, …) are normalised — only ``text``
    blocks land in the transcript, thinking/tool blocks are skipped cleanly.
    Falls back to the legacy ``content`` walk for messages that don't
    expose the accessor.
    """
    out: list[str] = []
    for m in messages[-limit:]:
        role = "user" if isinstance(m, HumanMessage) else "ai"
        blocks = getattr(m, "content_blocks", None)
        if blocks is not None:
            text = "".join(
                str(b.get("text", ""))
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                text = "".join(parts)
            else:
                text = str(content)
        # Truncate long lines so the transcript stays cheap to send.
        if len(text) > 500:
            text = text[:497] + "…"
        out.append(f"[{role}] {text}")
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` (overlay wins on conflict)."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _normalize_draft_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted keys in ``patch`` into nested dicts.

    Accepts both shapes — ``{"a.b.c": v}`` and ``{"a": {"b": {"c": v}}}`` —
    and returns a single nested dict suitable for ``_deep_merge`` against
    a Pydantic-shaped draft.
    """
    out: dict[str, Any] = {}
    for key, value in patch.items():
        # Recurse into nested dicts so any dotted keys deeper down also
        # get expanded.
        if isinstance(value, dict):
            value = _normalize_draft_patch(value)
        if "." in key:
            _set_dotted(out, key, value)
        else:
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = value
    return out


def _set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Insert ``value`` at ``dotted_key`` in ``target``, creating intermediates."""
    parts = dotted_key.split(".")
    cursor = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    leaf = parts[-1]
    if isinstance(value, dict) and isinstance(cursor.get(leaf), dict):
        cursor[leaf] = _deep_merge(cursor[leaf], value)
    else:
        cursor[leaf] = value


# ---------------------------------------------------------------------------
# Approval interrupt — second node in the graph
# ---------------------------------------------------------------------------


async def await_approval(state: BlueprintState) -> dict[str, Any]:
    """Pause the graph for approval when ``pending_approval_for`` is set.

    This runs as a second node AFTER ``run_turn`` so the state writes
    from this tick (most importantly ``pending_approval_for`` itself)
    are committed BEFORE the interrupt fires. That preserves the
    backward-compat contract with chat UIs (Loom): they read the flag
    from ``aget_state`` snapshots and don't need to know about
    interrupts.

    Programmatic SDK consumers see the ``__interrupt__`` event in
    streaming output and can ``Command(resume=<reply>)`` to continue.
    On resume, ``interrupt()`` returns the reply value; we append it as
    a ``HumanMessage`` and clear the approval flag, so the next user
    turn (or the next ``ainvoke`` call) sees a clean slate.
    """
    reply = interrupt(
        {
            "approval_for": state.pending_approval_for,
            "draft": state.draft,
            "completed_groups": list(state.completed_groups),
        }
    )
    text = reply if isinstance(reply, str) else str(reply)
    return {
        "pending_approval_for": None,
        "messages": [HumanMessage(content=text)],
    }


def route_after_turn(state: BlueprintState) -> str:
    """Route to ``await_approval`` only when the tick set the approval flag."""
    return "await_approval" if state.pending_approval_for else END


__all__ = ["await_approval", "route_after_turn", "run_turn"]
