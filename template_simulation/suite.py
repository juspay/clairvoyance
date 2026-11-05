"""What gets tested: the data model, and the cases derived from a template.

Structure is derived deterministically; only persona prose is LLM-authored.
**Every expectation must cite the template field it came from** — one that
cannot is dropped, never guessed, because a guess fails a correct template.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pipecat.processors.aggregators.llm_context import LLMContext

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from template_simulation.config import with_retry

CASES_FILE_SUFFIX = ".cases.json"
PAYLOAD_FILE_SUFFIX = ".payload.json"
SIDECAR_SUFFIXES = (CASES_FILE_SUFFIX, PAYLOAD_FILE_SUFFIX)

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# One adversarial persona, covering both traits worth a dedicated call:
# hostile (the common difficult caller) and interrupting mid-sentence (the
# only thing that exercises real barge-in). Each costs a real minute.
ADVERSARIAL = {
    "hostile_interrupt": (
        "You are angry about being called at all — short, irritated, pushing "
        "back on everything. You interrupt mid-sentence with a correction, "
        "then contradict it later, before finally reacting to the real issue."
    )
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Speaker(str, Enum):
    AGENT = "agent"
    CUSTOMER = "customer"


class EndReason(str, Enum):
    ENDED = "ended"  # the agent called end_conversation — the healthy path
    TRANSFERRED = "transferred"
    MAX_TURNS = "max_turns"  # runaway valve (turns or wall-clock) — a bug
    PERSONA_DONE = "persona_done"  # the customer had nothing left to say
    ERROR = "error"


class Stability(str, Enum):
    """How a case behaved across its repetitions. Only CONFIRMED_FAIL may
    drive a patch; only CONFIRMED_PASS -> CONFIRMED_FAIL is a regression.
    FLAKY is reported and never acted on — acting on noise is what turned an
    earlier iterate loop into a random walk."""

    CONFIRMED_PASS = "confirmed_pass"
    CONFIRMED_FAIL = "confirmed_fail"
    FLAKY = "flaky"


@dataclass
class Turn:
    speaker: Speaker
    text: str
    at_s: Optional[float] = None


@dataclass
class ToolCallRecord:
    name: str
    arguments: Dict[str, Any]
    result: Optional[Any] = None
    at_s: Optional[float] = None


@dataclass
class HookRecord:
    name: str
    function_name: str
    resolved_fields: Dict[str, Any]


@dataclass
class SimTrace:
    """Everything one simulated call produced — the sole input to grading."""

    case_id: str
    repetition: int = 0
    template_name: str = ""
    turns: List[Turn] = field(default_factory=list)
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    hooks: List[HookRecord] = field(default_factory=list)
    node_traversal: List[Dict[str, Any]] = field(default_factory=list)
    nodes_visited: List[str] = field(default_factory=list)
    final_node: Optional[str] = None
    end_reason: EndReason = EndReason.ERROR
    error: Optional[str] = None
    duration_s: float = 0.0
    agent_model: Optional[str] = None
    stt_heard: List[str] = field(default_factory=list)
    # What the persona MEANT to say, kept beside what STT actually heard —
    # the gap between the two lists is the finding audio mode exists for.
    persona_said: List[str] = field(default_factory=list)
    interruptions: List[float] = field(default_factory=list)
    audio_wav: bytes = b""
    audio_path: Optional[str] = None

    @property
    def agent_utterances(self) -> List[str]:
        return [t.text for t in self.turns if t.speaker is Speaker.AGENT]

    @property
    def customer_turns(self) -> int:
        return sum(1 for t in self.turns if t.speaker is Speaker.CUSTOMER)

    @property
    def called_function_names(self) -> List[str]:
        return [c.name for c in self.tool_calls]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "repetition": self.repetition,
            "template_name": self.template_name,
            "end_reason": self.end_reason.value,
            "final_node": self.final_node,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
            "agent_model": self.agent_model,
            "nodes_visited": self.nodes_visited,
            "turns": [
                {"speaker": t.speaker.value, "text": t.text, "at_s": t.at_s}
                for t in self.turns
            ],
            "tool_calls": [
                {
                    "name": c.name,
                    "arguments": c.arguments,
                    "result": c.result,
                    "at_s": c.at_s,
                }
                for c in self.tool_calls
            ],
            "hooks": [
                {
                    "name": h.name,
                    "function_name": h.function_name,
                    "resolved_fields": h.resolved_fields,
                }
                for h in self.hooks
            ],
            "persona_said": self.persona_said,
            "stt_heard": self.stt_heard,
            "interruptions": self.interruptions,
            "node_traversal": self.node_traversal,
        }


@dataclass
class Grounding:
    """The literal template text that entitles an expectation to exist."""

    source_path: str  # e.g. flow.nodes[greeting].functions[paid].transition_to
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "evidence": self.evidence,
        }


@dataclass
class Expectation:
    """What a case asserts. An ungrounded field is never asserted, however it
    got set — so a hand-edited cases file cannot smuggle in a requirement."""

    terminal_node: Optional[str] = None
    function: Optional[str] = None
    acceptable_functions: List[str] = field(default_factory=list)
    outcome_fields: Dict[str, Any] = field(default_factory=dict)
    required_args: List[str] = field(default_factory=list)
    any_terminal: bool = False
    grounding: Dict[str, Grounding] = field(default_factory=dict)

    def is_grounded(self, name: str) -> bool:
        return name in self.grounding

    @property
    def asserts_anything(self) -> bool:
        return bool(self.grounding)


@dataclass
class SimCase:
    """One scenario: who the customer is, what the call carries, what a
    correct agent does about it."""

    id: str
    persona: str
    tier: str = "A"
    payload: Dict[str, Any] = field(default_factory=dict)
    expect: Expectation = field(default_factory=Expectation)
    rationale: str = ""
    may_interrupt: bool = False
    # Sibling functions this persona must NOT drift into (see author_personas).
    avoid: List[str] = field(default_factory=list)
    # Every declared function's own description, so a failure can be explained
    # in the template's own words (see grade.explain_failure) — a template
    # whose name and description disagree is the defect, not the agent.
    descriptions: Dict[str, str] = field(default_factory=dict)
    reached_via: List[str] = field(default_factory=list)

    @property
    def invariant_only(self) -> bool:
        return not self.expect.asserts_anything

    def to_dict(self) -> Dict[str, Any]:
        e = self.expect
        return {
            "id": self.id,
            "tier": self.tier,
            "persona": self.persona,
            "payload": self.payload,
            "rationale": self.rationale,
            "may_interrupt": self.may_interrupt,
            "invariant_only": self.invariant_only,
            "expect": {
                "terminal_node": e.terminal_node,
                "function": e.function,
                "acceptable_functions": e.acceptable_functions,
                "outcome_fields": e.outcome_fields,
                "required_args": e.required_args,
                "any_terminal": e.any_terminal,
                "grounding": {k: g.to_dict() for k, g in e.grounding.items()},
            },
        }


@dataclass
class Assertion:
    name: str
    passed: bool
    detail: str = ""
    critical: bool = True


@dataclass
class CaseResult:
    case_id: str
    repetition: int
    assertions: List[Assertion]
    trace: SimTrace
    judge: Optional[Dict[str, Any]] = None
    tier: str = "A"
    diagnosis: Optional[str] = None

    @property
    def passed(self) -> bool:
        return all(a.passed for a in self.assertions if a.critical)


@dataclass
class CaseVerdict:
    case_id: str
    tier: str
    stability: Stability
    passes: int
    total: int
    invariant_only: bool = False
    primary_failure: Optional[str] = None
    detail: str = ""
    diagnosis: Optional[str] = None
    judge_score: Optional[float] = None

    @property
    def passed(self) -> bool:
        return self.stability is Stability.CONFIRMED_PASS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "tier": self.tier,
            "stability": self.stability.value,
            "passes": self.passes,
            "total": self.total,
            "invariant_only": self.invariant_only,
            "primary_failure": self.primary_failure,
            "detail": self.detail,
            "diagnosis": self.diagnosis,
            "judge_score": self.judge_score,
        }


# ---------------------------------------------------------------------------
# Reading the template
# ---------------------------------------------------------------------------


def iter_functions(template: TemplateModel) -> List[Tuple[str, Dict[str, Any]]]:
    """(node_name, function) for every LLM-visible node function."""
    flow = template.flow or {}
    out = [
        (node.get("node_name") or "", fn)
        for node in flow.get("nodes") or []
        for fn in node.get("functions") or []
        if isinstance(fn, dict)
    ]
    out += [
        ("__direct__", fn) for fn in flow.get("functions") or [] if isinstance(fn, dict)
    ]
    return out


def fn_name(fn: Dict[str, Any]) -> str:
    return fn.get("name") or fn.get("function_name") or ""


def node_names(template: TemplateModel) -> List[str]:
    return [
        n.get("node_name")
        for n in (template.flow or {}).get("nodes") or []
        if n.get("node_name")
    ]


def terminal_nodes(template: TemplateModel) -> List[str]:
    """Nodes the template itself declares as endings — two literal signals:
    a node with no functions cannot be left, and a node whose post_actions
    include end_conversation closes the call. Neither is inferred from prose."""
    out: List[str] = []
    for node in (template.flow or {}).get("nodes") or []:
        name = node.get("node_name")
        if not name:
            continue
        ends = any(
            isinstance(a, dict)
            and (
                a.get("handler") == "end_conversation"
                or a.get("name") == "end_conversation"
            )
            for a in (node.get("post_actions") or [])
        )
        if ends or not node.get("functions"):
            out.append(name)
    return out


def _path_to_node(template: TemplateModel, target: str) -> List[str]:
    """The shortest chain of function names that must fire, in order, to reach
    ``target`` from the template's own start node — empty if ``target`` IS the
    start (or unreachable). Read straight off ``transition_to`` edges, the
    same literal signal every other grounded claim in this file uses."""
    nodes = {n.get("node_name"): n for n in (template.flow or {}).get("nodes") or []}
    start = "initial" if "initial" in nodes else next(iter(nodes), None)
    if not start or target == start or start not in nodes:
        return []
    from collections import deque

    seen = {start}
    queue: deque = deque([(start, [])])
    while queue:
        node_name, path = queue.popleft()
        for fn in (nodes.get(node_name) or {}).get("functions") or []:
            name, dest = fn_name(fn), fn.get("transition_to")
            if not name or not dest or dest in seen:
                continue
            chain = path + [name]
            if dest == target:
                return chain
            seen.add(dest)
            queue.append((dest, chain))
    return []


def _static_outcome_fields(fn: Dict[str, Any]) -> Dict[str, Any]:
    """The values the template guarantees the hook will write."""
    return {
        field_name: spec.get("value")
        for hook in fn.get("hooks") or []
        for field_name, spec in (hook.get("expected_fields") or {}).items()
        if isinstance(spec, dict) and spec.get("source") == "static"
    }


def _llm_required_args(fn: Dict[str, Any]) -> List[str]:
    props = fn.get("properties") or {}
    return [a for a in (fn.get("required") or []) if a in props]


def mcp_tool_schemas(template: TemplateModel) -> List[Dict[str, Any]]:
    cfg = template.configurations
    mcp = getattr(cfg, "mcp", None) if cfg else None
    return [
        schema
        for server in (getattr(mcp, "servers", None) or [])
        if getattr(server, "enabled", False)
        for schema in (getattr(server, "tool_schemas", None) or [])
    ]


def _equivalent_functions(template: TemplateModel) -> Dict[str, List[str]]:
    """Functions with the same outcome AND destination are interchangeable —
    a template declaring user_busy and user_became_busy identically makes no
    observable promise about which one the agent picks."""
    groups: Dict[str, List[str]] = {}
    for _node, fn in iter_functions(template):
        name = fn_name(fn)
        if not name:
            continue
        key = json.dumps(
            {
                "outcome": _static_outcome_fields(fn),
                "to": fn.get("transition_to"),
                "args": sorted(_llm_required_args(fn)),
            },
            sort_keys=True,
            default=str,
        )
        groups.setdefault(key, []).append(name)
    return {
        name: [n for n in group if n != name]
        for group in groups.values()
        for name in group
    }


def template_placeholders(template: TemplateModel) -> List[str]:
    blob = json.dumps(template.flow or {}) + json.dumps(
        template.configurations.model_dump() if template.configurations else {},
        default=str,
    )
    return sorted(set(_PLACEHOLDER.findall(blob)))


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def default_payload(
    template: TemplateModel, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The schema's own ``example`` values, type-shaped stand-ins otherwise.
    Overrides win per key but never remove one — a partially filled real
    payload must not leave the rest of the fields empty."""
    payload: Dict[str, Any] = {}
    for key, spec in (template.expected_payload_schema or {}).items():
        if not isinstance(spec, dict):
            continue
        if "example" in spec:
            payload[key] = spec["example"]
        elif spec.get("type") == "number":
            payload[key] = 499
        elif spec.get("type") == "boolean":
            payload[key] = True
        else:
            payload[key] = f"sample {key.replace('_', ' ')}"
    payload.update(overrides or {})
    return payload


