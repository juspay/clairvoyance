"""The playbook (modules/05-outreach §The playbook; canon T19 col 6) — PURE.

The agent is an actor and its template is a script with holes in it. What
fills them is decided HERE, from what the event said, and handed over
finished: the walker picks a row per block, renders the named lines, and the
agent's single substitution pass has nothing left to guess.

Why the plan and not the vendor's schema (the 17 Sep ruling,
modules/05-outreach §The playbook):
a registration is LIVE — it changes under open runs, it is shared by every
plan on the topic, and its item_format caps at 160 characters, which is no
room for a twelve-step walk. The playbook is part of the document, so
publish copies it into the version row: a journey that started on script v3
keeps saying v3, and a wording fix reaches open runs only with
`on_publish: migrate`.

Three rules hold the shape, and each is one sentence:

  1. Every piece of text lives in `lines`, once. A block names lines and
     never holds a sentence.
  2. `say` is a name, or an ordered list of names.
  3. `transform` says how a FACT renders, once for the whole plan.

Its vocabulary is the system's own: `when` is the where-grammar over the
condition square's field grammar and first-match-wins with a mandatory
default is that square's `else`; a line with {holes} is the send node's
blank law; and `transform` names the built-ins a buddy template's
expected_payload_schema already names.

A line fills from FACTS only, never from another line — no chains, no
cycles, always total.
"""

import inspect
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.core.logger import logger
from app.crm.identity.contracts import CustomerFacts
from app.crm.outreach import predicates
from app.crm.outreach.nodes.context import is_bookkeeping
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import PlaybookBlock, Transform, WorkflowDefinition
from app.crm.shared.predicate import matches
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY

#: Keys the call square writes into the lead payload itself — a block of the
#: same name would overwrite one, silently.
RESERVED_PAYLOAD_KEYS = frozenset({"customer_mobile_number", "reporting_webhook_url"})

#: A hole in a line, spelled like a template blank and read like one.
HOLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: How one list-block renders: the ORDER and LINES sections of the prompt,
#: generated. A call agent reads them as steps; the name rides along so the
#: agent can say "step_open_app is done" without inventing an id.
_STEP = '- {name}: "{text}"'


def names_said(say: Any) -> List[str]:
    """PURE: the line names a `say` names, one or many."""
    return [say] if isinstance(say, str) else list(say)


def holes_in(text: str) -> List[str]:
    """PURE: the facts a line asks the run for."""
    return HOLE.findall(text)


def block_names(definition: WorkflowDefinition) -> Set[str]:
    """PURE: every block this document declares — what catalog_laws checks a
    door's facts for collisions against."""
    pb = definition.playbook
    return set(pb.blocks) if pb else set()


def line_holes(definition: WorkflowDefinition) -> List[str]:
    """PURE: every fact every line asks for, for the catalog's declared-name
    law — the same allow-list an action square's args answer to, because
    `{order_id}` in a line and `{order_id}` in args are the same lookup
    against the same run_facts."""
    pb = definition.playbook
    if pb is None:
        return []
    return [hole for text in pb.lines.values() for hole in holes_in(text)]


def multiline_blocks(definition: WorkflowDefinition) -> Set[str]:
    """PURE: blocks whose `say` is a list ANYWHERE — they render many lines,
    so they may reach a call or a webhook but never a WhatsApp blank, which
    carries no line break."""
    pb = definition.playbook
    if pb is None:
        return set()
    return {
        name
        for name, rows in pb.blocks.items()
        if any(isinstance(row.say, list) for row in rows)
    }


def needs_customer(definition: WorkflowDefinition, wanted: Iterable[str]) -> bool:
    """PURE: does any row of the ASKED-FOR blocks read the customer? The one
    DB read a `when` may cost is paid only when a row asks for it, exactly as
    a condition square pays it."""
    pb = definition.playbook
    if pb is None:
        return False
    return any(
        condition.field.startswith(predicates.CUSTOMER_PREFIX)
        for name in wanted
        if name in pb.blocks
        for row in pb.blocks[name]
        for condition in row.when
    )


