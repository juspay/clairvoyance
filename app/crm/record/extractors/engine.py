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

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.crm.record.extractors import flat
from app.crm.record.schemas import CatalogEntry, Extracted
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


@dataclass(frozen=True)
class DecodeSpec:
    """What one (source, topic) means to the engine. Built from a code
    CatalogEntry or a registered row — the engine never knows which."""

    # role -> paths in precedence order (first non-empty wins)
    identity: Dict[str, List[str]] = field(default_factory=dict)
    # template placeholder -> path (a bare name is a derived field)
    variables: Dict[str, str] = field(default_factory=dict)
    derive: Dict[str, Deriver] = field(default_factory=dict)


EMPTY_SPEC = DecodeSpec()


def field_value(
    payload: Dict[str, Any],
    path: str,
    derive: Optional[Dict[str, Deriver]] = None,
) -> Any:
    """Resolve one catalog path against one payload: payload.a.b walks the
    dots (a missing step -> None, never a raise); a bare name is a derived
    field when the caller's derive table knows it, else a top-level key."""
    if path.startswith(PAYLOAD_PREFIX):
        node: Any = payload
        for step in path[len(PAYLOAD_PREFIX) :].split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(step)
        return node
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
    for f in entry.fields:
        if f.deprecated:
            continue
        if f.identity:
            identity.setdefault(f.identity, []).extend([f.path, *f.fallbacks])
        if f.variable:
            variables[variable_name(f.path)] = f.path
    return DecodeSpec(identity=identity, variables=variables, derive=dict(derive))


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
        value = field_value(payload, path, spec.derive)
        if isinstance(value, _SCALARS) and len(str(value)) <= VARIABLE_MAX_CHARS:
            variables[name] = value

    return Extracted(handles=handles, facts=facts, variables=variables)


def _first_present(
    payload: Dict[str, Any], paths: List[str], derive: Dict[str, Deriver]
) -> Any:
    for path in paths:
        value = field_value(payload, path, derive)
        if value not in (None, ""):
            return value
    return None
