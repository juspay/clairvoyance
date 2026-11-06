"""``/assist/template/{id}/fields``: what the merchant edits, and what a save changes."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.skeleton import COMMERCE_V2
from app.ai.voice.agents.breeze_buddy.assist.engine.prompt_core import split_prompt
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.ai.voice.agents.breeze_buddy.assist.verticals.fields import apply_edits
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.schemas.breeze_buddy.assist.onboarding.template import AssistTemplateRequest

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def _blueprint() -> TemplateModel:
    body = json.loads((FIXTURE / "buddy-assist-default.v2.json").read_text())
    return TemplateModel(
        id="00000000-0000-0000-0000-00000000b1e0",
        reseller_id="BB_ASSIST",
        merchant_id=None,
        name=body["name"],
        flow=body["flow"],
        expected_payload_schema=body["expected_payload_schema"],
        expected_callback_response_schema=body["expected_callback_response_schema"]
        or {},
        configurations=body["configurations"],
        secrets={},
        is_active=True,
        supported_channels=body["supported_channels"],
    )


def _assistant(fields: Dict[str, List[str]]) -> TemplateModel:
    request = AssistTemplateRequest(
        reseller_id="BB_ASSIST",
        merchant_id="kosha",
        merchant_name="Kosha",
        website_url="https://kosha.example",
        platform="web",
    ).as_onboarding_request()
    return service.build_merchant_template(
        default_template=_blueprint(),
        body=request,
        website_context="",
        template_id="00000000-0000-0000-0000-00000000a551",
        existing_template=None,
        adapter=registry.for_request("web"),
        vertical=verticals.DEFAULT,
        fields=fields,
    )


def _hand_written() -> TemplateModel:
    """An assistant set up before the form: its brand block written by hand."""
    built = _assistant({"brand_line": ["Kosha"]})
    _, operating = split_prompt(built.flow["system_prompt"], COMMERCE_V2)
    brand = (
        "## Brand identity\n\n"
        "- **Brand:** Kosha\n"
        "- **Storefront:** `{shop_url}`\n"
        "- **Hours:** 10 am to 6 pm, Monday to Saturday\n\n"
        "### Verified website context\n\n"
        "Merino since 2019.\n\n"
        "Made in Ludhiana.\n\n"
        "### Delivery\n\n"
        "- Ships in 2-4 days\n"
        "  - Metros in 2\n\n"
    )
    return built.model_copy(
        update={"flow": {**built.flow, "system_prompt": brand + operating}}
    )


@pytest.fixture
def saved(monkeypatch) -> Dict[str, Any]:
    writes: Dict[str, Any] = {}

    async def update(template_id, flow, configurations, now) -> TemplateModel:
        # Only the prompt and settings reach the database.
        writes["template"] = TemplateModel(
            id=template_id,
            reseller_id="BB_ASSIST",
            merchant_id="kosha",
            name="kosha-assist",
            flow=flow,
            configurations=configurations,
        )
        return writes["template"]

    monkeypatch.setattr(service, "update_template_flow_and_configurations", update)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())
    return writes


async def test_a_hand_written_assistant_loses_nothing_on_save(saved) -> None:
    before = _hand_written()
    prompt = before.flow["system_prompt"]
    # Unchanged, a save writes the prompt back exactly.
    await service.save_fields(before, {})
    assert saved["template"].flow["system_prompt"] == prompt

    await service.save_fields(before, {"returns": ["Easy 7-day returns"]})
    after = saved["template"].flow["system_prompt"]
    for kept in (
        "- **Hours:** 10 am to 6 pm, Monday to Saturday",
        "### Verified website context\n\nMerino since 2019.\n\nMade in Ludhiana.",
        "- Ships in 2-4 days\n  - Metros in 2",  # sub-points: kept, not a field
    ):
        assert kept in after
    assert "### Returns and exchanges\n\n- Easy 7-day returns" in after
    # A quick reply's message is not on the form, so a save keeps it.
    replies = saved["template"].configurations.quick_replies
    assert replies[0].value == "Show me your bestsellers"


async def test_a_note_in_a_list_stays_when_the_list_is_edited(saved) -> None:
    # Beyond Bound's prompt: a rule for the model above the hero products.
    before = _hand_written()
    prompt = before.flow["system_prompt"].replace(
        "### Delivery",
        "### Hero products\n\n"
        "Names only — prices ALWAYS come from a tool call.\n\n"
        "- AeroShield Jacket\n- Zipper Sports Bra\n\n### Delivery",
    )
    before = before.model_copy(
        update={"flow": {**before.flow, "system_prompt": prompt}}
    )
    # The note is not shown as a product, so the merchant cannot delete it.
    fields = service.read_fields(before)
    assert fields is not None
    assert fields["hero_items"] == [
        "AeroShield Jacket",
        "Zipper Sports Bra",
    ]

    await service.save_fields(before, {"hero_items": ["AeroShield Jacket"]})
    after = saved["template"].flow["system_prompt"]
    assert "Names only — prices ALWAYS come from a tool call." in after
    assert "Zipper Sports Bra" not in after


async def test_an_edited_whatsapp_number_is_trusted(saved) -> None:
    await service.save_fields(_hand_written(), {"whatsapp": ["+91 98765 43210"]})
    trusted = saved["template"].configurations.render_ui.trusted_link_urls
    assert "https://wa.me/919876543210" in trusted


def test_a_value_too_long_is_refused_not_cut() -> None:
    with pytest.raises(ValueError, match="longer than"):
        apply_edits({}, {"returns": ["x" * 5000]}, verticals.DEFAULT.fields)
