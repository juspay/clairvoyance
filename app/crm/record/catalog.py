"""The event catalog (design/event-catalog.md; canon T24) — record's fourth
house registry, beside EXTRACTORS: (source, topic) -> what this event is
called and what's inside it. Two layers, one shape:

  CODE layer      — CATALOG below. Grows in the same PR as the decoder
                    (extractor + fixtures + catalog entry: the four-part
                    ritual; tests/crm/test_catalog.py pins the square).
  REGISTERED layer — crm_event_schema rows a push vendor signs at
                    enrollment (POST /ingest/schemas or the console wizard).

The catalog API merges them; the editor, the publish validator and the
where-grammar are layer-blind. gather (accessor reads) -> decide (pure:
merge, validate a registration, resolve a field) -> apply (register).

Hot-path law (canon T24 wiring): the FLOW runtime never reads the table;
the DECODE step reads only the cached spec (decode_spec — one engine, two
spec sources: a code CatalogEntry or a registered row, same shape),
and discovery costs one INSERT per new topic EVER (is_known/mark_known).
"""

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config.static import CRM_SCHEMA_CACHE_SECONDS
from app.crm.record.db import accessor
from app.crm.record.extractors import shopify
from app.crm.record.extractors.engine import (
    EMPTY_SPEC,
    PAYLOAD_PREFIX,
    DecodeSpec,
    Deriver,
    spec_for_entry,
)
from app.crm.record.schemas import (
    CatalogEntry,
    CatalogField,
    EventSchema,
    SampledField,
    SchemaRegistration,
)

# Type -> the ops the where-grammar implements for it (shared/predicate.py).
# One-to-one by law: the UI shows exactly these, the engine runs exactly
# these. phone is identity — never filterable.
OPS_BY_TYPE: Dict[str, List[str]] = {
    "text": ["is", "is_not", "in", "exists"],
    "choice": ["is", "is_not", "in", "exists"],
    "number": [">", ">=", "<", "<=", "=", "exists"],
    "boolean": ["is", "is_not", "exists"],
    "datetime": [">", ">=", "<", "<=", "exists"],
    "phone": [],
}
KEYABLE_TYPES = ("text", "number")
MAX_REGISTERED_FIELDS = 200
SAMPLE_WINDOW_EVENTS = 200
SEEN_WINDOW_DAYS = 7

