"""A dynamic segment's rule (canon T15 col 6, ``definition``): its FORMAT and
the shape laws — what a rule is, before anyone asks what it means.

Shared because two modules read one rule: outreach judges it against the
merchant's catalog before it is saved (outreach/segment_rules.py), and record
answers it in SQL over its own events (segments phase 4). Record may not
import outreach (boundary rule 12), so the grammar both of them read lives
here — the shared/predicate.py precedent: one where-grammar, read by the
publish validator and the entry processor alike.

The format (v1 — event fields only; customer attributes come later, and an
inferred one never, canon/05-audiences.md:15):

    rule      := {"all": [rule, ...]}          every child holds
               | {"any": [rule, ...]}          at least one child holds
               | {"not": rule}                 the child does not hold
               | event condition
    event     := {"event": "<topic>",          the customer had this event —
                  "within_days": N,              in the last N days (optional),
                  "at_least": N,                 at least N times (default 1),
                  "where": [{field, op, value}]} each one matching these
                                                 (optional; the workflows'
                                                 where-grammar, unchanged)

"Never had X" is {"not": {"event": "X"}} — one way to say it, not two.

The shape is closed: unknown keys, empty groups, a group with two words,
nesting deeper than MAX_RULE_DEPTH or more than MAX_RULE_EVENTS event
conditions are refused — the compiler gets a bounded tree.

Pure, no I/O; imports only shared/predicate.py (the ``where`` it reuses).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError

from app.crm.shared.predicate import Condition

#: The three group words. A group object carries exactly one of them.
GROUP_WORDS = ("all", "any", "not")

#: Every key an event condition may carry — anything else is a typo, refused.
EVENT_KEYS = frozenset({"event", "within_days", "at_least", "where"})

#: How deep groups may nest. Deep enough for any rule a person writes by
#: hand or in a builder; shallow enough that the compiled query stays sane.
MAX_RULE_DEPTH = 8

#: How many event conditions one rule may hold, for the same reason.
MAX_RULE_EVENTS = 50


@dataclass(frozen=True)
class EventCondition:
    """One event condition the shape walk found, for the next law to judge:
    where it sits (``rule.all[0]``), its topic, and the ``where`` conditions
    that parsed — each with its index in the rule's list, so a later problem
    points at the line the person wrote, not at its place among survivors."""

    path: str
    topic: str
    where: Tuple[Tuple[int, Condition], ...]


def rule_events(raw: Any) -> List[str]:
    """PURE: every event topic a rule names, nested groups included — what
    the catalog gather reads. Tolerates a malformed rule (the shape check
    reports it); collects only what is plainly there."""
    found: List[str] = []
    if isinstance(raw, dict):
        if isinstance(raw.get("event"), str):
            found.append(raw["event"])
        for word in ("all", "any"):
            if isinstance(raw.get(word), list):
                for child in raw[word]:
                    found.extend(rule_events(child))
        if "not" in raw:
            found.extend(rule_events(raw["not"]))
    return found


def check_shape(raw: Any) -> Tuple[List[str], List[EventCondition]]:
    """PURE: every shape law the rule breaks, as readable problems located
    by path, and the event conditions it holds. No problems = well-formed."""
    problems: List[str] = []
    events: List[EventCondition] = []
    _walk(raw, "rule", 1, problems, events)
    if len(events) > MAX_RULE_EVENTS:
        problems.append(
            f"rule: {len(events)} event conditions — at most {MAX_RULE_EVENTS}"
        )
    return problems, events


def _walk(
    node: Any,
    path: str,
    depth: int,
    problems: List[str],
    events: List[EventCondition],
) -> None:
    if depth > MAX_RULE_DEPTH:
        problems.append(f"{path}: nested deeper than {MAX_RULE_DEPTH} levels")
        return
    if not isinstance(node, dict):
        problems.append(
            f"{path}: a rule is an object — a group ({' / '.join(GROUP_WORDS)}) "
            "or an event condition"
        )
        return
    groups = [word for word in GROUP_WORDS if word in node]
    if groups:
        if len(node) != 1:
            problems.append(
                f"{path}: a group carries exactly one word "
                f"({' / '.join(GROUP_WORDS)}) and nothing else"
            )
            return
        word = groups[0]
        if word == "not":
            _walk(node["not"], f"{path}.not", depth + 1, problems, events)
            return
        children = node[word]
        if not isinstance(children, list) or not children:
            problems.append(f"{path}.{word}: needs a non-empty list of rules")
            return
        for index, child in enumerate(children):
            _walk(child, f"{path}.{word}[{index}]", depth + 1, problems, events)
        return
    if "event" not in node:
        problems.append(
            f"{path}: expected a group ({' / '.join(GROUP_WORDS)}) or an "
            "event condition"
        )
        return
    _event(node, path, problems, events)


def _event(
    node: Dict[str, Any],
    path: str,
    problems: List[str],
    events: List[EventCondition],
) -> None:
    unknown = sorted(set(node) - EVENT_KEYS)
    if unknown:
        problems.append(
            f"{path}: unknown key(s) {', '.join(unknown)} — an event condition "
            f"takes {', '.join(sorted(EVENT_KEYS))}"
        )
    topic = node.get("event")
    if not isinstance(topic, str) or not topic.strip():
        problems.append(f"{path}.event: names the event, as text")
        return
    for word in ("within_days", "at_least"):
        if word in node and not _positive_int(node[word]):
            problems.append(f"{path}.{word}: a whole number, 1 or more")
    raw_where = node.get("where", [])
    where: List[Tuple[int, Condition]] = []
    if not isinstance(raw_where, list):
        problems.append(f"{path}.where: a list of conditions [{{field, op, value}}]")
    else:
        for index, raw_condition in enumerate(raw_where):
            try:
                where.append((index, Condition.model_validate(raw_condition)))
            except ValidationError as e:
                problems.append(f"{path}.where[{index}]: {_condition_fault(e)}")
    events.append(EventCondition(path=path, topic=topic, where=tuple(where)))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _condition_fault(error: ValidationError) -> str:
    """The first fault pydantic found, as one line: where, then what."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    message = str(first["msg"]).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message