def _render_placeholders(text: str, payload: Dict[str, Any]) -> str:
    """A persona brief must never carry {vars} — the persona would speak them."""
    for key, value in (payload or {}).items():
        text = text.replace("{" + key + "}", str(value))
    return _PLACEHOLDER.sub(lambda m: m.group(1).replace("_", " "), text)


# ---------------------------------------------------------------------------
# Deriving the suite
# ---------------------------------------------------------------------------


def structural_cases(
    template: TemplateModel, payload: Optional[Dict[str, Any]] = None
) -> List[SimCase]:
    """Tier A — one case per node function. This is the only tier that makes
    real assertions, because it is the only place the template makes a
    checkable promise: this function goes to that node and writes these
    outcome fields, all declared literally."""
    payload = default_payload(template) if payload is None else payload
    equivalents = _equivalent_functions(template)
    known_nodes = set(node_names(template))
    endings = set(terminal_nodes(template))
    all_names = [fn_name(fn) for _n, fn in iter_functions(template) if fn_name(fn)]

    # A function name is unique only WITHIN a node; qualify the id only where
    # it actually repeats, so the common case keeps a short readable id.
    repeated = {n for n in all_names if all_names.count(n) > 1}
    descriptions = {
        fn_name(fn): (fn.get("description") or "").strip()
        for _n, fn in iter_functions(template)
        if fn_name(fn)
    }
    cases: List[SimCase] = []

    for node_name, fn in iter_functions(template):
        name = fn_name(fn)
        if not name:
            continue
        case_id = f"A-{node_name}-{name}" if name in repeated else f"A-{name}"
        description = (fn.get("description") or "").strip()
        base = f"flow.nodes[{node_name}].functions[{name}]"
        expect = Expectation(
            function=name, acceptable_functions=equivalents.get(name, [])
        )
        expect.grounding["function"] = Grounding(
            base, description or f"{name} is declared on node {node_name}"
        )

        target = fn.get("transition_to")
        if target and target in known_nodes and target in endings:
            expect.terminal_node = target
            expect.grounding["terminal_node"] = Grounding(
                f"{base}.transition_to", str(target)
            )
        elif target and target not in known_nodes:
            logger.warning(
                f"[sim] {name}.transition_to={target!r} names no node; not asserted"
            )

        outcome = _static_outcome_fields(fn)
        if outcome:
            expect.outcome_fields = outcome
            expect.grounding["outcome_fields"] = Grounding(
                f"{base}.hooks[].expected_fields(static)",
                json.dumps(outcome, ensure_ascii=False, default=str),
            )

        required = _llm_required_args(fn)
        if required:
            expect.required_args = required
            expect.grounding["required_args"] = Grounding(
                f"{base}.required", json.dumps(required)
            )

        cases.append(
            SimCase(
                id=case_id,
                tier="A",
                # The template's own description IS the brief. Nothing invented.
                persona=(
                    "You are the customer on this call. Behave so that the "
                    f"following becomes true:\n{description}\n"
                    "Get there naturally over a couple of turns."
                ),
                payload=dict(payload),
                expect=expect,
                avoid=[
                    other
                    for other in sorted(set(all_names))
                    if other != name and other not in expect.acceptable_functions
                ],
                descriptions=descriptions,
                reached_via=_path_to_node(template, node_name),
                rationale=(
                    f"`{name}` is declared on `{node_name}`"
                    + (f", transitions to `{target}`" if expect.terminal_node else "")
                    + (
                        f", writes {json.dumps(outcome, default=str)}"
                        if outcome
                        else ""
                    )
                ),
            )
        )
    return cases


