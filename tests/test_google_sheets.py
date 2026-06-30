"""Tests for app.services.google.sheets — URL extraction, formatting, and
fetch error paths (without real network calls).
"""

from __future__ import annotations

import json
from typing import Optional

import pytest

from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef
from app.services.data_sources import DATA_SOURCE_UNAVAILABLE
from app.services.google.sheets import (
    _rows_to_csv,
    _rows_to_json,
    _rows_to_markdown_table,
    extract_spreadsheet_id,
    fetch_formatted,
    fetch_sheet_data,
    get_column_headers,
    list_tabs,
)

# ---------------------------------------------------------------------------
# extract_spreadsheet_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0",
            "abc123",
        ),
        (
            "https://docs.google.com/spreadsheets/d/ABC-123_xyz/edit",
            "ABC-123_xyz",
        ),
        (
            "https://docs.google.com/spreadsheets/d/abc123",
            "abc123",
        ),
        ("https://example.com", None),
        ("", None),
        ("not-a-url", None),
    ],
)
def test_extract_spreadsheet_id(url: str, expected: Optional[str]):
    assert extract_spreadsheet_id(url) == expected


# ---------------------------------------------------------------------------
# _rows_to_markdown_table
# ---------------------------------------------------------------------------


def test_markdown_empty():
    assert _rows_to_markdown_table([], []) == "(no data)"


def test_markdown_no_rows():
    assert _rows_to_markdown_table(["a", "b"], []) == "(no data)"


def test_markdown_basic():
    result = _rows_to_markdown_table(
        ["Name", "Age"],
        [{"Name": "Alice", "Age": "30"}, {"Name": "Bob", "Age": "25"}],
    )
    lines = result.split("\n")
    assert lines[0] == "| Name | Age |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| Alice | 30 |"
    assert lines[3] == "| Bob | 25 |"


def test_markdown_missing_cell():
    result = _rows_to_markdown_table(
        ["A", "B", "C"],
        [{"A": "1", "B": "2"}],
    )
    assert "| 1 | 2 |  |" in result


def test_markdown_escapes_pipe_in_cell():
    result = _rows_to_markdown_table(
        ["Options"],
        [{"Options": "pick A | B"}],
    )
    assert "pick A \\| B" in result


def test_markdown_escapes_newline_in_cell():
    result = _rows_to_markdown_table(
        ["Desc"],
        [{"Desc": "line1\nline2"}],
    )
    assert "line1 line2" in result
    assert "\n" not in next(line for line in result.split("\n") if "line1" in line)


def test_markdown_escapes_backslash_in_cell():
    result = _rows_to_markdown_table(
        ["Path"],
        [{"Path": "a\\b"}],
    )
    assert "a\\\\b" in result


def test_markdown_escapes_pipe_in_header():
    result = _rows_to_markdown_table(
        ["A | B"],
        [{"A | B": "1"}],
    )
    assert "| A \\| B |" in result


def test_markdown_escapes_combined_special_chars():
    result = _rows_to_markdown_table(
        ["Cell"],
        [{"Cell": "a|b\nc\\d"}],
    )
    assert "a\\|b c\\\\d" in result


# ---------------------------------------------------------------------------
# _rows_to_csv
# ---------------------------------------------------------------------------


def test_csv_empty():
    assert _rows_to_csv([], []) == "(no data)"


def test_csv_basic():
    result = _rows_to_csv(
        ["Name", "Age"],
        [{"Name": "Alice", "Age": "30"}, {"Name": "Bob", "Age": "25"}],
    )
    assert result == "Name,Age\nAlice,30\nBob,25\n"


def test_csv_with_comma():
    result = _rows_to_csv(
        ["Desc"],
        [{"Desc": "hello, world"}],
    )
    assert result == 'Desc\n"hello, world"\n'


# ---------------------------------------------------------------------------
# _rows_to_json
# ---------------------------------------------------------------------------


def test_json_empty():
    assert _rows_to_json([]) == "[]"


def test_json_basic():
    rows = [{"a": "1"}, {"a": "2"}]
    result = _rows_to_json(rows)
    assert json.loads(result) == rows


# ---------------------------------------------------------------------------
# datasource_ namespace convention
# ---------------------------------------------------------------------------


def test_datasource_var_is_namespaced():
    ref = DataSourceRef.model_validate(
        {
            "name": "products",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/abc123/edit",
            "format": "markdown_table",
        }
    )
    var_key = f"datasource_{ref.name}"
    assert var_key == "datasource_products"
    assert var_key.startswith("datasource_")


def test_datasource_var_does_not_clash_with_bare_name():
    ref = DataSourceRef.model_validate(
        {
            "name": "api_token",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/abc123/edit",
            "format": "csv",
        }
    )
    var_key = f"datasource_{ref.name}"
    assert var_key == "datasource_api_token"
    assert var_key != ref.name


# ---------------------------------------------------------------------------
# Async helpers: faking the Google API session
# ---------------------------------------------------------------------------


