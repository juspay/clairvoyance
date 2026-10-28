from types import SimpleNamespace

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.anthropic.llm import AnthropicLLMService

from app.ai.voice.agents.breeze_buddy.observers.llm import call_llm


async def test_observer_call_parses_tool_use(monkeypatch):
    svc = AnthropicLLMService(
        api_key="test",
        settings=AnthropicLLMService.Settings(model="claude-sonnet-4-5"),
    )
    sent: dict = {}

    async def create(**params):
        sent.update(params)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name="end_call", input={})]
        )

    monkeypatch.setattr(svc._client.beta.messages, "create", create)
    tools = [
        FunctionSchema(name="end_call", description="d", properties={}, required=[])
    ]

    result = await call_llm(
        svc, "leave a message after the beep", "Detect voicemail.", tools, "vm"
    )

    assert result == ("end_call", {})
    assert sent["model"] == "claude-sonnet-4-5"
    assert sent["tool_choice"] == {"type": "auto"}
    assert [tool["name"] for tool in sent["tools"]] == ["end_call"]
