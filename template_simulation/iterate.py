"""Turn a confirmed failure into a template patch, and prove it.

Ask GRID for a patch → re-run the FULL suite on it → keep it only if a
targeted failure became a pass and nothing regressed. Targeted mode (default)
shows GRID only editable prose; ``ALLOW_STRUCTURAL`` shows the whole template.

Non-negotiable: identity/config fields never change, acceptance is a stability
transition and never a score, and the original file is never written.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.ai.voice.agents.breeze_buddy.template.generator.prompts import (
    build_system_prompt,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from template_simulation.suite import (
    CaseResult,
    CaseVerdict,
    SimCase,
    Speaker,
    Stability,
)

_TARGETED_MAX_TOKENS = 3000
_TARGETED_TIMEOUT_S = 90.0
_STRUCTURAL_MAX_TOKENS = 16000
_STRUCTURAL_TIMEOUT_S = 240.0
_TRANSCRIPT_TAIL_TURNS = 6

_PATCHABLE = frozenset(
    {
        "expected_function_called",
        "terminal_node",
        "required_args_present",
        "outcome_fields",
        "reached_a_terminal_node",
    }
)

# Identity, billing and provider configuration — nothing a prompt refinement
# has any business changing.
_LOCKED_FIELDS = (
    "id",
    "reseller_id",
    "merchant_id",
    "created_at",
    "updated_at",
    "expected_payload_schema",
    "expected_callback_response_schema",
    "configurations",
    "secrets",
    "telephony_number_id",
    "outbound_number_id",
    "is_active",
    "supported_channels",
)

_TARGETED_RULES = """\
You are refining an EXISTING production template from real test failures —
not rewriting it. Every word you add is a word a real customer hears and the
LLM must attend to on every call; padding is not free.

Return ONLY a JSON object in one ```json fence:
  {"edits": [
    {"node": "<node_name>", "field": "task_messages", "value": "..."},
    {"node": "<node_name>", "function": "<fn>", "field": "description", "value": "..."}
  ]}

1. "field" is task_messages or role_messages for a node edit, or description
   for a function edit (which requires "function").
2. "value" REPLACES THE ENTIRE CURRENT FIELD, not a diff or a fragment. You
   are shown the field's full current text below — "value" must be that same
   full text with your fix applied in place, everything else byte-for-byte
   unchanged. A node's task_messages is one prompt built from several rules
   working together (e.g. when to offer a link, when consent counts, when to
   classify) — dropping any of them because they weren't the one you were
   fixing breaks those other rules the same as deleting them on purpose.
   Before answering, check: does "value" still contain every rule from the
   original that this fix did not need to touch? If not, you have truncated
   the field, not edited it.
3. Include ONLY the edits you are making. No commentary, no full template. At
   most ONE edit per (node, field) or (node, function) pair — if a fix needs
   more than one change to the same field, make them all in that field's
   single "value", not as separate edits (a second edit to the same field
   overwrites the first, silently discarding it).
4. Change as little of the field's WORDING as the fix requires — a smaller
   diff against the original text is better — but never at the cost of
   dropping content unrelated to the fix. "Smallest change" means smallest
   edit, not shortest output.
5. Never touch function names, node names or transitions; this mode is
   prose-only. If the fix genuinely needs a structural change, say so in
   "value" as a one-line note instead of inventing one.
6. No filler, no hedging, no restating what the surrounding prompt says.
7. The failing cases below name the nodes and functions they are about. Edit
   those. An edit to a function no failing case mentions is how a patch fixes
   one case and silently breaks two others — it will be re-run against the
   whole suite and rejected.
"""

_STRUCTURAL_RULES = """\
You are refining an EXISTING production template from real test failures.

1. Output the COMPLETE template JSON in one ```json fence, not a diff.
2. You may NOT change: id, reseller_id, merchant_id, telephony_number_id,
   secrets, expected_payload_schema, configurations. Changing any of them
   causes the patch to be rejected outright.
3. Structural changes to `flow` are permitted this run, but make the SMALLEST
   change that plausibly fixes the reported failures.
