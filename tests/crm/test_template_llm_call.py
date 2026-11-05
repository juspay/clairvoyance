"""llm_call — the one async function in TEMPLATE_FUNCTION_REGISTRY.

Pipecat's OpenAILLMService and the dynamic-config key lookup are faked: these
pin OUR contract — luna on the fixed endpoint, the model from env, the
prompt as the system message and the value as the user message, the answer cleaned to one line, and every failure returning
the value unchanged.
"""

import asyncio
from typing import Any, Dict, Iterator, List, Optional

import pytest

import app.utils.transformation.utils as transformation
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY

RAW = "Accidental & Liquid Damage Protection, Blaupunkt 100 cm QLED Smart Goog"
SHORT = "Return only the main product's short name."
MERCHANT = "flipkart"


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
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    token = transformation._memo_merchant.set("")
    _FakeLLM.built, _FakeLLM.prompts = [], []
    _FakeLLM.answer, _FakeLLM.delay = "Blaupunkt Smart TV", 0.0
    CONFIG.clear()
    CONFIG["OPENAI_API_KEY"] = "dynamic-key"
    transformation._llm_call_cache.clear()
    monkeypatch.setattr(transformation, "_llm_call_service", None)
    monkeypatch.setattr(transformation, "OpenAILLMService", _FakeLLM)
    monkeypatch.setattr(transformation, "get_config", _fake_get_config)
    yield
    transformation._memo_merchant.reset(token)


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
    """Eight callers of one value at once make one model call."""
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
    """Cancelling one waiter must not cancel the call the others share."""
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


def test_a_cancelled_leader_still_answers_its_waiters() -> None:
    """Shutdown cancels the row that happens to be asking first."""
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
    assert waiter_result == "Blaupunkt Smart TV", "the waiter still gets the answer"
    assert len(_FakeLLM.prompts) == 1, "and the model was asked exactly once"


def test_two_different_keys_do_not_share_a_flight() -> None:
    """Two different values are two calls, never one shared answer."""
    _FakeLLM.delay = 0.02

    async def race() -> Any:
        return await asyncio.gather(
            TEMPLATE_FUNCTION_REGISTRY["llm_call"]("vivo S2 5G (Black)", prompt=SHORT),
            TEMPLATE_FUNCTION_REGISTRY["llm_call"]("SAMSUNG Galaxy S24", prompt=SHORT),
        )

    asyncio.run(race())
    assert len(_FakeLLM.prompts) == 2, "different values are different flights"


# --- the key ---------------------------------------------------------------


def test_the_key_names_the_model_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored answer belongs to the model that produced it."""
    before = transformation._cache_key(SHORT, RAW, MERCHANT)
    monkeypatch.setattr(transformation, "LLM_CALL_MODEL", "in.openai.other-model")
    after = transformation._cache_key(SHORT, RAW, MERCHANT)
    assert before != after


def test_the_key_is_the_same_in_another_process() -> None:
    """The key is the same in every process (sha256, never hash())."""
    import subprocess
    import sys

    snippet = (
        "import app.utils.transformation.utils as t;"
        "print(t._cache_key('p', 'Blaupunkt 100 cm QLED', 'flipkart'))"
    )
    keys = []
    for seed in ("0", "1"):
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            check=True,
        )
        keys.append(out.stdout.strip())
    assert keys[0] == keys[1], "the key is not stable across processes"


# --- the shared store (L2): one answer, every pod, across restarts ----------


class _FakeRedis:
    """Records what the memo asked the store, and can refuse like a real one."""

    def __init__(self, store: Optional[Dict[str, str]] = None, broken: bool = False):
        self.store: Dict[str, str] = store or {}
        self.broken = broken
        self.gets: List[str] = []
        self.sets: List[Any] = []

    async def get(self, key: str) -> Optional[str]:
        if self.broken:
            raise ConnectionError("store unreachable")
        self.gets.append(key)
        return self.store.get(key)

    async def setex(
        self, key: str, value: str, ttl_seconds: Optional[int] = None
    ) -> bool:
        """Same argument order as RedisService.setex: (key, value, ttl)."""
        if self.broken:
            raise ConnectionError("store unreachable")
        self.sets.append((key, value, ttl_seconds))
        self.store[key] = value
        return True


def _with_store(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> _FakeRedis:
    async def _get_service() -> _FakeRedis:
        return redis

    monkeypatch.setattr(transformation, "get_redis_service", _get_service)
    monkeypatch.setattr(transformation, "_l2_last_warning", 0.0)
    # The store is off by default: switch it on, and say whose call this is
    # as blocks_for does (the fixture resets it).
    monkeypatch.setattr(transformation, "LLM_CALL_L2_ENABLED", True)
    transformation._memo_merchant.set(MERCHANT)
    return redis


def test_an_answer_is_remembered_in_the_shared_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _with_store(monkeypatch, _FakeRedis())
    assert _run() == "Blaupunkt Smart TV"
    assert len(redis.sets) == 1
    key, value, ttl = redis.sets[0]
    assert key.startswith("tfx:")
    assert value == "Blaupunkt Smart TV", "the ANSWER must be the value argument"
    assert (
        ttl == transformation.LLM_CALL_L2_TTL_SECONDS
    ), "the TTL must be the ttl argument"


def test_the_store_answers_a_pod_that_never_asked_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a release buys: L1 dies with the process, the store does not."""
    _with_store(monkeypatch, _FakeRedis())
    assert _run() == "Blaupunkt Smart TV"
    # a fresh pod: same store, empty L1, and a model that would answer
    # differently if it were asked at all
    transformation._llm_call_cache.clear()
    _FakeLLM.answer = "SHOULD NOT BE ASKED"
    before = len(_FakeLLM.prompts)
    assert _run() == "Blaupunkt Smart TV"
    assert (
        len(_FakeLLM.prompts) == before
    ), "the model was asked despite a stored answer"


