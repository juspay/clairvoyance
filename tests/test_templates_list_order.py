"""``get_templates_list_query`` must produce a total order: LIMIT/OFFSET pages
over a sort with ties (``name`` is shared by hundreds of voice templates)
overlap and skip rows, which is how the console's agents list lost agents."""

from __future__ import annotations

from app.database.queries.breeze_buddy.template import get_templates_list_query


def test_paginated_list_breaks_name_ties_with_id():
    sql, values = get_templates_list_query(
        {"reseller_id": "BB_SHOPIFY", "limit": 100, "offset": 200}
    )
    assert "ORDER BY name, id" in sql
    assert "LIMIT" in sql and "OFFSET" in sql
    assert values[-2:] == [100, 200]


def test_unpaginated_list_keeps_newest_first_with_id_tiebreak():
    sql, values = get_templates_list_query({"reseller_id": "BB_SHOPIFY"})
    assert "ORDER BY created_at DESC, id" in sql
    assert "LIMIT" not in sql and "OFFSET" not in sql
