"""The sealed schema — canon's 17 tables, declared once, diffed in CI.

This is the reference the canon-conformance check (A3) compares the live
database against. Canon 07-wiring states the rule::

    MISSING is pending, EXTRA or MISMATCHED fails.

Read literally: a canon table that has not been built yet is *not* an error
(the modules land across phase 1), but a table that drifts from its
declaration, or a column nobody declared, is. That asymmetry is what lets
this check run from day one and tighten on its own as A6/A8/A13 land — the
gate is in place before the code it guards.

Scope of "EXTRA": only the crm_/platform_ namespace is governed here. The
~45 pre-CPaaS tables (lead_call_tracker, template, ...) are outside canon
and are never reported. Without that fence every legacy table would read as
EXTRA and the check could never go green.

## Naming: crm.customer vs crm_customer

Canon writes ``crm.customer`` / ``platform.identity`` — Postgres *schemas*,
with the boundary enforced as "a database grant, not a convention". The
foundation shipped flat, prefixed tables (``crm_customer``) in the public
schema instead, with no schemas and no per-module grants.

Rather than silently ratify that, each table records both: ``name`` is what
is actually built and diffed, ``canonical_name`` is what canon specifies.
``divergences()`` reports every pair that disagrees, so the gap stays
countable instead of being lost. When the schemas + grants land, the fix is
to change ``name`` here and let the diff drive the migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Column:
    """One column. ``type`` is the Postgres ``information_schema`` spelling
    (e.g. ``timestamp with time zone``, not ``timestamptz``) so it compares
    to introspection without a translation table."""

    name: str
    type: str
    not_null: bool = False


@dataclass(frozen=True)
class Table:
    """One canon table.

    Attributes:
        canon_id: Canon's table number (T02, T05, ...). Identity across docs.
        name: Table name as actually built — what the diff looks for.
        canonical_name: Name as canon specifies it. See the module docstring.
        tenant_scoped: True for a root table, which canon requires to carry
            ``merchant_id`` and to lead every unique index with it. False for
            platform-layer tables, which are deliberately cross-merchant.
        columns: Full column set. An undeclared column in the DB is EXTRA.
    """

    canon_id: str
    name: str
    canonical_name: str
    tenant_scoped: bool
    columns: tuple[Column, ...] = field(default_factory=tuple)

    @property
    def column_map(self) -> dict[str, Column]:
        return {column.name: column for column in self.columns}


_TIMESTAMPTZ = "timestamp with time zone"


# --- T02 — platform.identity -------------------------------------------
# The handles book: one row per identifier ACROSS merchants. Deliberately
# NOT tenant-scoped — cross-tenant suppression is its entire purpose, so it
# is exempt from the merchant_id law rather than in violation of it.
PLATFORM_IDENTITY = Table(
    canon_id="T02",
    name="platform_identity",
    canonical_name="platform.identity",
    tenant_scoped=False,
    columns=(
        Column("id", "uuid", not_null=True),
        Column("kind", "text", not_null=True),
        Column("value", "text", not_null=True),
        Column("is_suppressed", "boolean", not_null=True),
        Column("suppressions", "jsonb", not_null=True),
        Column("suppression_log", "jsonb", not_null=True),
        Column("first_seen_at", _TIMESTAMPTZ),
        Column("last_seen_at", _TIMESTAMPTZ),
        Column("created_at", _TIMESTAMPTZ, not_null=True),
        Column("updated_at", _TIMESTAMPTZ, not_null=True),
    ),
)


# --- T05 — crm.customer -------------------------------------------------
# The customer as ONE merchant knows them. Root table: every read is
# merchant-scoped, and each partial unique index leads with merchant_id.
CRM_CUSTOMER = Table(
    canon_id="T05",
    name="crm_customer",
    canonical_name="crm.customer",
    tenant_scoped=True,
    columns=(
        Column("id", "uuid", not_null=True),
        Column("merchant_id", "text", not_null=True),
        Column("display_name", "text"),
        Column("primary_locale", "text"),
        Column("timezone", "text"),
        Column("phone", "text"),
        Column("email", "text"),
        Column("igsid", "text"),
        Column("shopify_customer_id", "text"),
        Column("external_ref", "text"),
        Column("status", "text", not_null=True),
        Column("merged_into_id", "uuid"),
        Column("merged_at", _TIMESTAMPTZ),
        Column("first_seen_at", _TIMESTAMPTZ, not_null=True),
        Column("last_seen_at", _TIMESTAMPTZ, not_null=True),
        Column("created_at", _TIMESTAMPTZ, not_null=True),
        Column("updated_at", _TIMESTAMPTZ, not_null=True),
        Column("attributes", "jsonb", not_null=True),
    ),
)


# --- T13 — crm.event_raw ------------------------------------------------
# The event spine's mailbox: everything that arrives, verbatim. Root table
# — the dedupe unique is (merchant_id, source, external_id), so merchant_id
# leads as the tenancy law requires.
#
# Note migration 051 records a deliberate, phase-1-only deviation from the
# canon's monthly RANGE partitioning (a partitioned table's unique
# constraints must include the partition key, which would break that
# dedupe). Partitioning is not part of the column shape this check diffs,
# so the deviation lives in the migration header rather than here.
CRM_EVENT_RAW = Table(
    canon_id="T13",
    name="crm_event_raw",
    canonical_name="crm.event_raw",
    tenant_scoped=True,
    columns=(
        Column("id", "uuid", not_null=True),
        Column("merchant_id", "text", not_null=True),
        Column("source", "text", not_null=True),
        Column("topic", "text", not_null=True),
        Column("schema_version", "text", not_null=True),
        Column("external_id", "text", not_null=True),
        Column("payload", "jsonb", not_null=True),
        Column("received_at", _TIMESTAMPTZ, not_null=True),
        Column("occurred_at", _TIMESTAMPTZ),
        Column("processed_at", _TIMESTAMPTZ),
        Column("quarantine_reason", "text"),
        # Nullable by ADR 0020: the row lands before resolve() stamps it.
        Column("customer_id", "uuid"),
    ),
)


SEALED: dict[str, Table] = {
    table.name: table for table in (PLATFORM_IDENTITY, CRM_CUSTOMER, CRM_EVENT_RAW)
}

# Canon table IDs not yet declared here. Listed explicitly so "pending" is a
# counted, visible number rather than the silence of an empty registry.
#
# The canon's numbering is NOT contiguous: it defines 17 tables — T02, T05,
# T07-T09, T11-T22 — plus the V01 journey view. T01, T03, T04, T06 and T10
# do not exist. Assuming a dense T01-T22 range put five phantom tables in
# this list, so the countdown could never reach zero, which defeats the
# point of counting. CANON_TABLE_IDS is the full set; pending is whatever
# is not yet sealed, derived rather than hand-maintained.
CANON_TABLE_IDS: tuple[str, ...] = (
    "T02",
    "T05",
    "T07",
    "T08",
    "T09",
    "T11",
    "T12",
    "T13",
    "T14",
    "T15",
    "T16",
    "T17",
    "T18",
    "T19",
    "T20",
    "T21",
    "T22",
)

CANON_VIEW_IDS: tuple[str, ...] = ("V01",)

PENDING_CANON_IDS: tuple[str, ...] = tuple(
    canon_id
    for canon_id in CANON_TABLE_IDS + CANON_VIEW_IDS
    if canon_id not in {table.canon_id for table in SEALED.values()}
)

# Any table with one of these prefixes is governed by the sealed schema.
# Anything outside them is pre-CPaaS and out of canon's scope entirely.
GOVERNED_PREFIXES: tuple[str, ...] = ("crm_", "platform_")


def is_governed(table_name: str) -> bool:
    """True if ``table_name`` falls inside the canon namespace."""
    return table_name.startswith(GOVERNED_PREFIXES)


def divergences() -> list[tuple[str, str]]:
    """(built_name, canonical_name) for every table whose name differs.

    Reported by the checker, never failed on: the divergence is a known,
    accepted state, but one that should stay in view until it is closed.
    """
    return [
        (table.name, table.canonical_name)
        for table in SEALED.values()
        if table.name != table.canonical_name.replace(".", "_")
        or "." in table.canonical_name
    ]


def find_by_canon_id(canon_id: str) -> Optional[Table]:
    for table in SEALED.values():
        if table.canon_id == canon_id:
            return table
    return None
