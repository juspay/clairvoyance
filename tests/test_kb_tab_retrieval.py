"""Retrieval/cache tests — accessor and Redis both mocked."""

from unittest.mock import AsyncMock, patch

from app.services.knowledge_base import retrieval
from app.services.knowledge_base.retrieval import get_kb_tab_text, list_kb_tabs


async def test_get_kb_tab_text_returns_empty_for_no_kb_ids():
    text, truncated = await get_kb_tab_text([], "Pricing")
    assert text == ""
    assert truncated is False


async def test_get_kb_tab_text_concatenates_rows_with_document_headers():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with (
        patch(
            "app.services.knowledge_base.retrieval._get_kb_versions",
            new=AsyncMock(return_value=["3"]),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_redis_service",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_kb_tab_rows",
            new=AsyncMock(
                return_value=[
                    ("Health Companion Sheet", "iPhone 15,$999"),
                    ("Health Companion Sheet", "iPhone 16,$1099"),
                ]
            ),
        ),
    ):
        text, truncated = await get_kb_tab_text(["kb-1"], "Pricing")

    assert "Health Companion Sheet" in text
    assert "iPhone 15,$999" in text
    assert "iPhone 16,$1099" in text
    assert truncated is False
    fake_redis.setex.assert_called_once()


async def test_get_kb_tab_text_truncates_oversized_tab_and_flags_it():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    huge_row = "x" * (retrieval._TAB_TEXT_MAX_CHARS + 5_000)
    with (
        patch(
            "app.services.knowledge_base.retrieval._get_kb_versions",
            new=AsyncMock(return_value=["3"]),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_redis_service",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_kb_tab_rows",
            new=AsyncMock(return_value=[("Big Sheet", huge_row)]),
        ),
    ):
        text, truncated = await get_kb_tab_text(["kb-1"], "Big")

    assert truncated is True
    assert len(text) <= retrieval._TAB_TEXT_MAX_CHARS


async def test_get_kb_tab_text_returns_cached_value_on_hit():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = '{"text": "cached tab text", "truncated": false}'
    with (
        patch(
            "app.services.knowledge_base.retrieval._get_kb_versions",
            new=AsyncMock(return_value=["3"]),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_redis_service",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_kb_tab_rows",
            new=AsyncMock(side_effect=AssertionError("should not hit DB on cache hit")),
        ),
    ):
        text, truncated = await get_kb_tab_text(["kb-1"], "Pricing")

    assert text == "cached tab text"
    assert truncated is False


async def test_list_kb_tabs_returns_names_from_accessor():
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with (
        patch(
            "app.services.knowledge_base.retrieval._get_kb_versions",
            new=AsyncMock(return_value=["1"]),
        ),
        patch(
            "app.services.knowledge_base.retrieval.get_redis_service",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch(
            "app.services.knowledge_base.retrieval.list_kb_tab_names",
            new=AsyncMock(return_value=["Pricing", "Clinics"]),
        ),
    ):
        result = await list_kb_tabs(["kb-1"])

    assert result == ["Pricing", "Clinics"]