"""


@dataclass
class IterationRecord:
    iteration: int
    accepted: bool
    reason: str
    targeted: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)
    regressed: List[str] = field(default_factory=list)
    still_failing: List[str] = field(default_factory=list)
    edits: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "accepted": self.accepted,
            "reason": self.reason,
            "targeted": self.targeted,
            "fixed": self.fixed,
            "regressed": self.regressed,
            "still_failing": self.still_failing,
            "edits": self.edits,
        }


@dataclass
class IterateResult:
    template: Dict[str, Any]  # the best accepted template, raw
    summary: Any  # the RunSummary that produced it
    iterations: List[IterationRecord] = field(default_factory=list)
    stopped_reason: str = "unknown"


def _decode_fence(content: Any) -> Any:
    text = str(content or "")
    match = re.search(r"```json\s*\n([\s\S]*?)\n?```", text)
    try:
        return json.loads(match.group(1).strip() if match else text.strip())
    except json.JSONDecodeError:
        return None


def _locked_changed(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return [
        f
        for f in _LOCKED_FIELDS
        if json.dumps(before.get(f), sort_keys=True, default=str)
        != json.dumps(after.get(f), sort_keys=True, default=str)
    ]


def failure_brief(
    verdicts: List[CaseVerdict], results: List[CaseResult], cases: List[SimCase]
) -> str:
    """The evidence for one patch attempt: what the template promised (and
    where it says so), what happened, and the tail of the conversation.

    The transcript is the part that matters. A proposer told only "expected
    end_committed, got end_user_busy" has to guess; shown the last few turns
    it can see the customer said "call me tomorrow" and the prompt gave no
    guidance on deferrals. That is a patch aimed at a cause, not a symptom.
    """
    by_case = {c.id: c for c in cases}
    sample: Dict[str, CaseResult] = {}
    for r in results:
        if not r.passed and r.case_id not in sample:
            sample[r.case_id] = r

    lines: List[str] = []
    for v in verdicts:
        lines.append(f"### case `{v.case_id}` (failed {v.total}/{v.total} runs)")
        case = by_case.get(v.case_id)
        if case:
            lines.append(f"Customer persona: {case.persona.strip()}")
            for name, grounding in case.expect.grounding.items():
                lines.append(
                    f"Template promises {name} = {getattr(case.expect, name, None)!r} "
                    f"(declared at {grounding.source_path})"
                )
        lines.append(f"What failed: {v.primary_failure} — {v.detail}")
        if v.diagnosis:
            lines.append(f"Diagnosis: {v.diagnosis}")
        result = sample.get(v.case_id)
        if result and result.trace.turns:
            lines.append("End of the conversation:")
            for turn in result.trace.turns[-_TRANSCRIPT_TAIL_TURNS:]:
                who = "AGENT" if turn.speaker is Speaker.AGENT else "CUSTOMER"
                if turn.text:
                    lines.append(f"  {who}: {turn.text.strip()}")
            for call in result.trace.tool_calls:
                lines.append(
                    f"  AGENT -> calls {call.name}({json.dumps(call.arguments)})"
                )
        lines.append("")
    return "\n".join(lines) or "(no confirmed failures — nothing to refine)"


def _editable_snapshot(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The only prose a targeted patch may see: per-node messages and function
    descriptions. Config, hooks, transitions and schema never reach the prompt."""
    return [
        {
            "node": node.get("node_name"),
            "task_messages": node.get("task_messages"),
            "role_messages": node.get("role_messages"),
            "functions": [
                {
                    "function": fn.get("name") or fn.get("function_name"),
                    "description": fn.get("description"),
                }
                for fn in node.get("functions") or []
                if isinstance(fn, dict)
            ],
        }
        for node in (raw.get("flow") or {}).get("nodes") or []
    ]