_PAYLOAD_PATH = re.compile(
    r"^payload\.[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_DERIVED_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VENDOR_ROLES = ("phone", "name")  # canon T24: identity is phone | name
_PHONE_LIKE = re.compile(r"^\+?\d[\d\s-]{8,}$")

SchemaKey = Tuple[str, str, str]  # (merchant_id, source, topic)


# --- The CODE layer ---------------------------------------------------------
#
# One entry per (source, topic), DECLARED beside its derivers in the source's
# spec module (extractors/shopify.py — one source, one file). Adding an event
# = one entry + a recorded fixture, same PR, pinned by tests/crm/test_catalog.py.
# The engine (extractors/engine.py) executes these exactly as it executes a
# registered vendor row: one decode engine, two spec sources.

CATALOG: Dict[Tuple[str, str], CatalogEntry] = {
    (entry.source, entry.topic): entry for entry in shopify.ENTRIES
}

# (source, topic) -> derived field -> derive(payload). A spec module exports
# its derivers once; every entry of that source that declares a derived field
# gets it from here (pinned: declared == provided, per entry).
DERIVE: Dict[Tuple[str, str], Dict[str, Deriver]] = {
    key: {
        f.path: shopify.DERIVERS[f.path]
        for f in entry.fields
        if f.derived and f.path in shopify.DERIVERS
    }
    for key, entry in CATALOG.items()
}


# --- Pure helpers -----------------------------------------------------------


def with_ops(field: CatalogField) -> CatalogField:
    """The ops a field admits are a function of its type, never authored."""
    return field.model_copy(update={"ops": list(OPS_BY_TYPE[field.type])})


def code_entries() -> List[CatalogEntry]:
    return [
        entry.model_copy(update={"fields": [with_ops(f) for f in entry.fields]})
        for entry in CATALOG.values()
    ]


def canonical_path(path: str) -> str:
    """entry.key was authored bare ("order_id") before the catalog; the
    catalog's identity for the same thing is "payload.order_id"."""
    if path.startswith(PAYLOAD_PREFIX) or path in _all_derived_names():
        return path
    return PAYLOAD_PREFIX + path


def _all_derived_names() -> Set[str]:
    return {name for table in DERIVE.values() for name in table}


def derive_for(source: str, topic: str) -> Dict[str, Deriver]:
    return DERIVE.get((source, topic), {})


def validate_registration(registration: SchemaRegistration) -> List[str]:
    """PURE decide: every law a signed schema must satisfy, as problems.
    Unknown types are rejected HERE, never discovered at flow-publish."""
    problems: List[str] = []
    fields = registration.fields
    if len(fields) > MAX_REGISTERED_FIELDS:
        problems.append(f"too many fields ({len(fields)} > {MAX_REGISTERED_FIELDS})")
    seen: Set[str] = set()
    roles: Dict[str, str] = {}
    for field in fields:
        if field.derived:
            problems.append(f"{field.path}: derived fields are code-layer only")
        if field.fallbacks:
            problems.append(f"{field.path}: fallbacks are code-layer only")
        if field.identity is not None and field.identity not in VENDOR_ROLES:
            problems.append(
                f"{field.path}: identity role must be one of {' | '.join(VENDOR_ROLES)}"
            )
        if not _PAYLOAD_PATH.match(field.path):
            problems.append(
                f"{field.path}: path must be payload.<key>[.<key>...] "
                "(letters, digits, underscore)"
            )
        if field.path in seen:
            problems.append(f"{field.path}: declared twice")
        seen.add(field.path)
        if field.type == "choice" and not field.values:
            problems.append(f"{field.path}: choice needs its values")
        if field.type != "choice" and field.values:
            problems.append(f"{field.path}: only choice carries values")
        if field.keyable and field.type not in KEYABLE_TYPES:
            problems.append(f"{field.path}: keyable needs text or number")
        if field.identity is not None:
            if field.identity in roles:
                problems.append(
                    f"{field.path}: identity role {field.identity!r} already "
                    f"taken by {roles[field.identity]}"
                )
            roles[field.identity] = field.path
            if field.identity == "phone" and field.type != "phone":
                problems.append(f"{field.path}: identity:phone must be type phone")
            if field.keyable:
                problems.append(f"{field.path}: an identity field cannot be keyable")
        if field.type == "phone" and field.identity != "phone":
            problems.append(f"{field.path}: type phone is identity:phone or nothing")
    return problems


def etag_for(entries: List[CatalogEntry]) -> str:
    """Version stamp of one merged catalog: content-addressed, so a
    re-registration or a code deploy changes it and nothing else does."""
    body = json.dumps(
        [e.model_dump(mode="json", exclude={"seen_7d"}) for e in entries],
        sort_keys=True,
    )
    return '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'


def _guess_type(jtype: str, samples: List[Any]) -> str:
    if jtype == "number":
        return "number"
    if jtype == "boolean":
        return "boolean"
    texts = [s for s in samples if isinstance(s, str)]
    if texts and all(_PHONE_LIKE.match(t) for t in texts):
        return "phone"
    if texts and all(_parses_datetime(t) for t in texts):
        return "datetime"
    return "text"


def _parses_datetime(text: str) -> bool:
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _entry_from_row(row: EventSchema, seen: int) -> CatalogEntry:
    return CatalogEntry(
        source=row.source,
        topic=row.topic,
        label=row.label or row.topic,
        group=row.source,
        layer="registered",
        status="registered" if row.status == "registered" else "detected",
        version=row.version,
        fields=[with_ops(f) for f in row.fields],
        seen_7d=seen,
    )


# --- Reads the API and the validator gather -------------------------------


async def merged_catalog(merchant_id: str) -> List[CatalogEntry]:
    """Code layer + this merchant's registered layer, one shape, with the
    seen-this-week count computed on read (T24 stays cold)."""
    rows = await accessor.list_schemas(merchant_id)
    counts = {
        (c.source, c.topic): c.seen
        for c in await accessor.topic_counts(merchant_id, SEEN_WINDOW_DAYS)
    }
    entries = [
        e.model_copy(update={"seen_7d": counts.get((e.source, e.topic), 0)})
        for e in code_entries()
    ]
    code_keys = set(CATALOG)
    for row in rows:
        if (row.source, row.topic) in code_keys:
            continue  # the code layer is authoritative for its own events
        entries.append(_entry_from_row(row, counts.get((row.source, row.topic), 0)))
    return entries


async def topic_counts(merchant_id: str, days: int = SEEN_WINDOW_DAYS):
    """Events seen per (source, topic) in the window — the flow list's
    "saw 240" side, computed on read (the timeline.py seam: cross-module
    callers never depend on an accessor signature)."""
    return await accessor.topic_counts(merchant_id, days)


async def catalog_fields(
    merchant_id: str, topic: str
) -> Optional[Dict[str, CatalogField]]:
    """The validator's gather: every declared field for a topic (any
    source, both layers), by path. None = no layer knows this topic."""
    found: Dict[str, CatalogField] = {}
    known = False
    for entry in code_entries():
        if entry.topic == topic:
            known = True
            found.update({f.path: f for f in entry.fields})
    for row in await accessor.list_schemas(merchant_id):
        if row.topic == topic and row.status == "registered":
            known = True
            found.update({f.path: with_ops(f) for f in row.fields})
    return found if known else None


async def register_schema(
    merchant_id: str, registration: SchemaRegistration, registered_by: str
) -> EventSchema:
    problems = validate_registration(registration)
    if problems:
        raise SchemaValidationError(problems)
    row = await accessor.register_schema(
        merchant_id,
        registration.source,
        registration.topic,
        registration.label,
        json.dumps([f.model_dump(exclude={"ops"}) for f in registration.fields]),
        registered_by,
    )
    _SPEC_CACHE.pop((merchant_id, registration.source, registration.topic), None)
    return row


async def list_schemas(merchant_id: str) -> List[EventSchema]:
    return await accessor.list_schemas(merchant_id)


async def sample_fields(
    merchant_id: str, source: str, topic: str
) -> List[SampledField]:
    """The wizard's pre-fill: keys seen in the vendor's recent traffic with a
    guessed type — compute-on-read over crm_event_raw, nothing stored."""
    rows = await accessor.sample_fields(
        merchant_id, source, topic, SAMPLE_WINDOW_EVENTS
    )
    return [
        SampledField(
            path=PAYLOAD_PREFIX + r["path"],
            type_guess=_guess_type(r["jtype"], r["samples"]),  # type: ignore[arg-type]
            seen=r["seen"],
            samples=r["samples"],
        )
        for r in rows
    ]


# --- Hot-path helpers for the event worker ----------------------------------

_KNOWN: Set[SchemaKey] = set()
_SPEC_CACHE: Dict[SchemaKey, Tuple[float, DecodeSpec]] = {}


def is_known(key: SchemaKey) -> bool:
    return key in _KNOWN


def mark_known(key: SchemaKey) -> None:
    _KNOWN.add(key)


def code_spec(source: str, topic: str) -> Optional[DecodeSpec]:
    """The code layer's spec for a topic, or None when no entry declares
    it. Pure and free: no table, no cache."""
    entry = CATALOG.get((source, topic))
    if entry is None:
        return None
    return spec_for_entry(entry, derive_for(source, topic))


async def decode_spec(merchant_id: str, source: str, topic: str) -> DecodeSpec:
    """What the engine reads for one letter — the code layer when it
    declares the topic (authoritative, no I/O), else the vendor's
    registration, cached in process for CRM_SCHEMA_CACHE_SECONDS (T24 stays
    cold; the hot path reads the cache). EMPTY_SPEC when nothing declares
    the topic: the flat shape alone applies."""
    spec = code_spec(source, topic)
    if spec is not None:
        return spec
    key = (merchant_id, source, topic)
    cached = _SPEC_CACHE.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    row = await accessor.get_schema(merchant_id, source, topic)
    spec = EMPTY_SPEC
    if row is not None and row.status == "registered":
        entry = _entry_from_row(row, 0)
        spec = spec_for_entry(entry, {})
    _SPEC_CACHE[key] = (now + CRM_SCHEMA_CACHE_SECONDS, spec)
    return spec


class SchemaValidationError(Exception):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))
