"""Template version query builders — SQL-shape tests.

These builders feed conn.fetch() inside the template-write transaction
(Task 4), so the tests pin the properties the design depends on:
parameterization (no value interpolation), the MAX+1 scalar subquery that
makes version numbering atomic under the template row lock, and the list
query staying blob-free so the history panel is cheap.
"""

from app.database.queries.breeze_buddy.template_version import (
    count_template_versions_query,
    get_template_version_query,
    insert_template_version_query,
    list_template_versions_query,
)

TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


def test_insert_computes_next_version_via_scalar_subquery():
    query, values = insert_template_version_query(
        template_id=TEMPLATE_ID,
        name="order-confirmation",
        flow='{"initial_node": "greeting", "nodes": {"greeting": {}}}',
        configurations='{"stt_language": "en"}',
        expected_payload_schema=None,
        expected_callback_response_schema=None,
        updated_by="ops@breeze.in",
        change_source="update",
        restored_from=None,
    )
    assert "COALESCE(MAX(version_number), 0) + 1" in query
    assert "RETURNING version_number" in query
    # Same $1 used for both the row and the subquery's WHERE.
    assert query.count("$1") == 2
    assert values[0] == TEMPLATE_ID
    assert values[-2] == "update"
    assert values[-1] is None


def test_insert_is_fully_parameterized():
    query, _ = insert_template_version_query(
        TEMPLATE_ID, "n", "{}", None, None, None, None, "create", None
    )
    # No value ever lands in the SQL text itself.
    assert TEMPLATE_ID not in query
    assert "create" not in query.split("VALUES")[1]


def test_insert_restored_from_is_last_param():
    _, values = insert_template_version_query(
        TEMPLATE_ID, "n", "{}", None, None, None, "admin", "rollback", 5
    )
    assert values[-2:] == ["rollback", 5]


def test_list_query_excludes_blobs_and_orders_desc():
    query, values = list_template_versions_query(TEMPLATE_ID, limit=20, offset=0)
    assert "ORDER BY version_number DESC" in query
    select_clause = query.split("FROM")[0]
    assert "flow" not in select_clause
    assert "configurations" not in select_clause
    assert "schema" not in select_clause
    assert values == [TEMPLATE_ID, 20, 0]


def test_count_query():
    query, values = count_template_versions_query(TEMPLATE_ID)
    assert "COUNT(*)" in query
    assert values == [TEMPLATE_ID]


def test_get_version_query_selects_full_row():
    query, values = get_template_version_query(TEMPLATE_ID, 7)
    for col in (
        "flow",
        "configurations",
        "expected_payload_schema",
        "expected_callback_response_schema",
        "restored_from",
    ):
        assert col in query
    assert values == [TEMPLATE_ID, 7]