def test_a_stored_answer_fills_the_in_process_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store hit must not be paid twice: the second ask stays in process."""
    redis = _with_store(monkeypatch, _FakeRedis())
    _run()
    transformation._llm_call_cache.clear()
    redis.gets.clear()
    _run()  # reads the store, fills L1
    _run()  # must NOT read the store again
    assert len(redis.gets) == 1


def test_an_unreachable_store_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail OPEN: a store that is down costs a round trip, never the line."""
    _with_store(monkeypatch, _FakeRedis(broken=True))
    assert _run() == "Blaupunkt Smart TV"
    transformation._llm_call_cache.clear()
    assert _run() == "Blaupunkt Smart TV"


def test_a_failed_answer_is_not_remembered_in_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed answer is never written to the store."""
    redis = _with_store(monkeypatch, _FakeRedis())
    _FakeLLM.answer = RuntimeError("quota exceeded")
    assert _run() == RAW
    assert redis.sets == []


def test_one_batch_wanting_one_value_reads_the_store_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One batch wanting one value reads the store once."""
    redis = _with_store(monkeypatch, _FakeRedis())
    _FakeLLM.delay = 0.02

    async def go() -> Any:
        return await asyncio.gather(
            *(
                TEMPLATE_FUNCTION_REGISTRY["llm_call"](RAW, prompt=SHORT)
                for _ in range(20)
            )
        )

    answers = asyncio.run(go())
    assert answers == ["Blaupunkt Smart TV"] * 20
    assert len(redis.gets) == 1
    assert len(_FakeLLM.prompts) == 1


def test_the_store_switched_off_makes_no_redis_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_CALL_L2_ENABLED=false: the in-process memo alone, no read, no write."""
    redis = _with_store(monkeypatch, _FakeRedis())
    monkeypatch.setattr(transformation, "LLM_CALL_L2_ENABLED", False)
    assert _run() == "Blaupunkt Smart TV"
    assert _run() == "Blaupunkt Smart TV"
    assert redis.gets == [] and redis.sets == []
    assert len(_FakeLLM.prompts) == 1  # the in-process memo still answers


def test_a_call_that_raised_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise is not an answer: the next caller asks again."""
    calls: List[int] = []

    async def flaky(value: str, prompt: str) -> Optional[str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("client build failed")
        return "fixed"

    monkeypatch.setattr(transformation, "_llm_call_rewrite", flaky)
    with pytest.raises(RuntimeError):
        _run()
    assert _run() == "fixed"
    assert len(calls) == 2


# --- per merchant, and the guards in front of the store ---------------------


def test_two_merchants_asking_the_same_question_do_not_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two merchants with the same prompt and value get separate entries."""
    redis = _with_store(monkeypatch, _FakeRedis())
    assert _run() == "Blaupunkt Smart TV"
    with transformation.memo_merchant("meesho"):
        assert _run() == "Blaupunkt Smart TV"
    assert len(_FakeLLM.prompts) == 2, "one merchant was served the other's answer"
    stored = sorted(key for key, _, _ in redis.sets)
    assert len(stored) == 2
    assert ":flipkart:llm_call:" in stored[0] and ":meesho:llm_call:" in stored[1]


def test_an_empty_prompt_asks_nothing_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _with_store(monkeypatch, _FakeRedis())
    assert _run(prompt="") == RAW
    assert redis.gets == [] and _FakeLLM.prompts == []
    assert len(transformation._llm_call_cache) == 0, "nothing to ask, nothing held"


def test_an_oversized_stored_answer_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored answer passes the same length check as a model answer."""
    key = transformation._cache_key(SHORT, RAW, MERCHANT)
    too_long = "x" * (transformation.LLM_CALL_MAX_CHARS + 1)
    _with_store(monkeypatch, _FakeRedis({f"tfx:{key}": too_long}))
    assert _run() == "Blaupunkt Smart TV"
    assert len(_FakeLLM.prompts) == 1


def test_the_memo_holds_at_most_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two slots, three values: the oldest is the one let go."""
    monkeypatch.setattr(transformation, "LLM_CALL_CACHE_SIZE", 2)
    for value in ("one", "two", "three"):
        _run(value)
    assert len(transformation._llm_call_cache) == 2
    assert (
        transformation._cache_key(SHORT, "one", "")
        not in transformation._llm_call_cache
    )
