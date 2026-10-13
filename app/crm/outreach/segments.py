"""Segments (canon T15 + T21, canon/05-audiences.md) — a static list's row
(phase 1), its members (phase 2), a dynamic segment's rule (phase 3) and its
answer (phase 4).

A segment is a stored question; membership is computed at read time and
never stored. A static list is the one exception — an imported set IS its
members, stored as narrow rows (crm_segment_member). A dynamic segment's
rule is checked and saved here (segment_rules.py); who it holds for is
asked of record, which owns the events (record.contracts.customers_matching
/ count_matching — outreach never reads crm_event_raw itself).

What this file keeps:
- **A segment is never a permission list** (modules/05-outreach.md:233).
  Nothing here sends, consents or suppresses; a broadcast still asks the
  gate about every member.
- **People are never stored in arrays** (canon/05-audiences.md:45-47): every
  person goes through resolve() — create or attach — and becomes one member
  row; a person resolve() cannot use is a skipped line WITH its reason.
- **The name is unique per merchant** (T15 col 3). The unique index referees
  two racing creates; this file only names the refusal.
- **Counts carry their stamp, and a list's count is exact** (T15 cols 7-8):
  recounted from the rows and stamped by the same transaction that changed
  them. A dynamic segment's count is a cache, stamped with the moment it
  was asked; nothing decides on it.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from app.crm.identity.contracts import resolve
from app.crm.outreach.db import DbTxn, UniqueViolation, atomically
from app.crm.outreach.db.accessors import (
    segment as segment_accessor,
    segment_member as segment_member_accessor,
)
from app.crm.outreach.schemas import (
    MembersAdded,
    Segment,
    SegmentMember,
    SegmentMembersPage,
    SegmentPage,
    SkippedMember,
)
from app.crm.outreach.segment_rules import (
    SegmentRuleInvalid,
    gather_rule_catalogs,
    validate_rule,
)
from app.crm.record.contracts import (
    SegmentRuleUnanswerable,
    count_matching,
    customers_matching,
)

#: One add takes at most this many people. Each person is one resolve() —
#: sequential, so this bounds how long one request runs and how large the
#: one INSERT's array is. A bigger file is added in batches (phase 3, CSV
#: import), each its own add.
MAX_MEMBERS_PER_ADD = 1000

#: The evidence class a merchant's list carries into resolve() (ADR 0021):
#: imported — it may attach a handle to a customer, never displace one that
#: live traffic established.
MEMBER_EVIDENCE = "imported"

#: The audit label resolve() logs for a person who arrived through a list.
MEMBER_SOURCE_LABEL = "segment_member"

#: Why a person did not become a member — the report's reason word: no
#: phone, email or external_ref survived normalization (e.g. "abc" as a
#: phone), so there is nobody to resolve.
SKIP_NO_USABLE_HANDLE = "no_usable_handle"


class SegmentNameTaken(Exception):
    """The merchant already has a segment with this name (T15 col 3)."""


class SegmentNotFound(Exception):
    """This merchant has no segment with this id."""


class SegmentIsDynamic(Exception):
    """Members are added to static lists only (T21). A dynamic segment's
    members are computed from its definition, never added as rows."""


class MemberPlan(NamedTuple):
    """What an add will write: the unique customers, in first-seen order, and
    how many lines repeated a customer already earlier in the same upload."""

    customer_ids: List[str]
    duplicates: int


def segment_name(raw: str) -> str:
    """The name as stored: surrounding whitespace dropped, so "Diwali VIPs"
    and "Diwali VIPs " are one name to the unique index, not two lists a
    merchant cannot tell apart. Pure. Raises ValueError when nothing is
    left — a blank name is not a name. The 085 CHECK refuses a blank or
    untrimmed name too, so a writer that skips this cannot split a name."""
    name = (raw or "").strip()
    if not name:
        raise ValueError("a segment needs a name")
    return name


def plan_members(resolved_ids: List[str]) -> MemberPlan:
    """Pure DECIDE: the unique customers to add, first-seen order kept. Two
    lines are one customer when they carry the same phone — or a phone and an
    email that resolve() already knows to be one person — so the second one
    is counted as a duplicate in this upload, not added twice."""
    unique = list(dict.fromkeys(resolved_ids))
    return MemberPlan(customer_ids=unique, duplicates=len(resolved_ids) - len(unique))


async def create_static_list(
    merchant_id: str, name: str, created_by: Optional[str]
) -> Segment:
    """A new, empty static list. Raises SegmentNameTaken when the merchant
    already has a segment by this name, ValueError on a blank name."""
    stored = segment_name(name)
    try:
        return await segment_accessor.insert_static_list(
            merchant_id, stored, created_by
        )
    except UniqueViolation as e:
        raise SegmentNameTaken(stored) from e


async def create_dynamic_segment(
    merchant_id: str,
    name: str,
    definition: Dict[str, Any],
    created_by: Optional[str],
) -> Segment:
    """A new dynamic segment: the rule is checked against the merchant's own
    event catalog BEFORE it is stored — every event one they send, every
    field declared for it, every op fit for the field (segment_rules.py).
    Raises SegmentRuleInvalid (with every problem), SegmentNameTaken, or
    ValueError on a blank name."""
    stored = segment_name(name)
    catalogs = await gather_rule_catalogs(merchant_id, definition)
    problems = validate_rule(definition, catalogs)
    if problems:
        raise SegmentRuleInvalid(problems)
    try:
        return await segment_accessor.insert_dynamic_segment(
            merchant_id, stored, definition, created_by
        )
    except UniqueViolation as e:
        raise SegmentNameTaken(stored) from e


async def get_segment(merchant_id: str, segment_id: str) -> Optional[Segment]:
    """One segment, or None when this merchant has no such segment — another
    merchant's id answers None too, never their row."""
    return await segment_accessor.get_segment(merchant_id, segment_id)