def adversarial_cases(
    template: TemplateModel, payload: Optional[Dict[str, Any]] = None
) -> List[SimCase]:
    """Tier B — a difficult caller. **No case here names a function or node.**
    The template never says which branch a hostile caller belongs in, so the
    only grounded claim is that the call reaches SOME declared ending. Where
    it actually landed is reported as an observation for a human."""
    payload = default_payload(template) if payload is None else payload
    terminals = terminal_nodes(template)
    out: List[SimCase] = []
    for key, brief in ADVERSARIAL.items():
        expect = Expectation()
        if terminals:
            expect.any_terminal = True
            expect.grounding["any_terminal"] = Grounding(
                "flow.nodes[].post_actions[end_conversation]",
                f"declared terminal nodes: {terminals}",
            )
        out.append(
            SimCase(
                id=f"B-{key}",
                tier="B",
                persona=brief,
                payload=dict(payload),
                expect=expect,
                may_interrupt=True,  # actually speaks over the agent
                rationale=(
                    "Adversarial caller: only that the agent stays well-behaved "
                    "and still closes the call. Exercises real barge-in."
                ),
            )
        )
    return out


def tool_cases(
    template: TemplateModel, payload: Optional[Dict[str, Any]] = None
) -> List[SimCase]:
    """Tier T — tools a caller reaches by asking, not by being classified.

    A declared global function carries its own name/description/required args,
    so it can be asserted. An MCP catalog tool is discovered at runtime: the
    template says it exists, never when the agent must reach for it, so that
    case is invariant-only."""
    payload = default_payload(template) if payload is None else payload
    cases: List[SimCase] = []
    globals_ = (template.flow or {}).get("global_functions") or []
    descriptions = {
        fn_name(fn): (fn.get("description") or "").strip()
        for _n, fn in iter_functions(template)
        if fn_name(fn)
    }
    descriptions.update(
        {
            gf["name"]: (gf.get("description") or "").strip()
            for gf in globals_
            if gf.get("name")
        }
    )
    catalog = sorted(
        {t.get("name") for t in mcp_tool_schemas(template) if t.get("name")}
    )
    if catalog:
        product = payload.get("product_name") or "the product you already own"
        cases.append(
            SimCase(
                id="T-product_question",
                tier="T",
                persona=(
                    f"You are curious about OTHER products this store sells, not "
                    f"{product}. Ask what else they have, then ask what it does "
                    "and what it costs. You are only browsing."
                ),
                payload=dict(payload),
                rationale=(
                    f"MCP tools ({', '.join(catalog)}) are enabled but the template "
                    "never says when to use them — no tool call is asserted."
                ),
            )
        )

    for gf in globals_:
        name, required = gf.get("name"), gf.get("required") or []
        # A global with no required args always means the call's own subject,
        # which Tier A already covers. Needing the LLM to name something is
        # what marks a genuinely separate path.
        if not name or gf.get("type") != "http" or not required:
            continue
        description = (gf.get("description") or "").strip()
        base = f"flow.global_functions[{name}]"
        cases.append(
            SimCase(
                id=f"T-{name}",
                tier="T",
                persona=(
                    "You are the customer on this call. Steer the conversation so "
                    f"this becomes what you want:\n{description}\nAsk in your own words."
                ),
                payload=dict(payload),
                expect=Expectation(
                    function=name,
                    required_args=list(required),
                    grounding={
                        "function": Grounding(
                            f"{base}.description", description or name
                        ),
                        "required_args": Grounding(
                            f"{base}.required", json.dumps(list(required))
                        ),
                    },
                ),
                avoid=[o for o in sorted(descriptions) if o != name],
                descriptions=descriptions,
                rationale=f"`{name}` is a declared global HTTP function requiring {list(required)}.",
            )
        )
    return cases