def laws(definition: WorkflowDefinition) -> List[str]:
    """PURE decide: why this playbook may not be published (empty = fine).

    Judged at publish so a typo is a sentence the author reads, never a
    blank page on a live call. The hole-against-catalog law needs a door's
    declared names, so it lives with the other catalog laws
    (outreach/catalog_laws.py) and is not re-spelled here.
    """
    pb = definition.playbook
    if pb is None:
        return []
    problems: List[str] = []
    node_ids = [node.id for node in definition.nodes]

    known = ", ".join(sorted(TEMPLATE_FUNCTION_REGISTRY))
    spelled = {hole for text in pb.lines.values() for hole in holes_in(text)}
    # The half a typo fails SILENTLY on: the hole still resolves and the
    # number is read raw, which is what `transform` exists to stop.
    for fact in sorted(set(pb.transform) - spelled):
        problems.append(
            f"playbook transform {fact!r}: no line spells {{{fact}}} — a fact "
            "says how it reads where it is READ"
        )

    for fact, says in sorted(pb.transform.items()):
        for fn in says.function:
            func = TEMPLATE_FUNCTION_REGISTRY.get(fn)
            if func is None:
                problems.append(
                    f"playbook transform {fact!r}: {fn!r} is not a built-in — "
                    f"one of {known}"
                )
                continue
            if fn in _LIST_ONLY:
                problems.append(
                    f"playbook transform {fact!r}: {fn!r} reads a LIST, and a "
                    "hole is always one value — declare the list on the "
                    "catalog field with an item_format instead"
                )
                continue
            # Unsupplied, it would raise on every value of every run.
            for missing in _missing_arguments(func, says.params):
                problems.append(
                    f"playbook transform {fact!r}: {fn!r} needs {missing!r} — "
                    f'give it in params, e.g. "params": {{"{missing}": ...}}'
                )
        # A params key no function in the pipeline declares is a typo that
        # would otherwise be dropped in silence.
        taken = {
            name
            for fn in says.function
            if (parameters := _parameters(TEMPLATE_FUNCTION_REGISTRY.get(fn)))
            for name in list(parameters)[1:]
        }
        for spare in sorted(set(says.params) - taken):
            problems.append(
                f"playbook transform {fact!r}: no function it names takes "
                f"{spare!r} — {', '.join(says.function) or 'none named'}"
            )

    for name in sorted(pb.blocks):
        if is_bookkeeping(name):
            problems.append(
                f"playbook block {name!r} is a walker bookkeeping name — "
                "run_facts filters those out of a payload on purpose, and a "
                "block is merged after the filter"
            )
        if name in RESERVED_PAYLOAD_KEYS:
            problems.append(
                f"playbook block {name!r} is a reserved lead-payload key — the "
                "call square writes it itself"
            )

    for node in definition.nodes:
        if node.blocks and node.type != "call":
            problems.append(
                f"node {node.id}: blocks belongs to a call — a send names its "
                "blocks on the right of variables, an action inside args"
            )
        for name in node.blocks:
            if name not in pb.blocks:
                problems.append(
                    f"node {node.id}: {name!r} is not a playbook block — a square "
                    "may only ask for what the playbook declares"
                )

    # A multi-line block may reach a call or a webhook, never a WhatsApp
    # blank, which carries no line break (the send node's existing law) —
    # at publish, not at the provider two modules away.
    many = multiline_blocks(definition)
    for node in definition.nodes:
        for blank, fact in node.variables.items():
            if fact in many:
                problems.append(
                    f"send node {node.id}: variable {blank!r} <- {fact!r} renders "
                    "many lines — a template blank is one line"
                )

    for name, rows in pb.blocks.items():
        if not rows:
            problems.append(f"playbook block {name!r}: needs at least one row")
            continue
        if rows[-1].when:
            problems.append(
                f"playbook block {name!r}: the last row must have no `when` — it "
                "is the default, and it is what keeps the agent from speaking a "
                f"literal {{{name}}} on a live call"
            )
        for index, row in enumerate(rows[:-1]):
            if not row.when:
                problems.append(
                    f"playbook block {name!r} row {index}: a row before the last "
                    "needs a `when` — an early default makes every row under it "
                    "unreachable"
                )
        for index, row in enumerate(rows):
            for field in sorted({c.field for c in row.when}):
                problems.extend(
                    f"playbook block {name!r} row {index}: {problem}"
                    for problem in predicates.field_problems(field, node_ids)
                )
            for line in names_said(row.say):
                if line not in pb.lines:
                    problems.append(
                        f"playbook block {name!r} row {index}: {line!r} is not in "
                        "lines"
                    )

    for line, text in sorted(pb.lines.items()):
        if not text.strip() and text != "":
            problems.append(f"playbook line {line!r} is blank")
        if "\n" in text or "\r" in text:
            problems.append(
                f"playbook line {line!r} carries a line break — one line is one "
                "line, and a list block is what renders many"
            )
    return problems


#: Built-ins whose subject is an ARRAY — a hole's value is always a scalar,
#: so naming one could only ever be a no-op.
_LIST_ONLY = ("format_array",)


def _parameters(func: Any) -> Optional[Any]:
    """The built-in's parameters, or None when the signature cannot be read
    (a C builtin, a patched __signature__) — the laws below then refuse
    nothing, because they refuse only what they can prove wrong."""
    try:
        return inspect.signature(func).parameters
    except (TypeError, ValueError):
        return None


