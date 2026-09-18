"""The rule FIELD grammar and its judgement (enh A/01) — PURE.

A `condition` square reads facts already in hand and picks a labelled
edge without waiting. Its rules are judged here, in document order; the
first rule whose conditions all hold names the edge, none names `else`.
A `call` square asks the same question of the same fields to pick which
TEMPLATE it fires, its own template_id the `else` — one judgement
(`first_matching`), two readings of what the winning arm names.

The OP grammar is not this file's: it is the one where-grammar the corpus
sealed (design/event-catalog.md §The where-grammar; shared/predicate.py),
the same `Condition{field, op, value}` the door's `where` speaks and the
same evaluator — so a plan author learns one vocabulary, and the two
grammars are one function (`matches`). This file owns only WHERE a field's
value comes from:

    context.<key>              the run's facts, as run_facts() exposes them
                               (top-level facts, current_node, current_stage)
    facts.<node>.<key>         one stage's letter (rollout 16: context.facts)
    customer.<column>          display_name · primary_locale · timezone ·
                               has_phone · has_email  (the last two derived:
                               a predicate never reads a handle VALUE)
    customer.attributes.<name> the WINNING claim of an asserted attribute
                               (identity's ladder; inferred-only is absent)

A predicate never raises: a missing field, a non-numeric side of an
ordering op, a customer with no row — each is simply "this rule does not
hold". The honest fallback is always `else`; a parked run for a typo
would stop covering the customer, so the validator catches shape and the
walker forgives data. One thing is NOT forgiven into `else`: a customer
row we could not READ (a database error) propagates and the walker parks
the run to retry — a blip must never branch a customer down the wrong arm.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set, TypeVar

from app.crm.identity.contracts import CustomerFacts
from app.crm.outreach.schemas import ConditionRule
from app.crm.shared.predicate import Condition, matches


class Rule(Protocol):
    """What the judging needs of a rule, and all it needs: the conditions
    that must ALL hold. What an arm NAMES when it holds — an edge label
    (schemas.ConditionRule) or a template (schemas.TemplateRule) — is the
    caller's business, so ONE matcher serves both and the two can never
    drift on what "this rule holds" means."""

    if_: List[Condition]


R = TypeVar("R", bound=Rule)

CUSTOMER_COLUMNS = (
    "display_name",
    "primary_locale",
    "timezone",
    "has_phone",
    "has_email",
)

# Attribute names a predicate may never name: a handle value read into a
# branch is a handle value read into a log (module rules: logs never carry
# a phone/email), and the history bucket is not an attribute at all.
HANDLE_LIKE = frozenset(
    {
        "phone",
        "email",
        "igsid",
        "shopify_customer_id",
        "external_ref",
        "_handle_history",
    }
)

_FIELD = re.compile(
    r"^(?:"
    r"context\.(?P<ctx>[A-Za-z_][A-Za-z0-9_]*)"
    r"|facts\.(?P<node>[A-Za-z0-9_][A-Za-z0-9_-]*)\.(?P<stage_key>[A-Za-z_][A-Za-z0-9_]*)"
    r"|customer\.(?P<column>[A-Za-z_][A-Za-z0-9_]*)"
    r"|customer\.attributes\.(?P<attribute>[A-Za-z_][A-Za-z0-9_]*)"
    r")$"
)


def field_problems(field: str, node_ids: Iterable[str]) -> List[str]:
    """PURE decide: why this field may not be named, as problems (empty =
    fine). Judged at publish so a typo is a sentence the author reads,
    never a run that silently takes `else` forever."""
    m = _FIELD.match(field)
    if m is None:
        return [
            f"{field!r} is not a condition field (context.<key> · "
            "facts.<node>.<key> · customer.<column> · customer.attributes.<name>)"
        ]
    if m.group("node") is not None and m.group("node") not in set(node_ids):
        return [f"{field!r} names a square this plan does not have"]
    if m.group("attribute") is not None:
        if m.group("attribute") in HANDLE_LIKE:
            return [f"{field!r}: a handle is never readable by a predicate"]
        return []
    if m.group("column") is not None and m.group("column") not in CUSTOMER_COLUMNS:
        return [
            f"{field!r}: customer fields are {', '.join(CUSTOMER_COLUMNS)} — "
            "handles are never readable by a predicate"
        ]
    return []


def lookup(
    field: str,
    facts: Dict[str, Any],
    stage_facts: Dict[str, Any],
    customer: Optional[CustomerFacts],
) -> Any:
    """PURE: the value a field names right now, or None when absent (which
    satisfies no op — shared/predicate's conservative rule)."""
    m = _FIELD.match(field)
    if m is None:
        return None
    if m.group("ctx") is not None:
        return facts.get(m.group("ctx"))
    if m.group("node") is not None:
        letter = stage_facts.get(m.group("node"))
        return letter.get(m.group("stage_key")) if isinstance(letter, dict) else None
    if customer is None:
        return None
    if m.group("attribute") is not None:
        return customer.attributes.get(m.group("attribute"))
    # The executor shares the validator's whitelist: a name the grammar
    # would refuse (customer.attributes whole, a model method) reads as
    # absent here too, so no document — published or not — reaches past
    # the five columns.
    column = m.group("column")
    return getattr(customer, column, None) if column in CUSTOMER_COLUMNS else None


def first_matching(
    rules: Iterable[R],
    facts: Dict[str, Any],
    stage_facts: Dict[str, Any],
    customer: Optional[CustomerFacts],
) -> Optional[R]:
    """PURE decide: the first rule whose conditions ALL hold, or None — the
    caller's `else`. Document order is the author's if/else-if ladder, so a
    narrower arm placed below a broader one is simply never reached.

    Never raises: every lookup is total and the evaluator treats an
    unreadable value as "does not hold"."""
    for rule in rules:
        if matches(rule.if_, lambda path: lookup(path, facts, stage_facts, customer)):
            return rule
    return None


def choose(
    rules: List[ConditionRule],
    facts: Dict[str, Any],
    stage_facts: Dict[str, Any],
    customer: Optional[CustomerFacts],
) -> Optional[str]:
    """PURE decide: the LABEL the first holding rule names, or None — the
    condition square's reading of first_matching."""
    rule = first_matching(rules, facts, stage_facts, customer)
    return rule.on if rule is not None else None


def needs_customer(rules: Iterable[Rule]) -> bool:
    """PURE: does any rule read the customer? The one DB read judging may
    cost is paid only when a rule asks for it."""
    return any(c.field.startswith("customer.") for rule in rules for c in rule.if_)


def fields_named(rules: Iterable[Rule]) -> Set[str]:
    return {c.field for rule in rules for c in rule.if_}
