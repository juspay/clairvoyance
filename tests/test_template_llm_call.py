"""llm_call — the one async function in TEMPLATE_FUNCTION_REGISTRY.

Pipecat's OpenAILLMService and the dynamic-config key lookup are faked: these
pin OUR contract — luna on the fixed endpoint, the model from env, the
prompt as the system message and the value as the user message, the answer cleaned to one line, and every failure returning
the value unchanged.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import app.utils.transformation.utils as transformation
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY

RAW = "Accidental & Liquid Damage Protection, Blaupunkt 100 cm QLED Smart Goog"
SHORT = "Return only the main product's short name."


class _FakeLLM:
    """Stands in for OpenAILLMService: records how it was built and asked."""

    built: List[Dict[str, Any]] = []
    prompts: List[Any] = []
    answer: Any = "Blaupunkt Smart TV"
    delay: float = 0.0

    class Settings:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    def __init__(self, api_key: str, base_url: str, model: str, settings: Any) -> None:
        self.supports_developer_role = True
        self.record = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "settings": settings.kwargs,
        }
        _FakeLLM.built.append(self.record)

    async def run_inference(self, context: Any) -> Optional[str]:
        self.record["developer_role"] = self.supports_developer_role
        _FakeLLM.prompts.append(context.get_messages())
        if _FakeLLM.delay:
            await asyncio.sleep(_FakeLLM.delay)
        if isinstance(_FakeLLM.answer, Exception):
            raise _FakeLLM.answer
        return _FakeLLM.answer


CONFIG: Dict[str, Any] = {}


async def _fake_get_config(key: str, default: Any, return_type: type = str) -> Any:
    return CONFIG.get(key, default)


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLLM.built, _FakeLLM.prompts = [], []
    _FakeLLM.answer, _FakeLLM.delay = "Blaupunkt Smart TV", 0.0
    CONFIG.clear()
    CONFIG["OPENAI_API_KEY"] = "dynamic-key"
    transformation._llm_call_cache.clear()
    monkeypatch.setattr(transformation, "_llm_call_service", None)
    monkeypatch.setattr(transformation, "OpenAILLMService", _FakeLLM)
    monkeypatch.setattr(transformation, "get_config", _fake_get_config)


def _run(value: Any = RAW, prompt: str = SHORT) -> Any:
    return asyncio.run(TEMPLATE_FUNCTION_REGISTRY["llm_call"](value, prompt=prompt))


def test_one_user_message_to_luna_with_fixed_settings() -> None:
    assert _run() == "Blaupunkt Smart TV"
    assert _FakeLLM.built == [
        {
            "api_key": "dynamic-key",  # OPENAI_API_KEY from dynamic config
            "base_url": transformation.LLM_CALL_ENDPOINT,
            "model": "in.openai.gpt-5.6-luna",
            "settings": {
                "temperature": 0,
                "max_completion_tokens": 200,
                "extra": {
                    "reasoning_effort": "none",
                    "extra_body": {"prompt_cache_key": "crm-playbook-llm-call"},
                },
            },
            "developer_role": False,
        }
    ]
    # The author's prompt as the system message, the value alone as the user's.
    assert _FakeLLM.prompts == [
        [{"role": "system", "content": SHORT}, {"role": "user", "content": RAW}]
    ]


def test_no_key_keeps_the_value() -> None:
    CONFIG.clear()
    assert _run() == RAW
    assert _FakeLLM.built == []


def test_quotes_and_line_breaks_are_cleaned() -> None:
    _FakeLLM.answer = '  "Blaupunkt\nSmart TV"  '
    assert _run() == "Blaupunkt Smart TV"


@pytest.mark.parametrize(
    "answer", [None, "", "   ", "x" * (transformation.LLM_CALL_MAX_CHARS + 1)]
)
def test_an_empty_or_oversized_answer_keeps_the_value(answer: Any) -> None:
    _FakeLLM.answer = answer
    assert _run() == RAW


def test_an_error_keeps_the_value() -> None:
    _FakeLLM.answer = RuntimeError("quota exceeded")
    assert _run() == RAW


def test_a_slow_model_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transformation, "LLM_CALL_TIMEOUT_SECS", 0.01)
    _FakeLLM.delay = 1.0
    assert _run() == RAW


@pytest.mark.parametrize("value, prompt", [(RAW, ""), ("", SHORT), (None, SHORT)])
def test_nothing_to_rewrite_means_no_model_call(value: Any, prompt: str) -> None:
    assert _run(value, prompt) == value
    assert _FakeLLM.built == []


def test_the_same_prompt_and_value_is_one_model_call() -> None:
    assert _run() == _run() == "Blaupunkt Smart TV"
    assert len(_FakeLLM.prompts) == 1


def test_a_failed_answer_is_not_cached() -> None:
    _FakeLLM.answer = RuntimeError("quota exceeded")
    assert _run() == RAW
    _FakeLLM.answer = "Blaupunkt Smart TV"
    assert _run() == "Blaupunkt Smart TV"


def test_one_service_serves_every_call_on_a_loop() -> None:
    async def three() -> None:
        llm_call = TEMPLATE_FUNCTION_REGISTRY["llm_call"]
        await llm_call("a", prompt=SHORT)
        await llm_call("b", prompt=SHORT)
        await llm_call("c", prompt=SHORT)

    asyncio.run(three())
    assert len(_FakeLLM.built) == 1  # one HTTP client, not three
    assert len(_FakeLLM.prompts) == 3


def test_concurrent_callers_of_one_key_share_a_single_model_call() -> None:
    """The cache is written only AFTER two awaits, so N visits wanting the same
    (prompt, value) inside one walker batch each miss and each call the model —
    and the values that collide inside a batch are the popular ones (a top
    brand, a common sub-category). Without single-flight the concurrency dial
    fights the cache instead of compounding with it.
    """
    _FakeLLM.delay = 0.05  # long enough that all eight are in flight together

    async def race() -> Any:
        return await asyncio.gather(
            *(
                TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
                for _ in range(8)
            )
        )

    answers = asyncio.run(race())

    assert answers == ["Blaupunkt Smart TV"] * 8, "every caller gets the answer"
    assert len(_FakeLLM.prompts) == 1, "eight concurrent callers, ONE model call"


def test_a_cancelled_waiter_does_not_cancel_the_shared_call() -> None:
    """A waiter awaits the leader's future directly, so cancelling ONE waiter
    cancels the future every other waiter is holding: they get a
    CancelledError none of them asked for, while the leader's answer arrives
    fine. Shutdown cancels rows mid-batch, so this is the concurrency dial's
    own failure mode."""
    _FakeLLM.delay = 0.05

    async def race() -> Any:
        leader = asyncio.create_task(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
        )
        await asyncio.sleep(0)  # let the leader register its pending future
        doomed = asyncio.create_task(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
        )
        survivor = asyncio.create_task(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
        )
        await asyncio.sleep(0.01)  # both waiters are now parked on the future
        doomed.cancel()
        return await asyncio.gather(leader, survivor, return_exceptions=True)

    leader_answer, survivor_answer = asyncio.run(race())

    assert leader_answer == "Blaupunkt Smart TV"
    assert (
        survivor_answer == "Blaupunkt Smart TV"
    ), "one cancel must not poison siblings"
    assert len(_FakeLLM.prompts) == 1


def test_a_cancelled_leader_leaves_its_waiters_the_raw_value() -> None:
    """Shutdown cancels the row that happens to be asking. Its waiters must
    not hang on a future nobody will resolve: they fall open to the value
    unchanged, which is the same answer a timed-out or failed call gives."""
    _FakeLLM.delay = 0.05

    async def race() -> Any:
        leader = asyncio.create_task(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
        )
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
        )
        await asyncio.sleep(0.01)
        leader.cancel()
        return await asyncio.gather(leader, waiter, return_exceptions=True)

    leader_result, waiter_result = asyncio.run(race())

    assert isinstance(
        leader_result, asyncio.CancelledError
    ), "the leader's cancel stands"
    assert waiter_result == RAW, "the waiter falls open rather than hanging"


def test_two_different_keys_do_not_share_a_flight() -> None:
    """Single-flight is per (prompt, value): two different products in one
    batch are two questions, and must not be answered with each other's line."""
    _FakeLLM.delay = 0.02

    async def race() -> Any:
        return await asyncio.gather(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"]("vivo S2 5G (Black)", prompt=SHORT),
            TEMPLATE_FUNCTION_REGISTRY["llm_call"]("SAMSUNG Galaxy S24", prompt=SHORT),
        )

    asyncio.run(race())
    assert len(_FakeLLM.prompts) == 2, "different values are different flights"