def build_suite(
    template: TemplateModel, payload_overrides: Optional[Dict[str, Any]] = None
) -> List[SimCase]:
    payload = default_payload(template, payload_overrides)
    return (
        structural_cases(template, payload)
        + adversarial_cases(template, payload)
        + tool_cases(template, payload)
    )


def prompt_digest(template: TemplateModel) -> str:
    """The agent's own instructions, as text — every node's prompt and every
    function's description.

    The judge needs this. Asked to rate "policy adherence" from a transcript
    alone, it rates the agent against a policy IT thinks ought to exist: a real
    run scored an agent 1/5 for "confirming a cancellation without verification"
    when the template's own node said, verbatim, to confirm the cancellation.
    That is an invented expectation — the exact thing every other part of this
    harness refuses to do — and it was failing cases, because a catastrophic
    policy score is critical.
    """
    out: List[str] = []
    for node in (template.flow or {}).get("nodes") or []:
        out.append(f"## node `{node.get('node_name')}`")
        for key in ("role_messages", "task_messages"):
            for message in node.get(key) or []:
                content = (
                    message.get("content") if isinstance(message, dict) else message
                )
                if content:
                    out.append(str(content).strip())
        for fn in node.get("functions") or []:
            if isinstance(fn, dict):
                out.append(
                    f"- tool `{fn_name(fn)}`: {(fn.get('description') or '').strip()}"
                )
    return "\n".join(out)


