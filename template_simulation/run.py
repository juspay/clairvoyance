#!/usr/bin/env python
"""CLI, orchestration, report, artifacts. Run this file.

    uv run python template_simulation/run.py            # the template in templates/
    uv run python template_simulation/run.py -t <path>  # a specific template

Exits non-zero when the gate fails. The bar is "nothing failed reproducibly" —
a flaky case is a warning, since gating on it makes CI red at random.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# .env must load BEFORE any app import: app/core/config/static.py reads env at
# import time and app/core/security/jwt.py raises without JWT_* (CLAUDE.md).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")
os.environ.setdefault("SKIP_KMS_DECRYPT", "true")
os.environ.setdefault("JWT_SECRET_KEY", "sim-harness-local")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
sys.path.insert(0, str(REPO_ROOT))

# Silence pipecat's import-time banner and the app logger's own sink, then put
# back a WARNING+ sink — without it, judge failures and retry exhaustion
# vanished with no trace.
from loguru import logger as _logger  # noqa: E402

_logger.remove()
import app.core.logger  # noqa: E402,F401

_logger.remove()

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel  # noqa: E402

_logger.remove()

# Expected, not diagnostic: queuing a real EndFrame on end_conversation (what
# production does) stops the pipeline mid-flight, which leaves pipecat
# cancelling an in-flight bookkeeping task and logging about it.
_BENIGN = (
    "dangling tasks detected",
    "uncaught exception in event handler",
    "Task was destroyed but it is pending",
)
_logger.add(
    sys.stderr,
    level="WARNING",
    filter=lambda r: not any(b in r["message"] for b in _BENIGN),
    format="<level>{level: <8}</level> | {message}",
)

from template_simulation import config as cfg  # noqa: E402
from template_simulation.call import (  # noqa: E402
    build_persona,
    resolve_payload_overrides,
    run_call,
)
from template_simulation.grade import (  # noqa: E402
    aggregate,
    assert_all,
    case_to_dict,
    explain_failure,
    judge_critical_assertions,
    judge_trace,
    observe,
    score,
)
from template_simulation.iterate import (  # noqa: E402
    IterateResult,
    iterate,
    write_iteration_artifacts,
)
from template_simulation.suite import (  # noqa: E402
    CASES_FILE_SUFFIX,
    SIDECAR_SUFFIXES,
    Assertion,
    CaseResult,
    CaseVerdict,
    SimCase,
    SimTrace,
    Speaker,
    Stability,
    author_payload,
    author_personas,
    build_suite,
    prompt_digest,
    save_suite,
    terminal_nodes,
    validate_suite,
)

TEMPLATES_DIR = REPO_ROOT / "template_simulation" / "templates"


def say(line: str = "") -> None:
    print(line, flush=True)


# ---------------------------------------------------------------------------
# A suite run
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    template_name: str
    results: List[CaseResult] = field(default_factory=list)
    verdicts: List[CaseVerdict] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    suite_problems: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def score(self) -> float:
        """Reported, never gated on — an average lets one wholly broken branch
        hide behind fifteen healthy ones."""
        if not self.results:
            return 0.0
        return sum(score(r.assertions) for r in self.results) / len(self.results)

    def _by(self, stability: Stability) -> List[CaseVerdict]:
        return [v for v in self.verdicts if v.stability is stability]

    @property
    def confirmed_failures(self) -> List[CaseVerdict]:
        return self._by(Stability.CONFIRMED_FAIL)

    @property
    def confirmed_passes(self) -> List[CaseVerdict]:
        return self._by(Stability.CONFIRMED_PASS)

    @property
    def flaky(self) -> List[CaseVerdict]:
        return self._by(Stability.FLAKY)

    def gate(self) -> bool:
        return not self.confirmed_failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template": self.template_name,
            "score": round(self.score, 4),
            "duration_s": round(self.duration_s, 2),
            "gate": self.gate(),
            "counts": {
                "cases": len({v.case_id for v in self.verdicts}),
                "runs": len(self.results),
                "confirmed_pass": len(self.confirmed_passes),
                "confirmed_fail": len(self.confirmed_failures),
                "flaky": len(self.flaky),
            },
            "suite_problems": self.suite_problems,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "observations": self.observations,
        }


def _write_case(result: CaseResult, out_dir: Optional[Path]) -> None:
    """Transcript and audio together, the instant the run finishes — a suite
    stopped partway through must never leave one without the other."""
    if out_dir is None:
        return
    trace = result.trace
    if trace.audio_wav:
        audio_dir = out_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        path = audio_dir / f"{result.case_id}-{result.repetition}.wav"
        try:
            path.write_bytes(trace.audio_wav)
            trace.audio_path = path.name
        except OSError as e:
            _logger.warning(f"[sim] could not save recording: {e}")
    try:
        cases_dir = out_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        (cases_dir / f"{result.case_id}-{result.repetition}.json").write_text(
            json.dumps(case_to_dict(result), indent=2, ensure_ascii=False, default=str)
        )
    except OSError as e:
        _logger.warning(f"[sim] could not save case file: {e}")


async def run_one(
    template: TemplateModel,
    case: SimCase,
    *,
    repetition: int,
    persona_llm: Any,
    judge_llm: Optional[Any],
    terminals: List[str],
    instructions: str,
    out_dir: Optional[Path],
    sem: asyncio.Semaphore,
) -> CaseResult:
    """One call plus its grading. The semaphore is held ONLY around the call —
    the judge is a plain HTTP request with no websocket state to protect, and
    holding the lock through it added the judge's latency to every case."""
    async with sem:
        started = time.monotonic()
        try:
            trace = await run_call(
                template=template,
                case=case,
                persona=build_persona(case, persona_llm),
                repetition=repetition,
            )
        except Exception as e:
            _logger.exception(f"[sim] {case.id} rep{repetition} crashed")
            trace = SimTrace(case_id=case.id, repetition=repetition, error=str(e))

    spoke_for = time.monotonic() - started
    # Grading is off the call's clock but NOT free — timed separately so a
    # slow judge cannot hide behind the call seconds.
    graded_at = time.monotonic()
    assertions: List[Assertion] = assert_all(case, trace, terminals)
    judge = await judge_trace(trace, judge_llm, instructions) if judge_llm else None
    if judge:
        assertions.extend(judge_critical_assertions(judge))

    result = CaseResult(
        case_id=case.id,
        repetition=repetition,
        assertions=assertions,
        trace=trace,
        judge=judge,
        tier=case.tier,
        diagnosis=explain_failure(case, trace, assertions) or None,
    )
    _write_case(result, out_dir)

    mark = "PASS" if result.passed else "FAIL"
    grading = time.monotonic() - graded_at
    say(
        f"  [{mark}] {case.id}#{repetition}  {spoke_for:.0f}s call"
        + (f" + {grading:.0f}s grading" if grading >= 1 else "")
        + f", {trace.customer_turns} customer turns, "
        f"ended {trace.end_reason.value}"
        + (f" at {trace.final_node}" if trace.final_node else "")
    )
    if not result.passed:
        for a in result.assertions:
            if not a.passed and a.critical:
                say(f"         {a.name}: {a.detail}")
    return result


