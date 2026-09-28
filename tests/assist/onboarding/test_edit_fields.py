"""``/assist/agents/{id}/fields``: what the merchant edits, and what a save changes."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.commerce.skeleton import COMMERCE_V2
from app.ai.voice.agents.breeze_buddy.assist.engine import fields as engine_fields
from app.ai.voice.agents.breeze_buddy.assist.engine.prompt_core import split_prompt
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.assist import fields as fields_route
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding import AssistCreateRequest

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
STORE = verticals.DEFAULT.fields
MERCHANT = UserInfo(
    id="m-1",
    username="merchant",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_ASSIST"],
    merchant_ids=["kosha"],
)


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
    request = AssistCreateRequest(
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


@pytest.fixture
def saved(monkeypatch) -> Dict[str, Any]:
    """Records the save; the blueprint is there to restore a cleared section."""
    writes: Dict[str, Any] = {}

    async def update(template: TemplateModel) -> TemplateModel:
        writes["template"] = template
        return template

    async def template_in_scope(reseller_id, merchant_id, name):
        return _blueprint() if merchant_id is None else None

    monkeypatch.setattr(service, "_update_template", update)
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())
    return writes


def test_edits_keep_unsent_fields_clear_sent_empty_ones_and_refuse_hidden() -> None:
    current = {"brand_line": ["Kosha"], "email": ["a@kosha.example"]}
    edited = engine_fields.apply_edits(
        current, {"email": [], "offer_items": ["20% off thermals"]}, STORE
    )
    assert edited == {
        "brand_line": ["Kosha"],
        "email": [],
        "offer_items": ["20% off thermals"],
    }
    # One-line fields are made one line; a text field keeps its lines (a
    # two-line greeting), and no line can open a heading in the prompt.
    assert engine_fields.apply_edits({}, {"email": ["a\n b"]}, STORE) == {
        "email": ["a b"]
    }
    assert engine_fields.apply_edits(
        {}, {"initial_greeting": ["Cold where you are?\n### Ask us"]}, STORE
    ) == {"initial_greeting": ["Cold where you are?\nAsk us"]}
    with pytest.raises(ValueError):
        engine_fields.apply_edits(current, {"policies": ["x"]}, STORE)  # hidden
    with pytest.raises(ValueError):
        engine_fields.apply_edits(current, {"email": ["a@x", "b@x"]}, STORE)


async def test_a_save_rebuilds_only_the_merchants_parts(saved) -> None:
    before = _assistant(
        {
            "assistant_name": ["Kosha Assist"],
            "brand_line": ["Kosha"],
            "question": ["Which size?"],
            "question_answer": ["True to chest."],
        }
    )
    await service.save_assistant_fields(
        before,
        {
            "brand_line": ["Kosha: merino for Indian winters"],
            "initial_greeting": ["Cold where you are?"],
        },
    )
    after: TemplateModel = saved["template"]
    prompt = after.flow["system_prompt"]
    assert "- **Brand:** Kosha: merino for Indian winters" in prompt
    assert "### Which size?" in prompt  # unsent, so kept
    # The shared operating block is byte-for-byte what it was.
    assert (
        split_prompt(prompt, COMMERCE_V2)[1]
        == split_prompt(before.flow["system_prompt"], COMMERCE_V2)[1]
    )
    assert after.configurations is not None
    assert after.configurations.initial_greeting == "Cold where you are?"
    assert after.configurations.assist_fields is not None
    assert after.configurations.assist_fields["brand_line"] == [
        "Kosha: merino for Indian winters"
    ]


async def test_clearing_every_question_brings_the_blueprint_section_back(saved) -> None:
    before = _assistant(
        {
            "assistant_name": ["Kosha Assist"],
            "brand_line": ["Kosha"],
            "question": ["Which size?"],
            "question_answer": ["True to chest."],
        }
    )
    assert "### Sizing and selection help" not in before.flow["system_prompt"]
    await service.save_assistant_fields(before, {"question": [], "question_answer": []})
    prompt = saved["template"].flow["system_prompt"]
    assert "### Which size?" not in prompt
    assert "### Sizing and selection help" in prompt


async def test_an_assistant_without_fields_is_not_edited_as_fields(saved) -> None:
    older = _blueprint().model_copy(update={"merchant_id": "kosha"})
    with pytest.raises(service.AssistantNotEditableError):
        await service.save_assistant_fields(older, {"brand_line": ["x"]})
    assert saved == {}


def test_the_route_shows_the_form_and_guards_who_may_edit(monkeypatch) -> None:
    mine = _assistant({"assistant_name": ["Kosha Assist"], "brand_line": ["Kosha"]})
    theirs = mine.model_copy(update={"merchant_id": "someone-else"})
    blueprint = _blueprint()
    templates = {"mine": mine, "theirs": theirs, "blueprint": blueprint}

    async def by_id(template_id: str):
        return templates.get(template_id)

    monkeypatch.setattr(fields_route, "get_template_by_id", by_id)
    app = FastAPI()
    app.include_router(fields_route.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: MERCHANT
    client = TestClient(app)

    body = client.get("/assist/agents/mine/fields").json()
    assert body["editable"] is True
    shown = {f["key"]: f["values"] for s in body["sections"] for f in s["fields"]}
    assert "brand_line" in shown and "policies" not in shown  # hidden stays hidden
    # The greeting and chips shoppers see now, though the merchant set none.
    assert shown["initial_greeting"] == ["Hi! What are you looking for today?"]
    assert "What's popular?" in shown["quick_replies"]
    assert client.get("/assist/agents/theirs/fields").status_code == 403
    assert client.get("/assist/agents/blueprint/fields").status_code == 404
    bad = client.put("/assist/agents/mine/fields", json={"fields": {"policies": []}})
    assert bad.status_code == 400