def _fake_session(json_responses: list):
    """Fake AuthorizedSession that returns canned JSON responses.

    Each call to session.get consumes the next response in *json_responses*.
    Responses match real Sheets API shape — ``{"values": [[...], ...]}`` or
    ``{"sheets": [...]}``.
    """

    class _FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _FakeSession:
        def __init__(self):
            self._idx = 0
            self.closed = False

        def get(self, url: str, *, params: Optional[dict] = None, timeout: int = 10):
            resp = _FakeResponse(json_responses[self._idx])
            self._idx += 1
            return resp

        def close(self):
            self.closed = True

    return _FakeSession()


def _values_response(rows: list[list]) -> dict:
    """Wrap values list in the shape returned by the Sheets API."""
    return {"values": rows}


@pytest.fixture(autouse=True)
def patch_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: None,
    )


# ---------------------------------------------------------------------------
# list_tabs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tabs_success(monkeypatch):
    fake = _fake_session(
        [
            {
                "sheets": [
                    {"properties": {"title": "Sheet1"}},
                    {"properties": {"title": "Tab Two"}},
                ]
            }
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await list_tabs("abc123")
    assert result == ["Sheet1", "Tab Two"]


@pytest.mark.asyncio
async def test_list_tabs_no_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: None,
    )
    result = await list_tabs("abc123")
    assert result == []


@pytest.mark.asyncio
async def test_list_tabs_api_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    fake = _fake_session([])
    fake.get = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await list_tabs("abc123")
    assert result == []


# ---------------------------------------------------------------------------
# get_column_headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_headers_explicit_tab(monkeypatch):
    fake = _fake_session([_values_response([["Name", "Age", "City"]])])
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await get_column_headers("abc123", sheet_name="MyTab")
    assert result == ["Name", "Age", "City"]


@pytest.mark.asyncio
async def test_get_headers_auto_first_tab(monkeypatch):
    fake = _fake_session(
        [
            {"sheets": [{"properties": {"title": "FirstTab"}}]},
            _values_response([["Col1", "Col2"]]),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await get_column_headers("abc123", sheet_name=None)
    assert result == ["Col1", "Col2"]


@pytest.mark.asyncio
async def test_get_headers_no_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: None,
    )
    result = await get_column_headers("abc123")
    assert result == []


# ---------------------------------------------------------------------------
# fetch_sheet_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_data_basic(monkeypatch):
    fake = _fake_session(
        [
            _values_response([["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_sheet_data("abc123", sheet_name="Sheet1")
    assert result == [
        {"Name": "Alice", "Age": "30"},
        {"Name": "Bob", "Age": "25"},
    ]


@pytest.mark.asyncio
async def test_fetch_data_column_filter(monkeypatch):
    fake = _fake_session(
        [
            _values_response(
                [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]],
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_sheet_data("abc123", sheet_name="Sheet1", columns=["A", "C"])
    assert result == [
        {"A": "1", "C": "3"},
        {"A": "4", "C": "6"},
    ]


@pytest.mark.asyncio
async def test_fetch_data_short_rows_padded(monkeypatch):
    fake = _fake_session(
        [
            _values_response(
                [["A", "B", "C"], ["1", "2"]],
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_sheet_data("abc123", sheet_name="Sheet1")
    assert result == [{"A": "1", "B": "2", "C": ""}]


@pytest.mark.asyncio
async def test_fetch_data_no_session(monkeypatch):
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: None,
    )
    result = await fetch_sheet_data("abc123")
    assert result == []


# ---------------------------------------------------------------------------
# fetch_formatted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_formatted_markdown(monkeypatch):
    fake = _fake_session(
        [
            _values_response(
                [["Product", "Price"], ["Widget", "10"]],
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_formatted(
        "abc123", sheet_name="Sheet1", format="markdown_table"
    )
    assert "| Product | Price |" in result
    assert "| Widget | 10 |" in result


@pytest.mark.asyncio
async def test_fetch_formatted_csv(monkeypatch):
    fake = _fake_session(
        [
            _values_response([["A", "B"], ["1", "2"]]),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_formatted("abc123", sheet_name="Sheet1", format="csv")
    assert result == "A,B\n1,2\n"


@pytest.mark.asyncio
async def test_fetch_formatted_json(monkeypatch):
    fake = _fake_session(
        [
            _values_response([["A"], ["1"]]),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_formatted("abc123", sheet_name="Sheet1", format="json")
    assert json.loads(result) == [{"A": "1"}]


@pytest.mark.asyncio
async def test_fetch_formatted_default_is_markdown(monkeypatch):
    fake = _fake_session(
        [
            _values_response([["X"], ["Y"]]),
        ]
    )
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_formatted("abc123", sheet_name="Sheet1")
    assert "| X |" in result


@pytest.mark.asyncio
async def test_fetch_formatted_no_data_returns_placeholder(monkeypatch):
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: None,
    )
    result = await fetch_formatted("abc123", sheet_name="Empty")
    assert result == DATA_SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_fetch_formatted_empty_rows_returns_placeholder(monkeypatch):
    fake = _fake_session([_values_response([])])
    monkeypatch.setattr(
        "app.services.google.sheets._get_sheets_session",
        lambda: fake,
    )
    result = await fetch_formatted("abc123", sheet_name="Empty")
    assert result == DATA_SOURCE_UNAVAILABLE