async def run_suite(
    template: TemplateModel,
    cases: List[SimCase],
    *,
    persona_llm: Any,
    judge_llm: Optional[Any] = None,
    out_dir: Optional[Path] = None,
    suite_problems: Optional[List[str]] = None,
) -> RunSummary:
    """Every case once, in parallel; then a second run of only the ones that
    failed, to tell a real failure from an unlucky sample.

    Re-running the passes too was the old cost model — it doubled a 10-minute
    suite to buy confirmation for the handful of cases that actually needed it.
    """
    started = time.monotonic()
    summary = RunSummary(
        template_name=template.name, suite_problems=list(suite_problems or [])
    )
    terminals = terminal_nodes(template)
    # The judge grades policy adherence against THESE, not a policy it invents.
    instructions = prompt_digest(template)
    sem = asyncio.Semaphore(max(1, cfg.CALL_CONCURRENCY))

    async def once(case: SimCase, repetition: int) -> CaseResult:
        return await run_one(
            template,
            case,
            repetition=repetition,
            persona_llm=persona_llm,
            judge_llm=judge_llm,
            terminals=terminals,
            instructions=instructions,
            out_dir=out_dir,
            sem=sem,
        )

    say(f"\nrunning {len(cases)} case(s), {cfg.CALL_CONCURRENCY} at a time")
    first = list(await asyncio.gather(*(once(c, 0) for c in cases)))
    by_case: Dict[str, List[CaseResult]] = {r.case_id: [r] for r in first}

    failed = [c for c in cases if not by_case[c.id][0].passed]
    if failed:
        say(f"\nconfirming {len(failed)} failure(s) with a second run")
        for result in await asyncio.gather(*(once(c, 1) for c in failed)):
            by_case[result.case_id].append(result)

    for case in cases:
        results = by_case[case.id]
        summary.results.extend(results)
        summary.verdicts.append(aggregate(case, results))
        summary.observations.append(observe(case, results[0].trace))

    summary.verdicts.sort(key=lambda v: (v.stability.value, v.case_id))
    summary.duration_s = time.monotonic() - started
    return summary


