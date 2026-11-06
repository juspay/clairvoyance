"""Tests for access-aware user list scoping (``get_all_users_query``).

Since migration 036 the umbrella/workspace filters read the normalized grant
tables instead of the JSONB arrays, and must reflect *effective access*:

- ``merchant_identifier_filter`` (workspace view): explicit membership row OR
  an all-workspaces umbrella grant through the workspace's reseller.
- ``reseller_id_filter`` (umbrella view): an umbrella grant OR explicit
  membership in any of the umbrella's workspaces (rows whose affiliation was
  never recorded).
- ``allowed_merchant_ids`` (RBAC): explicit membership overlap OR an
  all-workspaces grant on an umbrella owning an allowed workspace; wildcard
  accounts with no umbrella grant (admin-style) have no rows and stay hidden.

Pure query-builder tests — SQL string + params only, no DB.
"""

from __future__ import annotations

from app.database.queries.breeze_buddy.users import get_all_users_query


def _build(**kwargs):
    query, count_query, params = get_all_users_query(**kwargs)
    return query, count_query, params


# ── workspace (merchant) filter ──────────────────────────────────────────────


def test_merchant_filter_matches_explicit_membership_and_umbrella_wildcard() -> None:
    query, count_query, params = _build(merchant_identifier_filter="acme-store")

    # explicit membership row
    assert "user_merchant_access uma" in query
    assert "uma.merchant_id = $1" in query
    # all-workspaces branch resolves the workspace's reseller through merchants
    assert "user_reseller_access ura" in query
    assert "ura.all_workspaces" in query
    assert "m.reseller_id = ura.reseller_id" in query
    assert "m.merchant_id = $1" in query
    assert params == ["acme-store", 50, 0]
    # the count query applies the identical WHERE clause
    assert "ura.all_workspaces" in count_query


def test_merchant_filter_is_one_param_used_twice() -> None:
    query, _, params = _build(merchant_identifier_filter="acme-store")
    assert params.count("acme-store") == 1
    assert query.count("$1") >= 2


# ── umbrella (reseller) filter ───────────────────────────────────────────────


def test_reseller_filter_matches_grant_or_umbrella_workspace_membership() -> None:
    query, _, params = _build(reseller_id_filter="breeze")

    # direct umbrella grant
    assert "ura.reseller_id = $1" in query
    # membership in any workspace the umbrella owns
    assert "m.merchant_id = uma.merchant_id" in query
    assert "m.reseller_id = $1" in query
    assert params == ["breeze", 50, 0]


# ── RBAC allowed set ─────────────────────────────────────────────────────────


def test_rbac_scope_covers_same_umbrella_wildcard_rows() -> None:
    query, _, params = _build(allowed_merchant_ids=["m1", "m2"])

    assert "uma.merchant_id = ANY($1::text[])" in query
    assert "ura.all_workspaces" in query
    assert "m.merchant_id = ANY($1::text[])" in query
    assert params == [["m1", "m2"], 50, 0]


def test_rbac_wildcard_caller_is_unrestricted() -> None:
    query, _, params = _build(allowed_merchant_ids=["*"])
    assert "user_merchant_access" not in query
    assert params[-2:] == [50, 0]  # only limit/offset


def test_no_jsonb_operators_remain_in_scoping_filters() -> None:
    query, _, _ = _build(
        merchant_identifier_filter="m1",
        reseller_id_filter="breeze",
        allowed_merchant_ids=["m1"],
    )
    assert "merchant_ids ?" not in query
    assert "reseller_ids ?" not in query


# ── combination: workspace view under a scoped caller ────────────────────────


def test_workspace_filter_composes_with_rbac_scope() -> None:
    query, _, params = _build(
        merchant_identifier_filter="m1",
        allowed_merchant_ids=["m1", "m2"],
        role_filter="user",
    )
    # three conditions ANDed: role, workspace filter, RBAC scope
    assert query.count(" AND ") >= 2
    assert params[0] == "user"
    assert params[1] == "m1"
    assert params[2] == ["m1", "m2"]


def test_plain_list_has_no_filters() -> None:
    query, _, params = _build()
    assert "WHERE" not in query
    assert params == [50, 0]
