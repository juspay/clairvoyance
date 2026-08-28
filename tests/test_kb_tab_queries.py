"""Pure unit tests for the KB tab-scoped query builders — no DB needed."""

from app.database.queries.breeze_buddy.knowledge_base import (
    get_kb_tab_rows_query,
    list_kb_tab_names_query,
)


def test_get_kb_tab_rows_query_binds_kb_ids_and_tab_name():
    text, values = get_kb_tab_rows_query(["kb-1", "kb-2"], "Pricing")

    assert values == [["kb-1", "kb-2"], "Pricing"]
    assert "kb_chunk" in text
    assert "kb_document" in text
    assert "metadata" in text
    assert "'table'" in text
    assert "status" in text
    assert "READY" in text
    assert "chunk_index" in text


def test_get_kb_tab_rows_query_matches_case_insensitively():
    """An LLM calling get_tab_data("pricing") must still hit a tab actually
    named "Pricing" — case is the one mismatch an LLM is likely to make even
    when it got the name from list_kb_tabs, and this tool's entire value
    proposition over semantic search is a deterministic hit."""
    text, _ = get_kb_tab_rows_query(["kb-1"], "pricing")

    assert "LOWER(" in text
    assert "LOWER($2)" in text


def test_list_kb_tab_names_query_binds_kb_ids():
    text, values = list_kb_tab_names_query(["kb-1"])

    assert values == [["kb-1"]]
    assert "DISTINCT" in text
    assert "metadata" in text
    assert "'table'" in text


def test_list_kb_tab_names_query_excludes_empty_string_tab_names():
    text, _ = list_kb_tab_names_query(["kb-1"])

    assert "!= ''" in text