def validate_suite(template: TemplateModel, cases: List[SimCase]) -> List[str]:
    """Problems with the SUITE, not the agent — a case asserting a node that
    no longer exists can never pass, and reporting that as an agent failure is
    the false signal this harness exists to avoid."""
    problems: List[str] = []
    known_nodes = set(node_names(template))
    known_functions = {fn_name(fn) for _n, fn in iter_functions(template)}
    known_functions |= {
        gf.get("name") for gf in (template.flow or {}).get("global_functions") or []
    }
    known_functions |= {t.get("name") for t in mcp_tool_schemas(template)}
    seen: set = set()

    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate case id")
        seen.add(case.id)
        exp = case.expect
        if exp.terminal_node and exp.terminal_node not in known_nodes:
            problems.append(
                f"{case.id}: expects node {exp.terminal_node!r}, not defined"
            )
        for name in filter(None, [exp.function, *exp.acceptable_functions]):
            if name not in known_functions:
                problems.append(f"{case.id}: expects function {name!r}, not declared")
        for name in ("terminal_node", "function", "outcome_fields", "required_args"):
            if getattr(exp, name, None) and not exp.is_grounded(name):
                problems.append(f"{case.id}: asserts {name} with no grounding")
    return problems


def save_suite(cases: List[SimCase], path: str) -> None:
    """Write-only: the run's own record of what it tested, for a person to
    read. Nothing loads it back — structure is always re-derived from the
    template, so a hand-edited copy could never take effect anyway."""
    with open(path, "w") as f:
        json.dump([c.to_dict() for c in cases], f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM authoring — prose only, never expectations
# ---------------------------------------------------------------------------

_AUTHOR_PROMPT = """\
You write CUSTOMER personas for testing a voice agent.

You are given ONE function the agent can call, described from the AGENT's
point of view. Invert it: how must a CUSTOMER behave for that function to
become the correct one?

Write in second person ("You are..."), 2-4 sentences: who they are, what they
want, what they will say or refuse so the described situation arises over a
few turns, and any detail the agent must extract from them.

HARD RULES — a persona breaking these tests the wrong thing and blames the
agent for the suite's mistake:
1. The customer MUST end up in the situation the description names. Do not
   soften it, do not have them want something else.
2. You are given OTHER functions this template declares. The customer must
   NOT behave in a way that makes one of those the correct call instead. If
   the target is "customer wants a reshipment", the customer does not ask for
   a refund.
3. The CALL RECORD is who this customer is and what they already bought — use
   its names, amounts and currency exactly, never contradict it. It is NOT a
   limit on what they may ask for. When the description is about something
   OTHER than what they bought (a different product, another item in the
   catalogue), the customer must ask for that other thing and name it plainly
   — writing the recorded product in instead inverts the whole scenario.
4. Never mention functions, outcomes, nodes, prompts or testing. Never use
   curly placeholders. They are just a person on a phone call.
5. The customer is MALE — the simulator voices them with a male voice. Give
   them a man's name and write them as a man. A female character contradicts
   the voice, and the persona then speaks feminine forms over it.
6. Write every amount and number the way it is SPOKEN — "eighteen ninety-nine
   rupees", never "₹1899". This brief is read aloud by a persona that copies
   its wording; a glyph-and-digits amount is mis-spoken and mis-heard, and the
   sentence carrying it gets split in two.

Return ONLY JSON: {"persona": "..."}
"""

_PAYLOAD_PROMPT = """\
You invent a realistic payload for ONE test phone call.

Given a voice-agent template's payload schema and the {variables} its prompts
reference, generate the values a real record for this call would carry.

- Cover every schema field; invent nothing outside it.
- Match the declared type exactly (string/number/boolean).
- Plausible and pronounceable on a phone call: a full person's name, a
  real-looking mobile number, a business a caller would recognize.
- Never use "sample", "test", "placeholder", "dummy", "example", or braces.

Return ONLY JSON: {"field": value, ...}
"""


def decode_llm_json(content: Any) -> Dict[str, Any]:
    """Parse a model reply that may or may not be fenced. Returns {} rather
    than raising — a malformed reply is a soft failure everywhere it's used."""
    text = str(content or "").strip()
    match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n?```", text)
    if match:
        text = match.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


async def author_payload(template: TemplateModel, llm: Any) -> Dict[str, Any]:
    """One GRID call: realistic values for the template's own schema. Returns
    {} on any failure — the synthesized defaults remain as the floor."""
    schema = {
        k: v
        for k, v in (template.expected_payload_schema or {}).items()
        if isinstance(v, dict)
    }
    if not schema:
        return {}

    context = f"Payload schema: {json.dumps(schema, ensure_ascii=False, default=str)}\n"
    used = template_placeholders(template)
    if used:
        context += f"Variables the prompts reference: {', '.join(used)}\n"
    try:
        raw = await with_retry(
            lambda: llm.run_inference(
                LLMContext([{"role": "user", "content": context}]),
                system_instruction=_PAYLOAD_PROMPT,
            ),
            label="author payload",
        )
        data = decode_llm_json(raw)
    except Exception as e:
        logger.warning(f"[sim] payload authoring failed; defaults stay: {e}")
        return {}

    # Coerce to the schema's declared types and drop anything not in it — a
    # hallucinated extra field would flow straight into prompt rendering.
    out: Dict[str, Any] = {}
    for key, spec in schema.items():
        if key not in data:
            continue
        value, kind = data[key], spec.get("type")
        if kind == "number" and not isinstance(value, bool):
            try:
                out[key] = float(value) if "." in str(value) else int(value)
            except (TypeError, ValueError):
                continue
        elif kind == "boolean":
            out[key] = (
                value
                if isinstance(value, bool)
                else str(value).lower() in ("1", "true", "yes")
            )
        elif isinstance(value, str) and value.strip():
            out[key] = value
        elif value is not None and kind not in ("number", "boolean"):
            out[key] = value
    return out


async def author_personas(
    template: TemplateModel,
    cases: List[SimCase],
    llm: Any,
    *,
    concurrency: int = 5,
    payload: Optional[Dict[str, Any]] = None,
    on_done: Any = None,
) -> List[SimCase]:
    """Rewrite briefs from agent-facing prose into customer behaviour.

    **Touches ``case.persona`` and nothing else.** Expectations are derived
    structurally and are not an LLM's to invent. Each call is told the sibling
    functions it must not drift into — a persona that asks for a refund in the
    reshipment case fails the template for the suite's own mistake.

    Every case targeting a declared function is rewritten, Tier T included: a
    description is written for the BOT, and left as-is it reaches the persona
    model as a character brief that is not a character at all.
    """
    by_name = {fn_name(fn): fn for _node, fn in iter_functions(template)}
    # Global functions are what Tier T cases target.
    for gf in (template.flow or {}).get("global_functions") or []:
        if gf.get("name"):
            by_name.setdefault(gf["name"], gf)
    payload = payload or {}
    targets = [
        c
        for c in cases
        if c.tier in ("A", "T") and (c.expect.function or "") in by_name
    ]
    if not targets:
        return cases

    sem = asyncio.Semaphore(concurrency)

    async def one(case: SimCase) -> None:
        fn = by_name[case.expect.function or ""]
        description = (fn.get("description") or "").strip()
        if not description:
            case.persona = _render_placeholders(case.persona, payload)
            return
        context = (
            f"Target function: {fn_name(fn)}\n"
            f"Agent-facing description: {description}\n"
            f"Call record (the customer's own facts): "
            f"{json.dumps(payload, ensure_ascii=False, default=str)}\n"
        )
        if case.avoid:
            context += (
                "Other functions this template declares — the customer must NOT "
                "behave so one of these becomes correct instead:\n"
                + "\n".join(
                    f"  - {name}: {(by_name[name].get('description') or '').strip()[:200]}"
                    for name in case.avoid
                    if name in by_name
                )
                + "\n"
            )
        if case.expect.required_args:
            context += (
                "The agent must obtain these from the customer's own words: "
                f"{', '.join(case.expect.required_args)}\n"
            )
        if case.reached_via:
            context += (
                "This situation is not reachable from the start of the call — "
                "the conversation must ALREADY have gone through these, in "
                "order, before it can happen:\n"
                + "\n".join(
                    f"  {i}. {by_name[n].get('description', '').strip()}"
                    for i, n in enumerate(case.reached_via, 1)
                    if n in by_name
                )
                + "\nPlay through those first, in your own words, THEN do what "
                "is described above. Do not skip straight to it.\n"
            )
        async with sem:
            try:
                raw = await with_retry(
                    lambda: llm.run_inference(
                        LLMContext([{"role": "user", "content": context}]),
                        system_instruction=_AUTHOR_PROMPT,
                    ),
                    label=f"author {case.id}",
                )
                persona = str(decode_llm_json(raw).get("persona") or "").strip()
            except Exception as e:
                logger.warning(f"[sim] persona authoring failed for {case.id}: {e}")
                persona = ""
        case.persona = _render_placeholders(persona or case.persona, payload)
        if on_done:
            on_done(case)

    await asyncio.gather(*(one(c) for c in targets))
    return cases