def _accepted(func: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: the subset of one `params` table this function declares.

    The table serves the whole pipeline, so ["string_trim", "trim_words"]
    with {"words": [...]} must not die on string_trim, which takes none."""
    if not params:
        return {}
    parameters = _parameters(func)
    if parameters is None:
        return {}
    # [1:] skips the SUBJECT — the value the hole passes positionally. A
    # params key named after it (eight of the eleven call theirs `value`)
    # would otherwise be passed a second time by keyword, and the call dies
    # on "multiple values for argument".
    beyond_the_value = list(parameters)[1:]
    return {k: v for k, v in params.items() if k in beyond_the_value}


def _missing_arguments(func: Any, params: Dict[str, Any]) -> List[str]:
    """PURE: the arguments this built-in needs beyond the value, and the
    plan did not give it — trim_words' `words` with no params."""
    parameters = _parameters(func)
    if parameters is None:
        return []
    beyond_the_value = list(parameters.values())[1:]
    return [
        p.name
        for p in beyond_the_value
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.name not in params
    ]


def _pick(rows: List[PlaybookBlock], lookup: Any) -> PlaybookBlock:
    """PURE: the first row whose `when` ALL holds. The last row has none, so
    this is total — publish refuses a block that could fall off the end."""
    for row in rows:
        if matches(row.when, lookup):
            return row
    return rows[-1]


def _fill(
    text: str,
    facts: Dict[str, Any],
    line: str,
    transform: Optional[Dict[str, Transform]] = None,
) -> str:
    """PURE: every hole answered, or the run parks NAMING the hole.

    The agent's substitution is one pass over a flat dict, so a hole left in
    here is a hole the agent reads aloud — "आपने {product_name} के लिए" on a
    live call. It cannot catch it, so we must.
    """

    def one(match: "re.Match[str]") -> str:
        value = facts.get(match.group(1))
        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, (str, int, float))
        ):
            raise NodeParked(
                f"playbook line {line!r}: {{{match.group(1)}}} has no value on "
                "this run"
            )
        rendered = value
        says = (transform or {}).get(match.group(1))
        params = says.params if says else {}
        for name in says.function if says else []:
            try:
                func = TEMPLATE_FUNCTION_REGISTRY[name]
                rendered = func(rendered, **_accepted(func, params))
            except Exception as e:
                # Fail OPEN: a missing hole leaves the sentence broken, but
                # a function that could not read one value leaves it
                # complete and merely unrendered — and NodeParked is a
                # PERMANENT park, so it would end that customer's journey
                # over how a number looks.
                #
                # But SAY so. Publish cannot reach a stored version row, so
                # a built-in renamed or given a new argument would leave
                # every published plan naming it silently raw, for every
                # merchant, for ever. Names only, never the value — the rule
                # the three buddy call sites of this registry already keep.
                logger.warning(
                    f"playbook line {line!r}: {name} could not render "
                    f"{{{match.group(1)}}} ({type(e).__name__}); "
                    "left as it arrived"
                )
                return str(value)
        # Nor may a transform EMPTY a hole that had a value:
        # extract_10_digit_mobile("NA") is "", and "OTP  par aayega" is the
        # gap this function exists to prevent.
        return str(rendered) if str(rendered).strip() else str(value)

    return HOLE.sub(one, text)


def resolve(
    definition: WorkflowDefinition,
    wanted: Iterable[str],
    facts: Dict[str, Any],
    stage_facts: Dict[str, Any],
    customer: Optional[CustomerFacts],
    run: Optional[predicates.RunLens] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """PURE: ONLY the blocks a node asked for — (rendered, chosen).

    `rendered` is {block: text} for the payload. `chosen` is {block: line},
    the NAME of the row that won: the caller records it as playbook_<node>
    so the funnel can group runs by which words they heard, without the text
    ever reaching the run row (canon T20 col 12).

    A block nobody asks for is never evaluated, never in a payload, never in
    a log. A one-name `say` renders as that line's text; a list renders one
    step per line, in order, which is what a call agent walks.
    """
    pb = definition.playbook
    if pb is None:
        return {}, {}

    def lookup(path: str) -> Any:
        # Every source the FIELD grammar has, run facts included: a `when`
        # may name any field a condition may, or an author has to learn which
        # words work where.
        return predicates.lookup(path, facts, stage_facts, customer, run)

    out: Dict[str, str] = {}
    chosen: Dict[str, str] = {}
    for name in wanted:
        rows = pb.blocks.get(name)
        if not rows:
            continue
        row = _pick(rows, lookup)
        said = names_said(row.say)
        chosen[name] = said[0] if isinstance(row.say, str) else ",".join(said)
        rendered = [_fill(pb.lines[line], facts, line, pb.transform) for line in said]
        out[name] = (
            rendered[0]
            if isinstance(row.say, str)
            else "\n".join(
                _STEP.format(name=line, text=text) for line, text in zip(said, rendered)
            )
        )
    return out, chosen