# ---------------------------------------------------------------------------
# One session: template -> suite -> run -> (iterate) -> artifacts
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    summary: Optional[RunSummary] = None
    iteration: Optional[IterateResult] = None
    out_dir: Optional[str] = None
    candidate_path: Optional[str] = None
    cases: List[SimCase] = field(default_factory=list)
    error: Optional[str] = None
    # Wall clock for EVERYTHING — suites, authoring, judging, patch calls. The
    # per-suite number only covers the calls, and a run once spent 67 of its 89
    # minutes outside them with nothing on screen to show it.
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.summary and self.summary.gate())


def repo_path(path: Any) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def resolve_template_path(override: Optional[str]) -> Path:
    """`-t` wins; otherwise the single file in template_simulation/templates/
    — "drop it here and run" is the point of that folder."""
    if override:
        resolved = repo_path(override)
        if resolved.is_file():
            return resolved
        say(
            f"[sim] {resolved} does not exist (a relative -t resolves from "
            f"{REPO_ROOT}, not your shell) — looking in {TEMPLATES_DIR}/ instead"
        )

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    found = sorted(
        f
        for f in TEMPLATES_DIR.iterdir()
        if f.is_file() and f.suffix == ".json" and not f.name.endswith(SIDECAR_SUFFIXES)
    )
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError(
            f"{len(found)} templates in {TEMPLATES_DIR}/ ({', '.join(f.name for f in found)}) "
            f"— pass -t <path> to choose one."
        )
    raise ValueError(
        f"No template to run. Drop the template JSON into {TEMPLATES_DIR}/ and run again, "
        f"or pass -t <path>."
    )


def _evidence(case: SimCase) -> str:
    """The template text a persona was written from — `refresh_cases` compares
    it to decide which personas a patch actually invalidated."""
    return "|".join(
        f"{k}={g.evidence}" for k, g in sorted(case.expect.grounding.items())
    )