async def list_segments(merchant_id: str, limit: int, offset: int) -> SegmentPage:
    """One page of the merchant's segments, newest first, and the total."""
    return SegmentPage(
        items=await segment_accessor.list_segments(merchant_id, limit, offset),
        total=await segment_accessor.count_segments(merchant_id),
    )


async def _segment(merchant_id: str, segment_id: str) -> Segment:
    """GATHER: this merchant's segment. Raises SegmentNotFound."""
    segment = await segment_accessor.get_segment(merchant_id, segment_id)
    if segment is None:
        raise SegmentNotFound(segment_id)
    return segment


async def _static_list(merchant_id: str, segment_id: str) -> Segment:
    """GATHER: this merchant's segment, which must be a static list. Raises
    SegmentNotFound / SegmentIsDynamic."""
    segment = await _segment(merchant_id, segment_id)
    if segment.kind != "static":
        raise SegmentIsDynamic(segment_id)
    return segment


async def add_members(
    merchant_id: str,
    segment_id: str,
    people: List[Dict[str, str]],
    source: Optional[str],
) -> MembersAdded:
    """Add people to a static list. Each person is a handles dict ({"phone":
    ..., "email": ..., "external_ref": ...}); each goes through resolve()
    with imported evidence and becomes one member row. Idempotent: sending
    the same people again adds nobody.

    The segment is checked BEFORE anyone is resolved, so a wrong id or a
    dynamic segment creates no customers. resolve() commits on its own (its
    own atom); the member rows and the count then share one atom.

    Raises ValueError (no people, or more than MAX_MEMBERS_PER_ADD),
    SegmentNotFound, SegmentIsDynamic."""
    if not people:
        raise ValueError("send at least one person")
    if len(people) > MAX_MEMBERS_PER_ADD:
        raise ValueError(
            f"at most {MAX_MEMBERS_PER_ADD} people per add — send the rest in "
            "another add"
        )
    await _static_list(merchant_id, segment_id)

    resolved: List[str] = []
    skipped: List[SkippedMember] = []
    for line, handles in enumerate(people, start=1):
        try:
            customer_id = await resolve(
                merchant_id,
                handles,
                evidence=MEMBER_EVIDENCE,
                source=MEMBER_SOURCE_LABEL,
            )
        except ValueError:
            # resolve() refuses only a person with no usable handle here
            # (merchant and evidence are fixed above) — a skipped line.
            skipped.append(SkippedMember(line=line, reason=SKIP_NO_USABLE_HANDLE))
            continue
        resolved.append(str(customer_id))

    plan = plan_members(resolved)
    added, member_count, computed_at = await atomically(
        _add_members_in_txn, merchant_id, segment_id, plan.customer_ids, source
    )
    return MembersAdded(
        received=len(people),
        added=added,
        already_member=len(plan.customer_ids) - added,
        duplicates_in_upload=plan.duplicates,
        skipped=skipped,
        member_count=member_count,
        computed_at=computed_at,
    )


