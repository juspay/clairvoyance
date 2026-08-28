"""Tests for chunk_table's handling of blank/missing header rows.

Regression coverage for a live-discovered bug: a sheet tab whose header row
is empty (or whitespace-only, e.g. a single space cell) produced ZERO
chunks — every real data row was silently discarded, even when the rows
themselves held real content. Root cause: chunk_table paired headers/cells
with `zip()`, which yields nothing when `headers` is empty, and the
downstream `if header and cell` filter dropped every cell whose header
column was blank. Both combined to make row content vanish silently
whenever a header cell was missing, with no warning anywhere.
"""

from app.services.knowledge_base.chunking import chunk_table
from app.services.knowledge_base.types import TableData


def test_blank_header_row_still_produces_chunks_from_real_data():
    """headers=[] (fully empty first row) must not discard the data rows."""
    table = TableData(
        name="INF-001",
        headers=[],
        rows=[
            ["Protocol ID", "INF-001"],
            ["Condition", "Fever"],
        ],
    )
    chunks = chunk_table(table)

    row_chunks = [c for c in chunks if not c.metadata.get("summary")]
    assert len(row_chunks) == 2
    assert any("Protocol ID" in c.text and "INF-001" in c.text for c in row_chunks)
    assert any("Condition" in c.text and "Fever" in c.text for c in row_chunks)
    # every chunk still carries the table tag get_tab_data depends on
    assert all(c.metadata.get("table") == "INF-001" for c in row_chunks)


def test_whitespace_only_header_still_produces_chunks_from_real_data():
    """A header cell that's just whitespace (e.g. '  ') strips to empty and
    must be treated the same as a fully blank header, not silently dropped."""
    table = TableData(
        name="RESP-001",
        headers=["  "],
        rows=[["Cough for 3 days"], ["No fever reported"]],
    )
    chunks = chunk_table(table)

    row_chunks = [c for c in chunks if not c.metadata.get("summary")]
    assert len(row_chunks) == 2
    assert any("Cough for 3 days" in c.text for c in row_chunks)


def test_mixed_headers_some_blank_some_real_keeps_all_cells():
    """A row with a real header for column 1 but a blank header for column 2
    must keep both cells — the labeled one formatted, the unlabeled one bare."""
    table = TableData(
        name="Mixed",
        headers=["Field", ""],
        rows=[["Priority", "High"]],
    )
    chunks = chunk_table(table)

    row_chunks = [c for c in chunks if not c.metadata.get("summary")]
    assert len(row_chunks) == 1
    text = row_chunks[0].text
    assert "Field: Priority" in text
    assert "High" in text


def test_normal_header_row_unchanged():
    """Existing, healthy tabs must format exactly as before this fix."""
    table = TableData(
        name="GI-001",
        headers=["PROTOCOL OVERVIEW"],
        rows=[["Protocol ID"], ["Condition"]],
    )
    chunks = chunk_table(table)

    row_chunks = [c for c in chunks if not c.metadata.get("summary")]
    assert len(row_chunks) == 2
    assert row_chunks[0].text == "GI-001 — PROTOCOL OVERVIEW: Protocol ID"


def test_summary_chunk_still_emitted_when_headers_are_blank():
    """The tab-summary chunk (used by list_kb_tabs discovery) must not
    silently disappear just because the header row happens to be blank."""
    table = TableData(
        name="INF-001",
        headers=[],
        rows=[["Protocol ID", "INF-001"]],
    )
    chunks = chunk_table(table)

    summary_chunks = [c for c in chunks if c.metadata.get("summary")]
    assert len(summary_chunks) == 1
    assert "INF-001" in summary_chunks[0].text


def test_fully_empty_row_still_skipped():
    """A genuinely empty row (all cells blank) must still be skipped —
    this fix is about blank HEADERS, not about resurrecting blank ROWS."""
    table = TableData(
        name="T",
        headers=[],
        rows=[["", ""], ["Real value", ""]],
    )
    chunks = chunk_table(table)

    row_chunks = [c for c in chunks if not c.metadata.get("summary")]
    assert len(row_chunks) == 1
    assert "Real value" in row_chunks[0].text
