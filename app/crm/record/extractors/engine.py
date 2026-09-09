"""The ONE decode engine (design/event-catalog.md §One decode engine, two
spec sources — ruled 1 Sep 2026).

A payload is decoded by a SPEC, never by hand-written code: which paths
hold the person (identity roles, in precedence order), which paths ride
into templates (variables), and which names are computed (derive — the
code escape hatch for the ~10% that is genuinely logic). Two spec sources
feed this one function:

  registered  — a push vendor's crm_event_schema row (T24), read through
                the cached mapping in record/catalog.py;
  code        — a connector's CatalogEntry (extractors/shopify.py), the
                SAME vocabulary written in code, with fallbacks and derive().

One engine cannot drift from itself: the path the editor shows for "the
phone" IS the path this function reads. (Two hand-written readers did
drift — #1025's extractor found the phone in four places while outreach's
entry context searched three, and the flagship run parked at its first
call node.)

The flat shape (customer_mobile_number / customer_name at the top level)
is the standing fallback beneath every spec: a conventional producer never
thinks about registration. Path resolution lives here too — catalog.py and
outreach's entry evaluator resolve fields through these same helpers.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from app.crm.record.extractors import flat
from app.crm.record.schemas import ABOUT_CUSTOMER, CatalogEntry, Extracted
from app.crm.shared.normalize import normalize_email, normalize_phone

Deriver = Callable[[Dict[str, Any]], Any]

PAYLOAD_PREFIX = "payload."
# Small-facts cap, the same ceiling outreach applies to run context: a
# variable is a template fill-in, never a payload photocopy.
VARIABLE_MAX_CHARS = 256
# Roles that are handles resolve() probes on — everything but the name.
_NORMALIZE: Dict[str, Callable[[str], Optional[str]]] = {
    "phone": normalize_phone,
    "email": normalize_email,
}
_SCALARS = (str, int, float, bool)
# The declared type whose value is an ARRAY. It is a template variable and
# nothing else: OPS_BY_TYPE gives it no ops, so the where-grammar never
# receives a list and the matcher never learns array semantics
# (design/event-catalog.md, sealed). The engine turns it into ONE scalar at
# decode, because that is what a template blank and a lead payload can carry.
LIST_TYPE = "list"
LIST_JOIN = ", "
# One blank inside an item_format — an element's own key, dot-walked.
ITEM_BLANK = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")
# Any brace group at all: what the validator measures a format against, so a
# misspelled blank is refused at registration instead of reaching a customer
# as the literal "{na-me}".
ANY_BLANK = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class DecodeSpec:
    """What one (source, topic) means to the engine. Built from a code
    CatalogEntry or a registered row — the engine never knows which."""

    # role -> paths in precedence order (first non-empty wins)
    identity: Dict[str, List[str]] = field(default_factory=dict)
    # template placeholder -> path (a bare name is a derived field)
    variables: Dict[str, str] = field(default_factory=dict)
    # placeholder -> how one element reads, for the variables whose declared
    # type is `list` (None = the path already named one field). A name here
    # is ALSO in `variables`: one loop reads both.
    lists: Dict[str, Optional[str]] = field(default_factory=dict)
    derive: Dict[str, Deriver] = field(default_factory=dict)
    # who the letter is about — the entry's word, passed through to
    # Extracted.about so the pass knows a NULL customer is by design
    about: Literal["customer", "merchant"] = ABOUT_CUSTOMER


EMPTY_SPEC = DecodeSpec()


def dig(node: Any, path: str) -> Any:
    """PURE: walk dots from here (a missing step -> None, never a raise).
    The ONE dot-walker: field_value walks a payload with it and the list
    reader walks each element with it, so `customer.phone` and an item's
    `variant.title` can never mean two different things."""
    for step in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def list_values(payload: Dict[str, Any], path: str) -> Optional[List[Any]]:
    """PURE: what a declared `list` field addresses. Its path walks THROUGH
    every array it crosses, and what it finds is flattened:

        payload.tags                            the tags themselves
        payload.line_items                      every line, whole
        payload.line_items.title                the title of every line
        payload.loanApplications.offers         every offer of every
                                                application
        payload.loanApplications.offers.rate    …and one field of each

    Nesting is not a special case, it is the same step again: real letters
    put a list inside a list (an order's applications, their offers), and a
    path that stopped at the first array could name the application but
    never the offer.

    Flattening is the honest answer for a template blank, which is ONE
    string: "every offer" reads as a sentence, and which application each
    came from is a distinction a blank cannot carry anyway. A plan that
    needs that distinction wants the application's own field beside it.

    None when nothing on the path is a list — the caller then writes no
    variable at all, rather than a half-answer."""
    if not path.startswith(PAYLOAD_PREFIX):
        return None
    found = _walk(payload, path[len(PAYLOAD_PREFIX) :].split("."))
    return found if isinstance(found, list) else None


def _walk(node: Any, steps: List[str]) -> Any:
    """PURE: the value at these steps, with every array crossed on the way
    mapped over and flattened into one list. A missing step is None, never
    a raise — a letter is not obliged to carry what a plan hopes for."""
    for index, step in enumerate(steps):
        if isinstance(node, list):
            found: List[Any] = []
            for element in node:
                below = _walk(element, steps[index:])
                if isinstance(below, list):
                    found.extend(below)
                else:
                    found.append(below)
            return found
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def render_item(element: Any, item_format: str) -> Optional[str]:
    """PURE: one element through its declared format — "{title} x{quantity}"
    over {"title": "Socks", "quantity": 2} is "Socks x2".

    Every blank must answer, or the element is SKIPPED WHOLE. A line reading
    " x2" because the title was missing is the half-formed value a provider
    renders as corruption; better to name three items than four badly."""
    missing = False

    def one(match: "re.Match[str]") -> str:
        nonlocal missing
        found = dig(element, match.group(1))
        if isinstance(found, bool) or not isinstance(found, (str, int, float)):
            missing = True
            return ""
        text = str(found).strip()
        if not text:
            missing = True
        return text

    rendered = ITEM_BLANK.sub(one, item_format).strip()
    return None if missing or not rendered else rendered


def join_list(
    values: Optional[List[Any]],
    item_format: Optional[str] = None,
    budget: int = VARIABLE_MAX_CHARS,
) -> Optional[str]:
    """PURE: the array as ONE scalar a template blank can carry.

    With a format each element is rendered through it; without one the
    values are already what the path named. Anything unrenderable is
    skipped, never printed as an object.

    Truncated HERE, with the overflow COUNTED — a list that came out over
    the ceiling would be dropped by the scalar gate below, and the send
    would then park on a blank whose cause is two modules away."""
    if not values:
        return None
    parts: List[str] = []
    for value in values:
        text = (
            render_item(value, item_format)
            if item_format
            else (
                None
                if isinstance(value, bool) or not isinstance(value, (str, int, float))
                else str(value).strip() or None
            )
        )
        if text:
            parts.append(text)
    return _joined_within(parts, budget) if parts else None


def _joined_within(parts: List[str], budget: int) -> str:
    """PURE: as many parts as fit, then "+N more". Every part is measured,
    the first one included — guarding that on "we kept something" let one
    long title through to a slice that cut it mid-word and took the count
    with it, which is the one thing this promises never to do."""
    kept: List[str] = []
    for part in parts:
        left = len(parts) - len(kept) - 1
        tail = f" +{left} more" if left else ""
        if len(LIST_JOIN.join([*kept, part])) + len(tail) > budget:
            break
        kept.append(part)
    text = LIST_JOIN.join(kept)
    remaining = len(parts) - len(kept)
    if not remaining:
        return text[:budget]
    counted = f"{text} +{remaining} more" if text else f"+{remaining} more"
    return counted if len(counted) <= budget else text[:budget]


def format_faults(item_format: str) -> List[str]:
    """PURE: where one item_format breaks its laws. Balance FIRST —
    ANY_BLANK sees only CLOSED groups, so "{title} x{quantity" would look
    like one well-formed blank and pass, and the engine would leave the
    literal "{quantity" in a customer's message."""
    leftover = ANY_BLANK.sub("", item_format)
    if "{" in leftover or "}" in leftover:
        return [f"item_format {item_format!r} has an unmatched brace"]
    blanks = ANY_BLANK.findall(item_format)
    if not blanks:
        return [f"item_format {item_format!r} names no key of an element"]
    return [
        f"item_format blank {bad!r} is not a key (letters, digits, underscore, dots)"
        for bad in blanks
        if not ITEM_BLANK.fullmatch(bad)
    ]


def field_value(
    payload: Dict[str, Any],
    path: str,
    derive: Optional[Dict[str, Deriver]] = None,
) -> Any:
    """Resolve one catalog path against one payload: payload.a.b walks the
    dots (a missing step -> None, never a raise); a bare name is a derived
    field when the caller's derive table knows it, else a top-level key."""
    if path.startswith(PAYLOAD_PREFIX):
        return dig(payload, path[len(PAYLOAD_PREFIX) :])
    if derive and path in derive:
        try:
            return derive[path](payload)
        except Exception:
            return None
    return payload.get(path)


def variable_name(path: str) -> str:
    """The {placeholder} a variable field fills: a derived field's own
    name, else the path's last segment (payload.customer.first_name ->
    first_name). Pinned unique per entry by tests/crm/test_catalog.py."""
    if path.startswith(PAYLOAD_PREFIX):
        return path.rsplit(".", 1)[-1]
    return path


def spec_for_entry(entry: CatalogEntry, derive: Dict[str, Deriver]) -> DecodeSpec:
    """PURE: a catalog entry (either layer) -> what the engine reads.
    Deprecated fields keep their place in the catalog but stop feeding
    decode; a field's fallbacks follow its own path in order."""
    identity: Dict[str, List[str]] = {}
    variables: Dict[str, str] = {}
    lists: Dict[str, Optional[str]] = {}
    for f in entry.fields:
        if f.deprecated:
            continue
        if f.identity:
            identity.setdefault(f.identity, []).extend([f.path, *f.fallbacks])
        if f.variable:
            name = variable_name(f.path)
            variables[name] = f.path
            if f.type == LIST_TYPE:
                lists[name] = f.item_format
            else:
                lists.pop(name, None)
    return DecodeSpec(
        identity=identity,
        variables=variables,
        lists=lists,
        derive=dict(derive),
        about=entry.about,
    )


def extract(payload: Dict[str, Any], spec: DecodeSpec) -> Extracted:
    """One letter in; handles, facts and template variables out. The flat
    shape first (standard keys), then the spec — a declared path wins over
    a standard key, so a vendor's rider_phone beats an absent
    customer_mobile_number and a present one is still honoured."""
    base = flat.extract(payload)
    handles: Dict[str, str] = dict(base.handles)
    facts: Dict[str, Any] = dict(base.facts)

    for role, paths in spec.identity.items():
        raw = _first_present(payload, paths, spec.derive)
        if raw is None:
            continue
        if role == "name":
            name = str(raw).strip()
            if name:
                facts["name"] = name
            continue
        normalize = _NORMALIZE.get(role)
        value = normalize(str(raw)) if normalize else str(raw).strip()
        if value:
            handles[role] = value

    variables: Dict[str, Any] = {}
    for name, path in spec.variables.items():
        if name in spec.lists:
            joined = join_list(list_values(payload, path), spec.lists[name])
            if joined:
                variables[name] = joined
            continue
        value = field_value(payload, path, spec.derive)
        if isinstance(value, _SCALARS) and len(str(value)) <= VARIABLE_MAX_CHARS:
            variables[name] = value

    return Extracted(
        handles=handles, facts=facts, variables=variables, about=spec.about
    )


def _first_present(
    payload: Dict[str, Any], paths: List[str], derive: Dict[str, Deriver]
) -> Any:
    for path in paths:
        value = field_value(payload, path, derive)
        if value not in (None, ""):
            return value
    return None