async def build_cases(
    template: TemplateModel,
    template_path: Path,
    author_llm: Any,
    out_dir: Path,
) -> Tuple[List[SimCase], List[str]]:
    """The suite, plus whatever is wrong with it. Freshly derived and freshly
    authored every run; the record written to the run's own output folder is
    that run's record, not a cross-run cache."""
    suite_path = out_dir / f"{template.name}{CASES_FILE_SUFFIX}"
    overrides = resolve_payload_overrides(
        template_path=template_path,
        schema=template.expected_payload_schema or {},
    )

    if author_llm is not None and not overrides:
        say("authoring a realistic payload (one GRID call)...")
        overrides = await author_payload(template, author_llm)

    cases = build_suite(template, overrides)

    if author_llm is not None and cases:
        say(f"authoring {len(cases)} persona(s)...")
        await author_personas(
            template,
            cases,
            author_llm,
            concurrency=5,
            payload=cases[0].payload if cases else {},
            on_done=lambda c: say(f"  persona: {c.id}"),
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        save_suite(cases, str(suite_path))
    except OSError as e:
        _logger.debug(f"[sim] could not persist suite: {e}")

    return cases, validate_suite(template, cases)


async def refresh_cases(
    template: TemplateModel, previous: List[SimCase], author_llm: Any
) -> List[SimCase]:
    """Re-derive the suite for a PATCHED template, keeping every persona whose
    source text is unchanged.

    This is what makes the patch loop mean anything. A case is "a customer who
    behaves the way function F's description says, must be answered by calling
    F" — and a prose patch edits exactly that description. Measuring a
    candidate against personas written from the PRE-patch text tests a
    conversation the patched template never promised: a template whose
    `refund_requested` was described as "wants the item reshipped" can be
    fixed by swapping the two descriptions, and that fix is invisible (it
    scores identically) unless the personas move with it.

    Only cases whose grounding evidence actually changed are re-authored, so
    an unrelated edit costs nothing.
    """
    by_id = {c.id: c for c in previous}
    cases = build_suite(template, previous[0].payload if previous else None)
    stale: List[SimCase] = []
    for case in cases:
        prev = by_id.get(case.id)
        if prev and prev.persona.strip() and _evidence(prev) == _evidence(case):
            case.persona, case.payload = prev.persona, prev.payload
        else:
            stale.append(case)

    if stale and author_llm is not None:
        say(f"  re-authoring {len(stale)} persona(s) the patch changed the text for")
        await author_personas(
            template,
            stale,
            author_llm,
            concurrency=5,
            payload=cases[0].payload if cases else {},
        )
    # Keep the caller's scope: a filtered run must stay filtered.
    wanted = {c.id for c in previous}
    return [c for c in cases if c.id in wanted] if previous else cases


def run_dir(output_root: Path, template_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in template_name)
    return output_root / safe / time.strftime("%Y%m%d-%H%M%S")


def write_artifacts(
    summary: RunSummary,
    out_dir: Path,
    cases: List[SimCase],
    iteration: Optional[IterateResult] = None,
) -> None:
    """One plain-text file with the whole story — the same text `render()`
    puts on the terminal, plus the iteration log when one ran."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text = [render(summary, cases)]
    if iteration is not None:
        text.append(f"\nITERATION — stopped: {iteration.stopped_reason}")
        for record in iteration.iterations:
            text.append(f"  round {record.iteration}: {record.reason}")
            for edit in record.edits:
                text.append(f"    {edit}")
    (out_dir / "summary.txt").write_text("\n".join(text) + "\n")


async def run_session(path: Path, *, run_iterate: bool = True) -> SessionResult:
    result = SessionResult()
    session_started = time.monotonic()
    try:
        template_path = Path(path)
        raw = json.loads(template_path.read_text())
        template = TemplateModel.model_validate(raw)

        persona_llm = await cfg.grid_llm(cfg.GRID_PERSONA_MODEL)
        judge_llm = await cfg.grid_llm(cfg.GRID_JUDGE_MODEL)
        author_llm = await cfg.grid_llm(cfg.GRID_AUTHOR_MODEL)

        out_dir = run_dir(repo_path(cfg.OUTPUT_DIR), template.name)
        say(f"template: {template.name}  ({template_path})")
        say(
            f"agent model: {getattr(cfg.agent_llm_config(template), 'model', 'default')}"
        )
        say(f"output: {out_dir}")

        cases, problems = await build_cases(
            template, template_path, author_llm, out_dir
        )
        result.cases = cases
        if not cases:
            raise ValueError(
                f"{template.name} declares no functions — nothing to test."
            )
        for problem in problems:
            say(f"  suite problem: {problem}")

        current = list(cases)
        iteration_round = 0

        async def measure(candidate: TemplateModel) -> RunSummary:
            """Run the suite against one template. A candidate gets its suite
            re-derived first (see refresh_cases), and its cases/audio land
            under their own ``iteration-<n>/`` — every round reuses the same
            case ids, so one shared folder would overwrite the last."""
            nonlocal current, iteration_round
            if candidate is template:
                measure_dir = out_dir
            else:
                iteration_round += 1
                current = await refresh_cases(candidate, current, author_llm)
                measure_dir = out_dir / f"iteration-{iteration_round}"
            return await run_suite(
                candidate,
                current,
                persona_llm=persona_llm,
                judge_llm=judge_llm,
                out_dir=measure_dir,
                suite_problems=problems,
            )

        summary = await measure(template)
        result.summary = summary

        # Iterate only escalates on a real failure — a clean run never pays
        # for the patch loop.
        if run_iterate and not summary.gate():
            iterate_llm = await cfg.grid_llm(
                cfg.GRID_ITERATE_MODEL, cfg.GRID_ITERATE_MAX_TOKENS
            )
            iteration = await iterate(
                template=template,
                template_raw=raw,
                measure=measure,
                cases_now=lambda: current,
                iterate_llm=iterate_llm,
                max_iterations=cfg.MAX_ITERATIONS,
                allow_structural=cfg.ALLOW_STRUCTURAL,
                say=say,
                initial=summary,
                patch_dir=out_dir / "patches",
            )
            result.iteration = iteration
            result.summary = iteration.summary
            result.candidate_path = str(write_iteration_artifacts(iteration, out_dir))
            # The accepted patch lives in iterated-<template>.json, never
            # overwriting the input.
            write_artifacts(iteration.summary, out_dir, cases, iteration=iteration)
        else:
            write_artifacts(summary, out_dir, cases)

        result.out_dir = str(out_dir)
    except Exception as e:
        _logger.exception("[sim] run failed")
        result.error = f"{type(e).__name__}: {e}"
    result.duration_s = time.monotonic() - session_started
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_MARK = {
    Stability.CONFIRMED_PASS: "PASS ",
    Stability.CONFIRMED_FAIL: "FAIL ",
    Stability.FLAKY: "FLAKY",
}


def render(summary: RunSummary, cases: List[SimCase]) -> str:
    by_id = {c.id: c for c in cases}
    lines = [
        "",
        "=" * 72,
        f"{summary.template_name} — {'PASS' if summary.gate() else 'FAIL'}"
        f"  (score {summary.score:.2f}, {summary.duration_s:.0f}s)",
        "=" * 72,
    ]
    if summary.suite_problems:
        lines.append("\nSUITE PROBLEMS (the harness, not the agent):")
        lines += [f"  - {p}" for p in summary.suite_problems]

    lines.append("")
    for v in summary.verdicts:
        judged = f"  judge {v.judge_score:.2f}" if v.judge_score is not None else ""
        lines.append(
            f"{_MARK[v.stability]} {v.case_id}  ({v.passes}/{v.total}){judged}"
        )
        if v.stability is not Stability.CONFIRMED_PASS:
            lines.append(f"      {v.primary_failure or 'unstable'}: {v.detail}")
            if v.diagnosis:
                lines.append(f"      why: {v.diagnosis}")
            case = by_id.get(v.case_id)
            if case:
                lines.append(f"      persona: {case.persona.strip()[:200]}")

    lines.append("\nWhere each call landed:")
    for o in summary.observations:
        lines.append(
            f"  {o['case_id']}: {o['landed_on']} via {o['functions_called'] or 'no function'} "
            f"({o['end_reason']}, {o['customer_turns']} customer turns)"
        )

    counts = summary.to_dict()["counts"]
    lines.append(
        f"\n{counts['confirmed_pass']} passed, {counts['confirmed_fail']} failed, "
        f"{counts['flaky']} flaky, over {counts['runs']} call(s)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _amain(args: argparse.Namespace) -> int:
    result = await run_session(
        resolve_template_path(args.template), run_iterate=cfg.RUN_ITERATE
    )

    if result.error:
        say(f"\nrun failed: {result.error}")
        return 2
    if result.summary:
        say(render(result.summary, result.cases))
    if result.iteration:
        say(f"\niteration stopped: {result.iteration.stopped_reason}")
        for record in result.iteration.iterations:
            say(f"  {record.iteration}: {record.reason}")
        if result.candidate_path:
            say(f"  candidate written to {result.candidate_path} (original untouched)")
    if result.out_dir:
        say(f"\nartifacts: {result.out_dir}")
    say(f"total: {result.duration_s / 60:.1f} min end to end")
    return 0 if result.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-t", "--template", help="template JSON to run (one-off override)"
    )
    try:
        return asyncio.run(_amain(parser.parse_args()))
    except KeyboardInterrupt:
        say("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
