"""Tests for handing the widget voice bot its template in the launch payload.

``start_daily_session(preload_template=True)`` runs ``_preload_template`` on
the API process and puts the template + vars in the bot's launch payload;
the bot's ``resolve_template_config`` uses them, else reads the database.
"""

from __future__ import annotations

import json
from typing import Any, Dict, cast

import pytest

from app.ai.voice.agents.breeze_buddy.agent import flow as flow_mod
from app.ai.voice.agents.breeze_buddy.services.daily import daily as daily_mod
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.schemas import LeadCallTracker

_VARS: Dict[str, Any] = {"shop_url": "shop.example", "Token": "cred-123"}
_LEAD = cast(LeadCallTracker, object())


def _template() -> TemplateModel:
    return TemplateModel(
        id="00000000-0000-0000-0000-0000000000a1",
        reseller_id="BB_SHOPIFY",
        merchant_id=None,
        name="preload-test",
        flow={"mode": "direct", "functions": [], "system_prompt": "hi"},
        expected_payload_schema={},
        expected_callback_response_schema={},
        configurations=ConfigurationModel.model_validate(
            {
                "mcp": {
                    "servers": [
                        {
                            "name": "store",
                            "url": "https://mcp.example/mcp",
                            "auth": {"type": "bearer", "token": "real-token"},
                        }
                    ]
                }
            }
        ),
    )


@pytest.fixture
def preload_sources(monkeypatch: pytest.MonkeyPatch) -> TemplateModel:
    template = _template()

    async def _lead(lead_id: str) -> object:
        return object()

    async def _load(lead: object) -> tuple:
        return template, template.configurations, dict(_VARS)

    monkeypatch.setattr(daily_mod, "get_lead_by_id", _lead)
    monkeypatch.setattr(daily_mod, "load_template_config", _load)
    return template


async def _over_the_wire(preload: Dict[str, Any]) -> Dict[str, Any]:
    # The launch payload reaches the bot as JSON (stdin / zygote socket).
    return json.loads(json.dumps({"lead_id": "lead-1", **preload}))


@pytest.mark.asyncio
async def test_preloaded_template_keeps_auth_secrets(
    preload_sources: TemplateModel,
) -> None:
    body = await _over_the_wire(await daily_mod._preload_template("lead-1"))

    template, configurations, template_vars = await flow_mod.resolve_template_config(
        _LEAD, body
    )

    assert configurations is not None and configurations.mcp is not None
    auth = configurations.mcp.servers[0].auth
    assert auth is not None and auth.token is not None
    assert auth.token.get_secret_value() == "real-token"
    assert template.flow == preload_sources.flow
    assert template_vars == _VARS


@pytest.mark.asyncio
async def test_bot_reads_database_without_preload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = (_template(), None, dict(_VARS))
    calls = []

    async def _load(lead: object) -> tuple:
        calls.append(lead)
        return loaded

    monkeypatch.setattr(flow_mod, "load_template_config", _load)
    lead = _LEAD

    result = await flow_mod.resolve_template_config(lead, {"lead_id": "lead-1"})

    assert result is loaded
    assert calls == [lead]


@pytest.mark.asyncio
async def test_failed_preload_falls_back_to_bot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lead(lead_id: str) -> object:
        return object()

    async def _load(lead: object) -> tuple:
        raise ValueError("No template found")

    monkeypatch.setattr(daily_mod, "get_lead_by_id", _lead)
    monkeypatch.setattr(daily_mod, "load_template_config", _load)

    assert await daily_mod._preload_template("lead-1") == {}


@pytest.mark.asyncio
async def test_missing_lead_sends_no_preload(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lead(lead_id: str) -> None:
        return None

    monkeypatch.setattr(daily_mod, "get_lead_by_id", _lead)

    assert await daily_mod._preload_template("lead-1") == {}
