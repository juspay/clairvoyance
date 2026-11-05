"""Tests for the access-grant projection builders (dual-write step of the
normalized access model, migration 036).

The grant tables must remain a faithful projection of the authoritative
JSONB arrays on users:

- non-``"*"`` reseller_ids entries -> user_reseller_access rows, with
  ``all_workspaces`` true iff merchant_ids carries the ``"*"`` wildcard
- ``role='reseller'`` -> resellers row ensured + all-workspaces self-grant
- non-``"*"`` merchant_ids entries -> user_merchant_access rows
- unknown umbrella slugs are materialized as bare resellers rows first
  (mirrors the migration's array-derived backfill); unknown MERCHANTS are
  skipped — a typo must not create a workspace
- admin-style wildcards (no umbrella linkage) -> no grant rows at all
- rows the arrays no longer assert are pruned, surviving rows keep their
  created_at/created_by (upsert-then-prune, never blanket delete)

Pure query-builder tests — SQL string + params only, no DB.
"""

from __future__ import annotations

from app.database.queries.breeze_buddy.access_grants import (
    ensure_reseller_query,
    project_user_access_queries,
)


def _sql(statements):
    return [q for q, _ in statements]


def _joined(statements):
    return "\n".join(q for q, _ in statements)


def _prune_sql(statements):
    return "\n".join(q for q, _ in statements if q.strip().startswith("DELETE"))


# ── ensure_reseller_query ────────────────────────────────────────────────────


def test_ensure_reseller_defaults_name_to_slug() -> None:
    query, params = ensure_reseller_query("breeze")
    assert "INSERT INTO resellers" in query
    assert "ON CONFLICT (id) DO NOTHING" in query
    assert params == ["breeze", "breeze"]


def test_ensure_reseller_uses_display_name_when_given() -> None:
    _, params = ensure_reseller_query("breeze", "Breeze Labs")
    assert params == ["breeze", "Breeze Labs"]


# ── member accounts ──────────────────────────────────────────────────────────


def test_member_projects_umbrella_affiliation_and_workspace_rows() -> None:
    statements = project_user_access_queries(
        user_id="alice",
        role="user",
        reseller_ids=["breeze"],
        merchant_ids=["acme-store", "other-store"],
        created_by="admin",
    )
    joined = _joined(statements)

    # unknown umbrella slugs are materialized before the grant lands
    viv = next(
        (q, p) for q, p in statements if "INSERT INTO resellers" in q and "unnest" in q
    )
    assert viv[1] == [["breeze"]]
    assert statements.index(viv) < next(
        i for i, (q, _) in enumerate(statements) if "user_reseller_access" in q
    )

    # umbrella affiliation upsert, non-wildcard
    ura = next((q, p) for q, p in statements if "INSERT INTO user_reseller_access" in q)
    assert "DO UPDATE SET all_workspaces = EXCLUDED.all_workspaces" in ura[0]
    assert ura[1] == ["alice", ["breeze"], False, "admin"]

    # workspace membership rows are FK-safe (joined against merchants)
    uma = next((q, p) for q, p in statements if "INSERT INTO user_merchant_access" in q)
    assert "FROM merchants m" in uma[0]
    assert uma[1] == ["alice", ["acme-store", "other-store"], "admin"]

    # both prunes present, keeping exactly what was asserted
    assert "DELETE FROM user_reseller_access" in joined
    assert "DELETE FROM user_merchant_access" in joined
    prune_r = next(p for q, p in statements if "DELETE FROM user_reseller_access" in q)
    prune_m = next(p for q, p in statements if "DELETE FROM user_merchant_access" in q)
    assert prune_r == ["alice", ["breeze"]]
    assert prune_m == ["alice", ["acme-store", "other-store"]]

    # no blanket deletes — surviving rows keep created_at/created_by
    assert "WHERE user_id = $1 AND NOT" in _prune_sql(statements)


def test_umbrella_wildcard_becomes_all_workspaces_grant() -> None:
    statements = project_user_access_queries(
        user_id="owner-login",
        role="merchant",
        reseller_ids=["breeze"],
        merchant_ids=["*"],
    )
    ura = next(p for q, p in statements if "INSERT INTO user_reseller_access" in q)
    assert ura == ["owner-login", ["breeze"], True, None]

    # the "*" itself never lands in the membership table
    assert not any("INSERT INTO user_merchant_access" in q for q, _ in statements)
    prune_m = next(p for q, p in statements if "DELETE FROM user_merchant_access" in q)
    assert prune_m == ["owner-login", []]  # empty keep-set -> prune all rows


# ── reseller accounts ────────────────────────────────────────────────────────


def test_reseller_gets_umbrella_row_and_all_workspaces_self_grant() -> None:
    statements = project_user_access_queries(
        user_id="breeze",
        role="reseller",
        reseller_ids=["breeze"],
        merchant_ids=["*"],
        username="breeze-admin",
    )
    sqls = _sql(statements)

    # umbrella entity ensured first
    assert "INSERT INTO resellers" in sqls[0]
    assert statements[0][1] == ["breeze", "breeze-admin"]

    # unconditional self-grant with all_workspaces forced true
    self_grant = next((q, p) for q, p in statements if "VALUES ($1, $1, true, $2)" in q)
    assert "DO UPDATE SET all_workspaces = true" in self_grant[0]
    assert self_grant[1] == ["breeze", None]

    # own umbrella is in the prune keep-set exactly once
    prune_r = next(p for q, p in statements if "DELETE FROM user_reseller_access" in q)
    assert prune_r == ["breeze", ["breeze"]]


# ── admin-style wildcards ────────────────────────────────────────────────────


def test_unlinked_wildcards_project_to_nothing() -> None:
    statements = project_user_access_queries(
        user_id="root",
        role="admin",
        reseller_ids=["*"],
        merchant_ids=["*"],
    )
    # no inserts at all — global access is what role='admin' means
    assert not any(q.strip().startswith("INSERT") for q, _ in statements)
    # prunes still run with empty keep-sets so stale rows disappear
    prune_params = [p for q, p in statements if q.strip().startswith("DELETE")]
    assert prune_params == [["root", []], ["root", []]]


def test_empty_arrays_prune_everything() -> None:
    statements = project_user_access_queries(
        user_id="ghost", role="user", reseller_ids=None, merchant_ids=None
    )
    assert not any(q.strip().startswith("INSERT") for q, _ in statements)
    prune_params = [p for q, p in statements if q.strip().startswith("DELETE")]
    assert prune_params == [["ghost", []], ["ghost", []]]
