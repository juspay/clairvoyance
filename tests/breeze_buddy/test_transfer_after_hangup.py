"""The caller hangs up while the LLM's transfer_to_agent call is in flight.

end_conversation has already finalized the call (conversation_ended=True), so
the provider no longer has it: Plivo answers the transfer with "call not
found", logged as an ERROR. The transfer is skipped instead, and the
pre-transfer message is not spoken into a call that is gone (that wait only
timed out after 15 s).
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from loguru import logger

from app.ai.voice.agents.breeze_buddy.handlers.internal import (
    builtin_dispatcher as dispatcher_mod,
    warm_transfer as transfer_mod,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import GlobalBuiltinFunction

TRANSFER = GlobalBuiltinFunction(
    name="transfer_to_agent",
    description="Transfer the call to a human agent",
    handler="connect_to_live_agent",
    pre_tts_message="Connecting you to our support team.",
)


@pytest.fixture
def logs():
    records: List[Dict[str, Any]] = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink)


def messages(records: List[Dict[str, Any]], level: str) -> List[str]:
    return [r["message"] for r in records if r["level"].name == level]


def make_context(ended: bool) -> TemplateContext:
    lead = SimpleNamespace(telephony_number_id="num-1", metaData=None)
    return TemplateContext(
        SimpleNamespace(conversation_ended=ended, call_sid="call-1", lead=lead)
    )


@pytest.fixture
def number_lookups(monkeypatch) -> List[str]:
    """The first step of a real transfer; returning None stops it right there."""
    calls: List[str] = []

    async def get_telephony_number_by_id(number_id: str):
        calls.append(number_id)
        return None

    monkeypatch.setattr(
        transfer_mod, "get_telephony_number_by_id", get_telephony_number_by_id
    )
    return calls


@pytest.fixture
def spoken(monkeypatch) -> List[str]:
    said: List[str] = []

    async def speak_and_wait(context: TemplateContext, message: str) -> None:
        said.append(message)

    monkeypatch.setattr(dispatcher_mod, "_speak_and_wait", speak_and_wait)
    return said


async def test_transfer_after_hangup_is_skipped(logs, number_lookups):
    result = await transfer_mod.connect_to_live_agent(make_context(ended=True), {})

    assert result["reason"] == "call_ended"
    assert number_lookups == []  # never reached the provider
    assert any("caller already hung up" in m for m in messages(logs, "WARNING"))
    assert messages(logs, "ERROR") == []


async def test_transfer_on_a_live_call_still_runs(number_lookups):
    result = await transfer_mod.connect_to_live_agent(make_context(ended=False), {})

    assert number_lookups == ["num-1"]
    assert result["reason"] == "telephony_number_not_found"


async def test_dispatcher_does_not_speak_into_an_ended_call(spoken, number_lookups):
    result, _ = await dispatcher_mod.builtin_function_dispatcher(
        make_context(ended=True), {}, TRANSFER
    )

    assert spoken == []
    assert result["reason"] == "call_ended"


async def test_dispatcher_speaks_before_a_live_transfer(spoken, number_lookups):
    await dispatcher_mod.builtin_function_dispatcher(
        make_context(ended=False), {}, TRANSFER
    )

    assert spoken == ["Connecting you to our support team."]
    assert number_lookups == ["num-1"]