def apply_edits(
    raw: Dict[str, Any], edits: List[Any]
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Apply each edit to a deep copy. An edit naming an unknown node,
    function or field is skipped and reported, never silently applied."""
    patched = copy.deepcopy(raw)
    nodes = {
        n.get("node_name"): n for n in (patched.get("flow") or {}).get("nodes") or []
    }
    applied: List[str] = []
    skipped: List[str] = []
    seen: set = set()

    for edit in edits:
        if not isinstance(edit, dict):
            skipped.append(f"not an object: {edit!r}")
            continue
        node_name, fn_name = edit.get("node"), edit.get("function")
        name, value = edit.get("field"), edit.get("value")
        node = nodes.get(node_name)
        if node is None:
            skipped.append(f"unknown node {node_name!r}")
            continue

        # A second edit to the same target overwrites the first outright
        # (there is no diff/merge here) — keep the first, drop the rest.
        target = (node_name, fn_name, name)
        if target in seen:
            skipped.append(f"duplicate edit to {node_name}.{fn_name or name} ignored")
            continue
        seen.add(target)

        if fn_name is not None:
            if name != "description":
                skipped.append(f"function edits only support description ({name!r})")
                continue
            fn = next(
                (
                    f
                    for f in node.get("functions") or []
                    if isinstance(f, dict)
                    and (f.get("name") or f.get("function_name")) == fn_name
                ),
                None,
            )
            if fn is None:
                skipped.append(f"unknown function {fn_name!r} on {node_name!r}")
                continue
            fn["description"] = value
            applied.append(f"{node_name}.{fn_name}.description")
            continue

        if name not in ("task_messages", "role_messages"):
            skipped.append(f"node edits only support task/role_messages ({name!r})")
            continue
        original = node.get(name)
        orig_text = (
            original
            if isinstance(original, str)
            else "".join(
                str(m.get("content", "")) for m in original or [] if isinstance(m, dict)
            )
        )
        # Losing more than half of a substantial field means GRID sent back
        # the touched fragment, not the whole field — everything else that
        # field carried would be deleted with it.
        if (
            isinstance(value, str)
            and len(orig_text) >= 200
            and len(value) < len(orig_text) / 2
        ):
            skipped.append(
                f"{node_name}.{name}: new value is {len(value)} chars vs "
                f"{len(orig_text)} original — looks like unrelated content was "
                "dropped rather than edited; not applied"
            )
            continue
        if isinstance(value, str) and isinstance(original, list):
            # GRID edits prose as a flat string but a template stores
            # [{"role", "content"}] — writing it straight back made pydantic
            # validate one CHARACTER at a time. Keep the shape.
            role = (
                original[0].get("role", "system")
                if original and isinstance(original[0], dict)
                else "system"
            )
            node[name] = [{"role": role, "content": value}]
        else:
            node[name] = value
        applied.append(f"{node_name}.{name}")

    return patched, applied, skipped


async def _ask_grid(
    llm: Any, system_prompt: str, message: str, *, max_tokens: int, timeout: float
) -> Tuple[Optional[str], Optional[str]]:
    from pipecat.processors.aggregators.llm_context import LLMContext

    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(
            llm.run_inference(
                LLMContext([{"role": "user", "content": message}]),
                system_instruction=system_prompt,
                max_tokens=max_tokens,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None, f"LLM call timed out after {timeout:.0f}s"
    except Exception as e:
        return None, f"LLM call failed: {type(e).__name__}: {e}"
    logger.info(f"[iterate] patch call returned in {time.monotonic() - started:.1f}s")
    return raw, None


async def propose_patch(
    raw: Dict[str, Any], brief: str, llm: Any, *, allow_structural: bool = False
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Returns (patch or None, reason). ``reason`` is always populated — on
    success too — so every attempt is auditable, not just the failures."""
    if allow_structural:
        message = (
            f"Here is the current template:\n\n```json\n{json.dumps(raw, indent=2, ensure_ascii=False)}\n```\n\n"
            f"It failed these cases on EVERY repetition. Fix it:\n\n{brief}"
        )
        text, err = await _ask_grid(
            llm,
            build_system_prompt(refinement_mode=True) + "\n\n" + _STRUCTURAL_RULES,
            message,
            max_tokens=_STRUCTURAL_MAX_TOKENS,
            timeout=_STRUCTURAL_TIMEOUT_S,
        )
        if err:
            return None, err
        patch = _decode_fence(text)
        if not isinstance(patch, dict):
            return None, "response contained no parseable ```json template"
        try:
            TemplateModel.model_validate(patch)
        except Exception as e:
            return None, f"patch does not validate as a template: {e}"
        locked = _locked_changed(raw, patch)
        if locked:
            return None, f"patch touched locked field(s): {locked}"
        return patch, "proposed (structural)"

    message = (
        "Editable prose from the current template:\n\n```json\n"
        f"{json.dumps(_editable_snapshot(raw), indent=2, ensure_ascii=False)}\n```\n\n"
        "These cases failed on EVERY repetition — reproducible, not noise. "
        f"Return the edits that fix them:\n\n{brief}"
    )
    text, err = await _ask_grid(
        llm,
        build_system_prompt(refinement_mode=True) + "\n\n" + _TARGETED_RULES,
        message,
        max_tokens=_TARGETED_MAX_TOKENS,
        timeout=_TARGETED_TIMEOUT_S,
    )
    if err:
        return None, err
    parsed = _decode_fence(text)
    edits = parsed.get("edits") if isinstance(parsed, dict) else None
    if not isinstance(edits, list) or not edits:
        return None, "response had no usable 'edits' list"

    patched, applied, skipped = apply_edits(raw, edits)
    if not applied:
        return None, f"no edits could be applied: {skipped}"
    if skipped:
        logger.warning(f"[iterate] {len(skipped)} edit(s) skipped: {skipped}")
    try:
        TemplateModel.model_validate(patched)
    except Exception as e:
        return None, f"patched template failed validation: {e}"
    return patched, f"proposed ({len(applied)} edit(s): {', '.join(applied)})"


@dataclass
class PatchOutcome:
    """Evidence for accepting or rejecting a candidate — every field is a
    transition between STABILITY classes, never a score."""

    fixed: List[str] = field(default_factory=list)  # confirmed fail -> confirmed pass
    regressed: List[str] = field(
        default_factory=list
    )  # confirmed pass -> confirmed fail
    still_failing: List[str] = field(default_factory=list)
    destabilised: List[str] = field(default_factory=list)  # confirmed pass -> flaky

    @property
    def accept(self) -> bool:
        """Fixed something reproducibly, broke nothing reproducibly. Flaky
        movement either way is ignored — reacting to it is what this rule
        exists to prevent."""
        return bool(self.fixed) and not self.regressed

    def reason(self) -> str:
        if self.regressed:
            return f"rejected: regressed {self.regressed}"
        if not self.fixed:
            return "rejected: fixed no confirmed failure"
        note = f" (destabilised {self.destabilised})" if self.destabilised else ""
        return f"accepted: fixed {self.fixed}{note}"


def compare(before: Any, after: Any, targets: Set[str]) -> PatchOutcome:
    """``targets`` are the confirmed failures the patch was aimed at. Fixing
    something else is welcome, but it is the targeted fixes that justify
    accepting the candidate."""
    after_by_id = {v.case_id: v for v in after.verdicts}
    outcome = PatchOutcome()

    for case_id in sorted(targets):
        verdict = after_by_id.get(case_id)
        if verdict is not None and verdict.stability is Stability.CONFIRMED_PASS:
            outcome.fixed.append(case_id)
        else:
            outcome.still_failing.append(case_id)

    for v in before.confirmed_passes:
        after_v = after_by_id.get(v.case_id)
        if after_v is None:
            continue
        if after_v.stability is Stability.CONFIRMED_FAIL:
            outcome.regressed.append(v.case_id)
        elif after_v.stability is Stability.FLAKY:
            # Not a rejection on its own, but a patch quietly turning solid
            # cases into coin flips is something a person needs to see.
            outcome.destabilised.append(v.case_id)
    return outcome


async def iterate(
    *,
    template: TemplateModel,
    template_raw: Dict[str, Any],
    measure: Callable[[TemplateModel], Any],  # async: TemplateModel -> RunSummary
    cases_now: Callable[[], List[SimCase]],
    iterate_llm: Any,
    max_iterations: int = 3,
    allow_structural: bool = False,
    say: Callable[[str], None] = print,
    initial: Any = None,
    patch_dir: Optional[Path] = None,
) -> IterateResult:
    """Refine until nothing fails reproducibly, or the proposer stops making
    progress. ``initial`` is the eval pass already run, so round 1 does not
    re-measure the template it was just handed.

    ``cases_now`` is a callable because a prose patch can change the very text
    a case's persona was written from — the brief must quote the cases that
    actually ran, not the ones from before the patch.
    """
    current_raw = template_raw
    summary = initial if initial is not None else await measure(template)
    records: List[IterationRecord] = []
    barren = 0

    for i in range(1, max_iterations + 1):
        confirmed = summary.confirmed_failures
        if not confirmed:
            return IterateResult(
                current_raw,
                summary,
                records,
                "no_confirmed_failures" if not summary.flaky else "only_flaky_remain",
            )

        actionable = [v for v in confirmed if v.primary_failure in _PATCHABLE]
        unpatchable = [v for v in confirmed if v.primary_failure not in _PATCHABLE]
        if unpatchable:
            say(
                f"  {len(unpatchable)} failure(s) carry no template citation "
                f"({sorted((v.primary_failure or '?') for v in unpatchable)}) — "
                f"reported, not sent to the proposer"
            )
        if not actionable:
            return IterateResult(current_raw, summary, records, "nothing_patchable")
        confirmed = actionable

        targets = {v.case_id for v in confirmed}
        say(f"\niteration {i}/{max_iterations} — targeting {sorted(targets)}")

        patch, reason = await propose_patch(
            current_raw,
            failure_brief(confirmed, summary.results, cases_now()),
            iterate_llm,
            allow_structural=allow_structural,
        )
        if patch is None:
            say(f"  no patch: {reason}")
            records.append(IterationRecord(i, False, reason, targeted=sorted(targets)))
            barren += 1
            if barren >= 2:
                return IterateResult(
                    current_raw, summary, records, "no_usable_patch_twice"
                )
            continue

        say(f"  {reason}")
        if patch_dir is not None:
            # Every proposal is kept, accepted or not — a rejected patch is
            # the most useful thing to read when the loop makes no progress.
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / f"iteration-{i}.json").write_text(
                json.dumps(patch, indent=2, ensure_ascii=False)
            )
        candidate = TemplateModel.model_validate(patch)
        candidate_summary = await measure(candidate)
        outcome = compare(summary, candidate_summary, targets)
        record = IterationRecord(
            iteration=i,
            accepted=outcome.accept,
            reason=outcome.reason(),
            targeted=sorted(targets),
            fixed=outcome.fixed,
            regressed=outcome.regressed,
            still_failing=outcome.still_failing,
            edits=[reason],
        )
        records.append(record)
        say(f"  {record.reason}")

        if outcome.accept:
            current_raw, summary = patch, candidate_summary
            barren = 0
        else:
            barren += 1
            # Two rounds without a confirmed fix means the proposer is not
            # converging on this evidence. Grinding on produces prose churn
            # and overfits the template to the persona's phrasing.
            if barren >= 2:
                return IterateResult(current_raw, summary, records, "no_progress")

    return IterateResult(current_raw, summary, records, "max_iterations")


def write_iteration_artifacts(result: IterateResult, out_dir: Path) -> Path:
    """Writes the accepted template as ``iterated-<template>.json``. The file
    IS the template, identity fields and all — only what a patch touched
    differs. The input file is never overwritten; promotion is a human action.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    name = result.template.get("name") or "template"
    candidate = out_dir / f"iterated-{name}.json"
    candidate.write_text(json.dumps(result.template, indent=2, ensure_ascii=False))
    return candidate
