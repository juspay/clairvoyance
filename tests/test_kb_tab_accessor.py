"""Accessor tests with run_parameterized_query mocked — no live DB."""

from unittest.mock import AsyncMock, patch

import pytest

from app.database.accessor.breeze_buddy.knowledge_base import (
    get_kb_tab_rows,
    list_kb_tab_names,
)


async def test_get_kb_tab_rows_returns_document_name_text_pairs():
    fake_rows = [
        {"document_name": "Health Companion Sheet", "text": "row1 text"},
        {"document_name": "Health Companion Sheet", "text": "row2 text"},
    ]
    with patch(
        "app.database.accessor.breeze_buddy.knowledge_base.run_parameterized_query",
        new=AsyncMock(return_value=fake_rows),
    ):
        result = await get_kb_tab_rows(["kb-1"], "Pricing")

    assert result == [
        ("Health Companion Sheet", "row1 text"),
        ("Health Companion Sheet", "row2 text"),
    ]


async def test_get_kb_tab_rows_logs_and_reraises_on_error():
    with patch(
        "app.database.accessor.breeze_buddy.knowledge_base.run_parameterized_query",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        with pytest.raises(RuntimeError, match="db down"):
            await get_kb_tab_rows(["kb-1"], "Pricing")


async def test_list_kb_tab_names_returns_flat_list():
    fake_rows = [{"table_name": "Pricing"}, {"table_name": "Clinics"}]
    with patch(
        "app.database.accessor.breeze_buddy.knowledge_base.run_parameterized_query",
        new=AsyncMock(return_value=fake_rows),
    ):
        result = await list_kb_tab_names(["kb-1"])

    assert result == ["Pricing", "Clinics"]
