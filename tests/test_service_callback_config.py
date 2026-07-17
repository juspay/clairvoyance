from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    ServiceCallbackConfig,
)
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest


def test_service_callback_config_defaults():
    cfg = ServiceCallbackConfig(url="https://example.com/cb")
    assert cfg.max_attempts == 3


def test_service_callback_config_custom_attempts():
    cfg = ServiceCallbackConfig(url="https://example.com/cb", max_attempts=1)
    assert cfg.max_attempts == 1


def test_service_callback_config_rejects_zero_attempts():
    bad_attempts: int = 0
    with pytest.raises(Exception):
        ServiceCallbackConfig(url="https://example.com/cb", max_attempts=bad_attempts)


def test_configuration_model_accepts_service_callback():
    model = ConfigurationModel(
        service_callback=ServiceCallbackConfig(
            url="https://example.com/cb", max_attempts=5
        )
    )
    assert model.service_callback is not None
    assert model.service_callback.url == "https://example.com/cb"
    assert model.service_callback.max_attempts == 5


def test_configuration_model_service_callback_optional():
    model = ConfigurationModel()
    assert model.service_callback is None


def test_push_lead_request_has_no_reporting_webhook_url():
    properties = PushLeadRequest.model_json_schema().get("properties", {})
    assert "reporting_webhook_url" not in properties


async def test_service_callback_uses_template_url():
    from app.ai.voice.agents.breeze_buddy.callbacks.service_callback import (
        service_callback,
    )

    sent: dict = {}

    async def fake_send(session, url, data, max_retries=3):
        sent["url"] = url
        sent["max_retries"] = max_retries
        return True

    configurations = ConfigurationModel(
        service_callback=ServiceCallbackConfig(
            url="https://example.com/hook", max_attempts=2
        )
    )
    context = MagicMock()
    context.configurations = configurations
    context.lead = SimpleNamespace(
        metaData={},
        outcome="SUCCESS",
        call_initiated_time=None,
        attempt_count=0,
        request_id="req-1",
        payload={},
    )
    context.call_sid = "CA123"
    context.aiohttp_session = object()
    context.expected_callback_response_schema = None

    with patch(
        "app.ai.voice.agents.breeze_buddy.callbacks.service_callback.send_webhook_with_retry",
        new=fake_send,
    ):
        await service_callback(context, {})

    assert sent["url"] == "https://example.com/hook"
    assert sent["max_retries"] == 2


async def test_service_callback_prefers_lead_payload_webhook_url():
    """Lead payload's reporting_webhook_url should win over template config."""
    from app.ai.voice.agents.breeze_buddy.callbacks.service_callback import (
        service_callback,
    )

    sent: dict = {}

    async def fake_send(session, url, data, max_retries=3):
        sent["url"] = url
        sent["max_retries"] = max_retries
        return True

    configurations = ConfigurationModel(
        service_callback=ServiceCallbackConfig(
            url="https://example.com/hook", max_attempts=2
        )
    )
    context = MagicMock()
    context.configurations = configurations
    context.lead = SimpleNamespace(
        metaData={},
        outcome="SUCCESS",
        call_initiated_time=None,
        attempt_count=0,
        request_id="req-1",
        payload={"reporting_webhook_url": "https://example.com/legacy"},
    )
    context.call_sid = "CA123"
    context.aiohttp_session = object()
    context.expected_callback_response_schema = None

    with patch(
        "app.ai.voice.agents.breeze_buddy.callbacks.service_callback.send_webhook_with_retry",
        new=fake_send,
    ):
        await service_callback(context, {})

    assert sent["url"] == "https://example.com/legacy"


async def test_service_callback_skips_when_no_config():
    from app.ai.voice.agents.breeze_buddy.callbacks.service_callback import (
        service_callback,
    )

    called = []

    async def fake_send(session, url, data, max_retries=3):
        called.append(url)
        return True

    context = MagicMock()
    context.configurations = ConfigurationModel()
    context.lead = SimpleNamespace(
        metaData={},
        outcome="SUCCESS",
        call_initiated_time=None,
        attempt_count=0,
        request_id="req-2",
        payload={},
    )
    context.call_sid = "CA456"
    context.aiohttp_session = object()
    context.expected_callback_response_schema = None

    with patch(
        "app.ai.voice.agents.breeze_buddy.callbacks.service_callback.send_webhook_with_retry",
        new=fake_send,
    ):
        await service_callback(context, {})

    assert called == []


async def test_precheck_failure_uses_template_service_callback():
    from app.ai.voice.agents.breeze_buddy.managers.calls import _run_pre_checks_for_lead
    from app.ai.voice.agents.breeze_buddy.managers.pre_checks import (
        PreCheckResult,
        SinglePreCheckResult,
    )

    sent: dict = {}

    async def fake_send(session, url, data, max_retries=3):
        sent["url"] = url
        sent["max_retries"] = max_retries
        return True

    async def fake_update(**kwargs):
        return None

    async def fake_pre_checks(**kwargs):
        return PreCheckResult(
            should_proceed=False,
            results=[SinglePreCheckResult("eligibility", False, "rejected")],
        )

    template = SimpleNamespace(
        configurations=ConfigurationModel(
            service_callback=ServiceCallbackConfig(
                url="https://example.com/precheck", max_attempts=2
            )
        )
    )
    lead = SimpleNamespace(
        id="lead-1",
        attempt_count=0,
        request_id="request-1",
        payload={},
    )
    config = SimpleNamespace(pre_checks=[object()])

    with (
        patch(
            "app.ai.voice.agents.breeze_buddy.managers.calls.run_pre_checks",
            new=fake_pre_checks,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.managers.calls.update_lead_call_completion_details",
            new=fake_update,
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.managers.calls.send_webhook_with_retry",
            new=fake_send,
        ),
    ):
        assert not await _run_pre_checks_for_lead(
            cast(Any, config), cast(Any, lead), cast(Any, template), object()
        )

    assert sent == {"url": "https://example.com/precheck", "max_retries": 2}


async def test_retry_call_uses_template_service_callback():
    from app.ai.voice.agents.breeze_buddy.managers.calls import _retry_call

    sent: dict = {}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    async def fake_send(session, url, data, max_retries=3):
        sent["url"] = url
        sent["max_retries"] = max_retries
        return True

    template = SimpleNamespace(
        configurations=ConfigurationModel(
            service_callback=ServiceCallbackConfig(
                url="https://example.com/no-answer", max_attempts=4
            )
        )
    )
    lead = SimpleNamespace(
        attempt_count=0,
        call_initiated_time=None,
        call_id="call-1",
        request_id="request-1",
        payload={},
    )
    config = SimpleNamespace(max_retry=1)

    with (
        patch(
            "app.ai.voice.agents.breeze_buddy.managers.calls.create_aiohttp_session",
            return_value=Session(),
        ),
        patch(
            "app.ai.voice.agents.breeze_buddy.managers.calls.send_webhook_with_retry",
            new=fake_send,
        ),
    ):
        await _retry_call(
            cast(Any, lead), cast(Any, config), "NO_ANSWER", cast(Any, template)
        )

    assert sent == {"url": "https://example.com/no-answer", "max_retries": 4}
