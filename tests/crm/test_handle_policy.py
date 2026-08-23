"""The ADR 0021 handle-lifecycle policy: attach if free, overwrite on
declared/observed, imported never displaces, inferred refused outright.
plan_handle_writes is the policy table as a pure function; resolve()'s
pre-DB validation is tested directly (it raises before any connection)."""

import asyncio
from typing import Any, Dict

import pytest

from app.crm.identity.resolve import (
    pick_survivor,
    plan_handle_writes,
    plan_resolution,
    resolve,
)

CURRENT = {
    "phone": "+919876543210",
    "email": "old@example.com",
    "igsid": None,
    "shopify_customer_id": None,
    "external_ref": None,
}


def test_attach_when_free() -> None:
    writes = plan_handle_writes(CURRENT, {"igsid": "ig_1"}, "imported")
    assert writes == {"igsid": "ig_1"}


def test_same_value_is_noop() -> None:
    writes = plan_handle_writes(CURRENT, {"phone": "+919876543210"}, "declared")
    assert writes == {}


@pytest.mark.parametrize("evidence", ["declared", "observed"])
def test_ladder_overwrites(evidence) -> None:
    writes = plan_handle_writes(CURRENT, {"email": "new@example.com"}, evidence)
    assert writes == {"email": "new@example.com"}


def test_imported_never_displaces() -> None:
    writes = plan_handle_writes(CURRENT, {"email": "bulk@example.com"}, "imported")
    assert writes == {}


def test_mixed_attach_and_keep() -> None:
    incoming = {"email": "bulk@example.com", "igsid": "ig_1"}
    writes = plan_handle_writes(CURRENT, incoming, "imported")
    assert writes == {"igsid": "ig_1"}  # attach free, keep occupied


def test_resolve_refuses_inferred() -> None:
    # resolve validates before touching any connection, so driving the
    # coroutine synchronously is safe (and avoids an async-plugin dep).
    with pytest.raises(ValueError, match="guesses"):
        asyncio.run(resolve("m1", {"phone": "+919876543210"}, evidence="inferred"))


def test_resolve_requires_merchant() -> None:
    with pytest.raises(ValueError, match="merchant_id"):
        asyncio.run(resolve("", {"phone": "+919876543210"}))


def test_resolve_requires_usable_handle() -> None:
    with pytest.raises(ValueError, match="usable handle"):
        asyncio.run(resolve("m1", {"phone": "garbage"}))


def _owner(id_: str, first_seen: int, **handles: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": id_, "first_seen_at": first_seen}
    for col in ("phone", "email", "igsid", "shopify_customer_id", "external_ref"):
        row.setdefault(col, handles.get(col))
    return row


def test_plan_creates_when_no_owner() -> None:
    plan = plan_resolution([], {"phone": "+919876543210"}, "observed")
    assert plan.create and plan.survivor_id is None and plan.losers == ()


def test_plan_staples_to_oldest_owner() -> None:
    a = _owner("a", 1, email="e1@x.com")  # oldest -> survivor
    b = _owner("b", 2, phone="+919876543210")
    plan = plan_resolution(
        [b, a], {"phone": "+919876543210", "email": "e1@x.com"}, "observed"
    )
    assert not plan.create
    assert plan.survivor_id == "a"
    assert plan.losers == ("b",)
    assert plan.writes == {"phone": "+919876543210"}  # attach freed handle


def test_survivor_tie_breaks_to_lower_id() -> None:
    a = _owner("a", 1)
    b = _owner("b", 1)
    assert pick_survivor([b, a])["id"] == "a"


def test_whitespace_handle_cannot_mint_a_customer() -> None:
    # regression (PR #1016 review): "   " must not survive normalization —
    # a customer with zero usable handles must not exist.
    from app.crm.identity.resolve import _normalize

    assert _normalize({"igsid": "   "}) == {}
    with pytest.raises(ValueError, match="usable handle"):
        asyncio.run(resolve("m1", {"igsid": "   "}))
