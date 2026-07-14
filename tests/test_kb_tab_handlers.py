"""Handler tests — retrieval layer mocked, TemplateContext stubbed."""

from types import SimpleNamespace
from typing import List, cast
from unittest.mock import AsyncMock, patch

from app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data import (
    get_tab_data,
    list_tabs,
)
from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    DEFAULT_KB_INSTRUCTION,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext


def _context_with_kb(
    kb_ids: List[str], enabled: bool = True, injection_instruction=None
) -> TemplateContext:
    kb_config = SimpleNamespace(
        enabled=enabled,
        knowledge_base_ids=kb_ids,
        injection_instruction=injection_instruction,
    )
    configurations = SimpleNamespace(knowledge_base=kb_config)
    return cast(
        TemplateContext,
        SimpleNamespace(configurations=configurations, call_sid="CA123"),
    )


def _context_without_kb() -> TemplateContext:
    configurations = SimpleNamespace(knowledge_base=None)
    return cast(
        TemplateContext,
        SimpleNamespace(configurations=configurations, call_sid="CA123"),
    )


async def test_get_tab_data_errors_when_no_kb_attached():
    result = await get_tab_data(_context_without_kb(), {"tab_name": "Pricing"})
    assert result["status"] == "error"
    assert "No knowledge base" in result["message"]


async def test_get_tab_data_errors_when_tab_name_missing():
    result = await get_tab_data(_context_with_kb(["kb-1"]), {})
    assert result["status"] == "error"
    assert "tab_name" in result["message"]


async def test_get_tab_data_returns_content_on_success():
    with patch(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.get_kb_tab_text",
        new=AsyncMock(return_value=("iPhone 15,$999", False)),
    ):
        result = await get_tab_data(_context_with_kb(["kb-1"]), {"tab_name": "Pricing"})

    assert result["status"] == "success"
    assert result["content"] == "iPhone 15,$999"
    assert "truncated" not in result
    assert result["instruction"] == DEFAULT_KB_INSTRUCTION


async def test_get_tab_data_uses_templates_custom_injection_instruction():
    """Same untrusted-data framing query_knowledge_base already returns —
    the LLM must be told to treat tab content as reference material, not
    instructions, and a template-level override must win over the default."""
    with patch(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.get_kb_tab_text",
        new=AsyncMock(return_value=("iPhone 15,$999", False)),
    ):
        result = await get_tab_data(
            _context_with_kb(["kb-1"], injection_instruction="Answer in Hindi only."),
            {"tab_name": "Pricing"},
        )

    assert result["instruction"] == "Answer in Hindi only."


async def test_get_tab_data_surfaces_truncation_notice():
    with patch(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.get_kb_tab_text",
        new=AsyncMock(return_value=("first part of a big tab", True)),
    ):
        result = await get_tab_data(_context_with_kb(["kb-1"]), {"tab_name": "Big"})

    assert result["status"] == "success"
    assert result["truncated"] is True
    assert "narrow" in result["message"]


async def test_get_tab_data_reports_empty_tab_as_not_found():
    with patch(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.get_kb_tab_text",
        new=AsyncMock(return_value=("", False)),
    ):
        result = await get_tab_data(
            _context_with_kb(["kb-1"]), {"tab_name": "Nonexistent"}
        )

    assert result["status"] == "not_found"


async def test_get_tab_data_fails_open_on_timeout():
    async def _hang(*args, **kwargs):
        import asyncio

        await asyncio.sleep(10)

    with (
        patch(
            "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.get_kb_tab_text",
            new=_hang,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data._TAB_FETCH_TIMEOUT_SECS",
            0.01,
        ),
    ):
        result = await get_tab_data(_context_with_kb(["kb-1"]), {"tab_name": "Pricing"})

    assert result["status"] == "error"
    assert "timed out" in result["message"]


async def test_list_tabs_returns_names():
    with patch(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.kb_tab_data.list_kb_tabs",
        new=AsyncMock(return_value=["Pricing", "Clinics"]),
    ):
        result = await list_tabs(_context_with_kb(["kb-1"]), {})

    assert result["status"] == "success"
    assert result["tabs"] == ["Pricing", "Clinics"]


async def test_list_tabs_errors_when_no_kb_attached():
    result = await list_tabs(_context_without_kb(), {})
    assert result["status"] == "error"
