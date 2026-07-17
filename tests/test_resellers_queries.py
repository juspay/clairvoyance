"""Tests for reseller (umbrella) entity query builders and the effective-access
read queries (GET /user/{id}/access).

Pure query-builder tests — SQL string + params only, no DB.
"""

from __future__ import annotations

from app.database.queries.breeze_buddy.resellers import (
    create_reseller_query,
    delete_reseller_query,
    get_all_resellers_query,
    get_reseller_by_id_query,
    get_user_umbrella_grants_query,
    get_user_workspace_access_query,
    update_reseller_query,
)

# ── list ─────────────────────────────────────────────────────────────────────


def test_plain_list_has_no_filters_and_computed_columns() -> None:
    query, count_query, params = get_all_resellers_query()
    # no outer WHERE: the FROM..resellers line flows straight to ORDER BY
    # (the computed-column subqueries have their own inner WHEREs)
    outer = query.split("FROM resellers r")[1].split("ORDER BY")[0]
    assert "WHERE" not in outer
    assert params == [50, 0]
    # the console columns ride every read
    assert "workspace_count" in query
    assert "member_count" in query
    assert "has_login" in query
    assert "COUNT(*) as total" in count_query


def test_search_matches_id_or_name_with_escaping() -> None:
    query, count_query, params = get_all_resellers_query(id_or_name_filter="bre_eze%")
    assert "(r.id ILIKE $1 OR r.name ILIKE $1)" in query
    assert params == ["%bre\\_eze\\%%", 50, 0]
    assert "ILIKE $1" in count_query


def test_rbac_scope_restricts_to_allowed_ids() -> None:
    query, _, params = get_all_resellers_query(
        allowed_reseller_ids=["breeze"], page=2, limit=10
    )
    assert "r.id = ANY($1::text[])" in query
    assert params == [["breeze"], 10, 10]


def test_admin_scope_is_unrestricted() -> None:
    query, _, _ = get_all_resellers_query(allowed_reseller_ids=None)
    assert "ANY(" not in query


def test_sort_field_is_validated() -> None:
    query, _, _ = get_all_resellers_query(sort_by="; DROP TABLE", sort_order="up")
    assert "ORDER BY r.created_at DESC" in query


# ── single row / mutations ───────────────────────────────────────────────────


def test_get_by_id_carries_computed_columns() -> None:
    query, params = get_reseller_by_id_query("breeze")
    assert "WHERE r.id = $1" in query
    assert "has_login" in query
    assert params == ["breeze"]


def test_create_returns_row() -> None:
    query, params = create_reseller_query("breeze", "Breeze Labs", None, True)
    assert "INSERT INTO resellers" in query
    assert "RETURNING" in query
    assert params[:4] == ["breeze", "Breeze Labs", None, True]


def test_update_with_no_fields_is_a_noop() -> None:
    query, params = update_reseller_query("breeze")
    assert query == ""
    assert params == []


def test_update_sets_only_given_fields() -> None:
    query, params = update_reseller_query("breeze", name="New Name")
    set_clause = query.split("SET")[1].split("WHERE")[0]
    assert "name = $1" in set_clause
    assert "description" not in set_clause
    assert "is_active" not in set_clause
    assert "updated_at = $2" in set_clause
    assert params[0] == "New Name"
    assert params[-1] == "breeze"


def test_delete_is_plain_and_fk_guarded_by_schema() -> None:
    query, params = delete_reseller_query("breeze")
    assert query.strip().startswith("DELETE FROM resellers")
    assert "RETURNING id" in query
    assert params == ["breeze"]


# ── effective access ─────────────────────────────────────────────────────────


def test_umbrella_grants_join_reseller_names() -> None:
    query, params = get_user_umbrella_grants_query("alice")
    assert "FROM user_reseller_access ura" in query
    assert "LEFT JOIN resellers r" in query
    assert params == ["alice"]


def test_workspace_access_unions_explicit_and_inherited() -> None:
    query, params = get_user_workspace_access_query("alice")
    assert "'explicit' AS source" in query
    assert "'inherited' AS source" in query
    # inherited rows only flow through all-workspaces grants
    assert "ura.all_workspaces" in query
    # explicit membership wins on collision: DISTINCT ON ordered by source
    assert "DISTINCT ON (merchant_id)" in query
    assert "ORDER BY merchant_id, source" in query
    assert params == ["alice"]