async def _add_members_in_txn(
    txn: DbTxn,
    merchant_id: str,
    segment_id: str,
    customer_ids: List[str],
    source: Optional[str],
) -> Tuple[int, int, datetime]:
    """ATOMIC: the member rows and the list's count share one fate — the
    count is exact (T15 col 7) only when the transaction that changed the
    rows stamps it. The segment row is locked first, so two adds to one list
    run in turn and each recount sees the other's rows; the lock also
    re-checks, under the lock, that the list is still this merchant's and
    still static."""
    is_static = await segment_accessor.lock_segment(txn, merchant_id, segment_id)
    if is_static is None:
        raise SegmentNotFound(segment_id)
    if not is_static:
        raise SegmentIsDynamic(segment_id)
    added = await segment_member_accessor.insert_members(
        txn, segment_id, customer_ids, source
    )
    member_count, computed_at = await segment_accessor.stamp_member_count(
        txn, merchant_id, segment_id
    )
    return added, member_count, computed_at


async def segment_members(
    merchant_id: str, segment_id: str, after: Optional[str], limit: int
) -> SegmentMembersPage:
    """evaluate(id), one chunk at a time (canon/05-audiences.md wiring), in
    customer_id order: a static list's members are its rows; a dynamic
    segment's are whoever its rule holds for right now, answered by record.
    Pass the page's ``next_after`` as ``after`` for the next page; None means
    the last page was reached. Raises SegmentNotFound, or SegmentRuleInvalid
    when record cannot answer the stored rule."""
    segment = await _segment(merchant_id, segment_id)
    if segment.kind == "static":
        items = await segment_member_accessor.member_page(
            merchant_id, segment_id, after, limit
        )
    else:
        try:
            ids = await customers_matching(
                merchant_id, segment.definition, after, limit
            )
        except SegmentRuleUnanswerable as e:
            raise SegmentRuleInvalid(e.problems) from e
        items = [SegmentMember(customer_id=customer_id) for customer_id in ids]
    next_after = items[-1].customer_id if len(items) == limit else None
    return SegmentMembersPage(items=items, next_after=next_after)


async def count_segment(merchant_id: str, segment_id: str) -> Segment:
    """count(id) -> the segment with its count and stamp (canon/05-audiences.md
    wiring). A static list's count is already exact (stamped by every add),
    so it is returned as it stands. A dynamic segment is counted now by
    record and the cache stamped with the moment it was asked. Raises
    SegmentNotFound, or SegmentRuleInvalid when record cannot answer the
    stored rule."""
    segment = await _segment(merchant_id, segment_id)
    if segment.kind == "static":
        return segment
    asked_at = datetime.now(timezone.utc)
    try:
        n = await count_matching(merchant_id, segment.definition)
    except SegmentRuleUnanswerable as e:
        raise SegmentRuleInvalid(e.problems) from e
    stamped = await segment_accessor.stamp_dynamic_count(
        merchant_id, segment_id, n, asked_at
    )
    # None: a count asked later already stands (the stamp only moves
    # forward) — return that one; or the segment is gone.
    return stamped or await _segment(merchant_id, segment_id)
