"""Segments (canon T15, canon/05-audiences.md) — phase 1: a static list's
row, created and read.

A segment is a stored question; membership is computed at read time and
never stored. A static list is the one exception — an imported set IS its
members (phase 2 adds them; phase 4 compiles dynamic definitions). Until
then every segment this file creates is a static list.

What this file keeps:
- **A segment is never a permission list** (modules/05-outreach.md:233).
  Nothing here sends, consents or suppresses; a broadcast still asks the
  gate about every member.
- **The name is unique per merchant** (T15 col 3). The unique index referees
  two racing creates; this file only names the refusal.
- **Counts carry their stamp** (T15 col 8): a new list's count is exact —
  zero members, stamped at birth.
"""

from typing import Optional

from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.accessors import segment as segment_accessor
from app.crm.outreach.schemas import Segment, SegmentPage


class SegmentNameTaken(Exception):
    """The merchant already has a segment with this name (T15 col 3)."""


def segment_name(raw: str) -> str:
    """The name as stored: surrounding whitespace dropped, so "Diwali VIPs"
    and "Diwali VIPs " are one name to the unique index, not two lists a
    merchant cannot tell apart. Pure. Raises ValueError when nothing is
    left — a blank name is not a name. The 086 CHECK refuses a blank or
    untrimmed name too, so a writer that skips this cannot split a name."""
    name = (raw or "").strip()
    if not name:
        raise ValueError("a segment needs a name")
    return name


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
