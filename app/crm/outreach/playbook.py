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

Two rules hold the shape, and both are one sentence:

  1. Every piece of text lives in `lines`, once. A block names lines and
     never holds a sentence.
  2. `say` is a name, or an ordered list of names.

Nothing here is new vocabulary. `when` is the where-grammar over the
condition square's field grammar; first-match-wins with a mandatory default
is that square's `else`; a line with {holes} is the send node's blank law.

A line fills from FACTS only, never from another line — no chains, no
cycles, always total.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.crm.identity.contracts import CustomerFacts
from app.crm.outreach import predicates
from app.crm.outreach.nodes.context import is_bookkeeping
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import PlaybookBlock, WorkflowDefinition
from app.crm.shared.predicate import matches

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


def _pick(rows: List[PlaybookBlock], lookup: Any) -> PlaybookBlock:
    """PURE: the first row whose `when` ALL holds. The last row has none, so
    this is total — publish refuses a block that could fall off the end."""
    for row in rows:
        if matches(row.when, lookup):
            return row
    return rows[-1]


def _fill(text: str, facts: Dict[str, Any], line: str) -> str:
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
        return str(value)

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
        # Every source the FIELD grammar has, including the engine's derived
        # run facts: a `when` may name any field a condition may name, or the
        # two vocabularies would diverge and an author would have to learn which
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
        rendered = [_fill(pb.lines[line], facts, line) for line in said]
        out[name] = (
            rendered[0]
            if isinstance(row.say, str)
            else "\n".join(
                _STEP.format(name=line, text=text) for line, text in zip(said, rendered)
            )
        )
    return out, chosen
