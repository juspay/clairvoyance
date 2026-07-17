from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    ServiceCallbackConfig,
)


async def test_apply_transfer_uses_target_template_configuration():
    from app.ai.voice.agents.breeze_buddy.agent.transfer import apply_transfer

    source_config = ConfigurationModel(
        service_callback=ServiceCallbackConfig(url="https://example.com/source")
    )
    target_config = ConfigurationModel()
    original_lead = SimpleNamespace(id="lead-1", metaData={})
    updated_lead = SimpleNamespace(id="lead-1", metaData=None)
    target_template = SimpleNamespace(
        id="target-template-id",
        name="target-template",
        configurations=target_config,
    )
    bot = SimpleNamespace(
        transfer_count=0,
        generation=0,
        template=SimpleNamespace(id="source-template-id", name="source-template"),
        configurations=source_config,
        template_vars={},
        _handoff_messages=[],
        context=None,
        prior_generation_messages=[],
        lead=original_lead,
        is_daily_mode=False,
        _rebuild=SimpleNamespace(
            ws_proxy=object(),
            telephony_transport_type="telephony",
            telephony_call_data={},
        ),
        task=None,
        flow_manager=None,
        _context_aggregator=None,
        _rtvi_processor=None,
        approval_manager=None,
        _user_idle_callback_handler=None,
        _flow_initialized=False,
        greeting_source=None,
        greeting_text=None,
        _post_greeting_task=None,
    )
    transfer = SimpleNamespace(
        template=target_template,
        template_vars={"customer_name": "Ada"},
        handoff_messages=[{"role": "system", "content": "handoff"}],
    )
    transfer_context = SimpleNamespace(
        record_node_exit=lambda: None,
        _get_ist_timestamp=lambda: "2026-07-24T00:00:00+05:30",
    )

    async def fake_update(**kwargs):
        assert kwargs == {
            "lead_id": "lead-1",
            "template": "target-template",
            "template_id": "target-template-id",
        }
        return updated_lead

    async def fake_vad(**kwargs):
        return "vad", "vad-params"

    async def fake_transport(*args):
        return "transport"

    with (
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer.TemplateContext",
            return_value=transfer_context,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer.update_lead_template",
            new=fake_update,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer.FlowConfigBuilder",
            return_value=SimpleNamespace(handler_map={}),
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer.create_vad_analyzer",
            new=fake_vad,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer.get_transport_params",
            return_value={"telephony": lambda: "params"},
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.agent.transfer._create_telephony_transport",
            new=fake_transport,
        ),
        patch("app.ai.voice.agents.breeze_buddy.agent.transfer.update_log_context"),
    ):
        await apply_transfer(cast(Any, bot), cast(Any, transfer))

    assert bot.template is target_template
    assert bot.configurations is target_config
    assert bot.configurations.service_callback is None
    assert bot.lead is updated_lead
    assert bot.lead.metaData["agent_transfers"][0]["to_template"] == "target-template"
