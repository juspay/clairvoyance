"""One job per finished conversation runs its custom evals (voice only), then
its topics. A call's job is queued once the end-of-call outcome check is done
(crm_mirror's finished tap, phone or web). What passed
is marked on the job, so its retries, at the head of the queue, run only
what failed; FAILED after the last delivery. Custom evals are batched one
judge request per engine and model."""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy import crm_mirror
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis import (
    queue,
    worker,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.custom import (
    agent_evals,
)
from app.database.queries.breeze_buddy.evaluation_config import OUTCOME_CORRECTNESS
from app.schemas import (
    CallDirection,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.evals.custom import batch
from app.services.evals.engines.common import ChoiceResult, NoulResult, Verdict
from app.services.model_provider import ProviderError

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"
TOPIC_ROW = {"id": "topic-id", "evaluation_type": "TOPIC", "name": "topic"}


def _row(name: str, model: str = "jev-1.13.0", engine: str = "structured") -> Dict:
    return {
        "id": f"{name}-id",
        "evaluation_type": "CONVERSATION_EVALS",
        "name": name,
        "configuration": json.dumps(
            {
                "engine": engine,
                "provider": "typesafe",
                "model": model,
                "questions": [
                    {
                        "type": "noul",
                        "key": "asked",
                        "label": "Asked",
                        "instructions": "Did the agent ask?",
                    }
                ],
            }
        ),
    }


def _job(
    deliveries: int = 0,
    topics_done: bool = False,
    channel: ConversationChannel = ConversationChannel.VOICE,
    evals_done: Any = (),
) -> ConversationEvaluationJob:
    return ConversationEvaluationJob(
        source_id="lead-1",
        channel=channel,
        template_id=TEMPLATE_ID,  # type: ignore[arg-type]
        deliveries=deliveries,
        topics_done=topics_done,
        evals_done=list(evals_done),
    )


def _context() -> Dict[str, Any]:
    return {
        "source_id": "lead-1",
        "reseller_id": "r",
        "merchant_id": "m",
        "template_id": TEMPLATE_ID,
        "started_at": datetime(2026, 10, 8, tzinfo=timezone.utc),
        "transcript": [{"role": "user", "content": "hi"}],
    }


# --- the job -------------------------------------------------------------------


def test_a_job_queued_before_this_change_runs_its_topics():
    raw = json.dumps(
        {"source_id": "lead-1", "channel": "VOICE", "template_id": TEMPLATE_ID}
    )
    assert ConversationEvaluationJob.model_validate_json(raw).topics_done is False


# --- queueing --------------------------------------------------------------------


@pytest.fixture
def redis(monkeypatch):
    client = SimpleNamespace(rpush=AsyncMock(return_value=1))
    service = SimpleNamespace(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(queue, "get_redis_service", AsyncMock(return_value=service))
    monkeypatch.setattr(queue, "has_enabled_evaluations", AsyncMock(return_value=True))
    return SimpleNamespace(client=client, service=service)


def _pushed(redis) -> List[ConversationEvaluationJob]:
    return [
        ConversationEvaluationJob.model_validate_json(call.args[1])
        for call in redis.client.rpush.await_args_list
    ]


@pytest.mark.parametrize("channel", list(ConversationChannel))
async def test_a_finished_conversation_queues_one_job(redis, channel):
    await queue.enqueue_conversation_evaluation("lead-1", channel, TEMPLATE_ID)

    (job,) = _pushed(redis)
    assert (job.source_id, job.channel, job.topics_done) == ("lead-1", channel, False)


async def test_a_template_with_nothing_on_queues_nothing(monkeypatch, redis):
    monkeypatch.setattr(queue, "has_enabled_evaluations", AsyncMock(return_value=False))

    await queue.enqueue_conversation_evaluation(
        "lead-1", ConversationChannel.VOICE, TEMPLATE_ID
    )

    redis.client.rpush.assert_not_awaited()


async def test_a_failed_job_goes_back_to_the_head_of_the_queue(monkeypatch):
    client = SimpleNamespace(lpush=AsyncMock())
    service = SimpleNamespace(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(queue, "get_redis_service", AsyncMock(return_value=service))

    await queue.requeue_conversation_evaluation(_job(deliveries=2, topics_done=True))

    key, raw = client.lpush.await_args.args
    job = ConversationEvaluationJob.model_validate_json(raw)
    assert (key, job.deliveries, job.topics_done) == (
        queue.CONVERSATION_EVALUATION_QUEUE,
        2,
        True,
    )


# --- queued once the outcome is final ---------------------------------------------


def _finished_lead(**overrides: Any) -> LeadCallTracker:
    values: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="r",
        template="t",
        template_id=TEMPLATE_ID,
        merchant_id="m",
        payload={},
        metaData={"transcription": [{"role": "user", "content": "hi"}]},
        status=LeadCallStatus.FINISHED,
        outcome="BUSY",
        call_id="CA-1",
        attempt_count=0,
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )
    values.update(overrides)
    return LeadCallTracker(**values)


@pytest.fixture
def tap(monkeypatch):
    """crm_mirror's finished tap with its collaborators recorded in order."""
    order: List[str] = []
    spawned: List[Any] = []
    queued: List[Dict[str, Any]] = []

    async def checked(lead: Any) -> str:
        order.append("checked_outcome")
        return "NOT_INTERESTED"

    async def mirror(*args: Any, **kwargs: Any) -> None:
        order.append("call.completed")

    async def enqueue(
        source_id: str, channel: Any, template_id: str, **kwargs: Any
    ) -> None:
        order.append(f"job:{source_id}")
        queued.append({"channel": channel, "template_id": template_id, **kwargs})

    monkeypatch.setattr(
        crm_mirror,
        "spawn_background_task",
        lambda coro, name="": spawned.append(asyncio.ensure_future(coro)),
    )
    monkeypatch.setattr(crm_mirror, "checked_outcome", checked)
    monkeypatch.setattr(crm_mirror, "mirror_to_crm", mirror)
    monkeypatch.setattr(crm_mirror, "call_facts", AsyncMock(return_value={}))
    monkeypatch.setattr(crm_mirror, "enqueue_conversation_evaluation", enqueue)
    return SimpleNamespace(
        order=order, spawned=spawned, queued=queued, monkeypatch=monkeypatch
    )


async def test_a_calls_job_is_queued_after_the_outcome_check(tap):
    crm_mirror._finished_lead_tap(_finished_lead())
    await asyncio.gather(*tap.spawned)

    # the judge reads the lead when the job runs: by then the outcome is final
    assert tap.order == ["checked_outcome", "call.completed", "job:lead-1"]
    assert tap.queued == [
        {"channel": ConversationChannel.VOICE, "template_id": TEMPLATE_ID}
    ]


async def test_a_call_with_nothing_said_queues_no_job(tap):
    crm_mirror._finished_lead_tap(_finished_lead(metaData={}))
    await asyncio.gather(*tap.spawned)

    assert tap.order == ["checked_outcome", "call.completed"]


async def test_the_job_is_queued_even_if_call_completed_fails(tap):
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("crm down")

    tap.monkeypatch.setattr(crm_mirror, "mirror_to_crm", broken)

    crm_mirror._finished_lead_tap(_finished_lead())
    await asyncio.gather(*tap.spawned, return_exceptions=True)

    assert tap.order == ["checked_outcome", "job:lead-1"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"merchant_id": None},
        {"execution_mode": ExecutionMode.TELEPHONY_TEST},
        {"metaData": {"transcription": [{"role": "user"}], "playground": True}},
    ],
)
async def test_a_call_kept_from_the_crm_is_still_evaluated(tap, overrides):
    crm_mirror._finished_lead_tap(_finished_lead(**overrides))
    await asyncio.gather(*tap.spawned)

    # no outcome check and no call.completed, as before; topics and custom
    # evals as wherever topics ran (the worker drops playground calls)
    assert tap.order == ["job:lead-1"]


@pytest.mark.parametrize(
    "mode, expected",
    [
        # a production web call: the outcome check and call.completed, as on
        # a phone call, then its job
        (ExecutionMode.DAILY, ["checked_outcome", "call.completed", "job:lead-1"]),
        # the dashboard's demo calls and the widget's voice never reach the
        # CRM; their job is queued all the same
        (ExecutionMode.DAILY_TEST, ["job:lead-1"]),
        (ExecutionMode.DAILY_STREAM, ["job:lead-1"]),
    ],
)
async def test_a_web_calls_job_is_queued_by_the_finished_tap(tap, mode, expected):
    crm_mirror._finished_lead_tap(_finished_lead(execution_mode=mode))
    await asyncio.gather(*tap.spawned)

    assert tap.order == expected


# --- the worker ------------------------------------------------------------------


@pytest.fixture
def worker_env(monkeypatch):
    order: List[str] = []

    async def topics(context: Any, evaluation: Any) -> bool:
        order.append("topics")
        return True

    async def evals(job: Any, context: Any, evaluations: Any) -> List[Any]:
        order.append("evals")
        return []

    env = SimpleNamespace(
        order=order,
        evaluations=[TOPIC_ROW, _row("quality")],
        analyze=AsyncMock(side_effect=topics),
        run=AsyncMock(side_effect=evals),
        topic_failure=AsyncMock(),
        eval_failures=AsyncMock(),
        requeue=AsyncMock(),
        context=AsyncMock(return_value=_context()),
    )
    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "_paused_until", 0.0)
    monkeypatch.setattr(
        worker,
        "get_enabled_evaluations",
        AsyncMock(side_effect=lambda _: env.evaluations),
    )
    monkeypatch.setattr(worker, "get_analysis_context", env.context)
    monkeypatch.setattr(worker, "analyze_topics", env.analyze)
    monkeypatch.setattr(worker, "run_agent_evals", env.run)
    monkeypatch.setattr(worker, "save_topic_failure", env.topic_failure)
    monkeypatch.setattr(worker, "save_eval_failures", env.eval_failures)
    monkeypatch.setattr(worker, "requeue_conversation_evaluation", env.requeue)
    return env


async def test_one_job_runs_the_custom_evals_then_the_topics(worker_env):
    job = _job()

    await worker._evaluate(job)

    assert worker_env.order == ["evals", "topics"]
    worker_env.run.assert_awaited_once_with(job, _context(), worker_env.evaluations)
    worker_env.requeue.assert_not_awaited()


async def test_an_agent_with_only_custom_evals_runs_no_topics(worker_env):
    worker_env.evaluations = [_row("quality")]

    await worker._evaluate(_job())

    assert worker_env.order == ["evals"]


async def test_failed_custom_evals_requeue_the_job_with_its_topics_done(worker_env):
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job()

    await worker._evaluate(job)

    worker_env.requeue.assert_awaited_once_with(job)
    assert (job.deliveries, job.topics_done) == (1, True)
    # as a topics outage: every consumer pauses before the retry
    assert worker._consecutive_failures == 1
    assert worker._paused_until > time.monotonic()


async def test_failing_custom_evals_back_off_like_the_topics(worker_env):
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job()
    pauses = []

    for _ in range(3):
        worker._paused_until = 0.0  # the previous pause is over
        await worker._evaluate(job)
        pauses.append(round(worker._paused_until - time.monotonic()))

    assert pauses == [30, 60, 120]


async def test_custom_evals_that_get_through_end_the_pause(worker_env):
    async def stores(job: Any, context: Any, evaluations: Any) -> List[Any]:
        job.evals_done.append("quality")
        return []

    worker_env.evaluations = [_row("quality")]
    worker_env.run.side_effect = stores
    worker._consecutive_failures = 2

    await worker._evaluate(_job())

    assert (worker._consecutive_failures, worker._paused_until) == (0, 0.0)


async def test_a_retry_never_runs_the_stored_topics_again(worker_env):
    await worker._evaluate(_job(deliveries=1, topics_done=True))

    assert worker_env.order == ["evals"]


async def test_an_unreachable_topics_model_still_runs_the_custom_evals(worker_env):
    worker_env.analyze.side_effect = worker.ModelUnavailableError("down")
    job = _job()

    await worker._evaluate(job)

    worker_env.run.assert_awaited_once()
    worker_env.requeue.assert_awaited_once_with(job)
    assert (job.deliveries, job.topics_done) == (1, False)
    assert worker._paused_until > time.monotonic()  # every consumer pauses


async def test_the_last_delivery_fails_each_part_still_failing(worker_env):
    worker_env.analyze.side_effect = worker.ModelUnavailableError("down")
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job(deliveries=worker._MAX_DELIVERIES - 1)

    await worker._evaluate(job)

    worker_env.requeue.assert_not_awaited()
    context, evaluation, error = worker_env.topic_failure.await_args.args
    assert (evaluation, error.startswith("MODEL_UNAVAILABLE after 5 deliveries")) == (
        TOPIC_ROW,
        True,
    )
    worker_env.eval_failures.assert_awaited_once_with(
        job, _context(), [_row("quality")]
    )


async def test_the_last_delivery_saves_the_evals_failures_before_the_topics(
    worker_env,
):
    worker_env.analyze.side_effect = worker.ModelUnavailableError("down")
    worker_env.topic_failure.side_effect = RuntimeError("db down")
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job(deliveries=worker._MAX_DELIVERIES - 1)

    with pytest.raises(RuntimeError):
        await worker._evaluate(job)

    worker_env.eval_failures.assert_awaited_once_with(
        job, _context(), [_row("quality")]
    )


async def test_failed_custom_evals_are_still_retried_when_the_topics_raise(
    worker_env,
):
    worker_env.analyze.side_effect = RuntimeError("db down")
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job()

    await worker._evaluate(job)

    worker_env.requeue.assert_awaited_once_with(job)
    assert (job.deliveries, job.topics_done) == (1, True)


async def test_a_topics_error_with_every_eval_passed_still_raises(worker_env):
    worker_env.analyze.side_effect = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await worker._evaluate(_job())

    worker_env.requeue.assert_not_awaited()


async def test_failing_custom_evals_are_retried_until_the_last_delivery(worker_env):
    worker_env.run.side_effect = None
    worker_env.run.return_value = [_row("quality")]
    job = _job()

    for _ in range(worker._MAX_DELIVERIES):
        await worker._evaluate(job)

    assert worker_env.requeue.await_count == worker._MAX_DELIVERIES - 1
    assert job.deliveries == worker._MAX_DELIVERIES
    worker_env.eval_failures.assert_awaited_once_with(
        job, _context(), [_row("quality")]
    )
    # the topics passed on the first delivery and ran only then
    assert worker_env.order.count("topics") == 1


def _lead() -> Any:
    return SimpleNamespace(
        id="lead-1",
        template_id=TEMPLATE_ID,
        status=LeadCallStatus.FINISHED,
        call_initiated_time=datetime(2026, 10, 8, tzinfo=timezone.utc),
        created_at=None,
        metaData={"transcription": [{"role": "user", "content": "hi"}]},
        outcome="BUSY",
        payload={"order_id": "o-1"},
        reseller_id="r",
        merchant_id="m",
    )


async def test_a_calls_context_carries_what_a_judge_needs(monkeypatch):
    monkeypatch.setattr(worker, "get_lead_by_id", AsyncMock(return_value=_lead()))

    context = await worker.get_analysis_context(_job())

    assert context is not None
    assert context["recorded_outcome"] == "BUSY"
    assert context["payload"] == {"order_id": "o-1"}
    assert context["channel"] == "VOICE"
    assert context["meta_data"]["transcription"]
    assert context["transcript"] == [{"role": "user", "content": "hi"}]


# --- batching ------------------------------------------------------------------


def test_the_preset_eval_is_not_a_custom_eval():
    rows = [
        _row("quality"),
        _row(OUTCOME_CORRECTNESS),
        TOPIC_ROW,
    ]
    assert [row["name"] for row in batch.agent_evals(rows)] == ["quality"]


def test_evals_on_one_model_share_a_request_and_prompt_evals_wait():
    rows = [
        _row("a"),
        _row("b"),
        _row("c", model="jev-2"),
        _row("d", engine="prompt"),
    ]
    groups = batch.batches(rows)
    assert [[row["name"] for row in group] for group in groups] == [
        ["a", "b"],
        ["c"],
    ]


def test_evals_on_another_endpoint_get_their_own_request():
    rows = [_row("a"), _row("b"), _row("c"), _row("d")]
    for row, names in (
        (rows[1], {"api_url_name": "GRID_URL", "api_key_name": "GRID_API"}),
        (rows[2], {"api_key_name": "OTHER_KEY"}),
        (rows[3], {"api_url_name": "GRID_URL", "api_key_name": "GRID_API"}),
    ):
        row["configuration"] = json.dumps({**json.loads(row["configuration"]), **names})
    groups = batch.batches(rows)
    assert [[row["name"] for row in group] for group in groups] == [
        ["a"],
        ["b", "d"],
        ["c"],
    ]


async def test_a_batch_is_one_request_split_back_per_eval(monkeypatch):
    run = AsyncMock(
        return_value=Verdict(
            engine="structured",
            provider="typesafe",
            model="jev-1.13.0",
            result=[
                NoulResult(key="a.asked", label="Asked", value=0.9),
                ChoiceResult(key="b.asked", label="Asked", value="X", confidence=0.7),
            ],
        )
    )
    save = AsyncMock()
    monkeypatch.setattr(batch, "run_evaluation", run)
    monkeypatch.setattr(batch, "save_verdict", save)

    marked: List[str] = []
    await batch.run_batch(
        [_row("a"), _row("b")], _context(), ConversationChannel.VOICE, marked
    )

    sent = run.await_args_list[-1].args[2]
    assert [question["key"] for question in sent["questions"]] == [
        "a.asked",
        "b.asked",
    ]
    stored = {call.args[4]: call.args[3] for call in save.await_args_list}
    assert [result.key for result in stored["a"].result] == ["asked"]
    assert [result.key for result in stored["b"].result] == ["asked"]
    assert save.await_args_list[0].args[0] == "a-id"
    assert marked == ["a", "b"]


# --- the custom evals' run -------------------------------------------------------


async def _stores_all(batch_rows: Any, context: Any, channel: Any, stored: Any) -> None:
    stored.extend(row["name"] for row in batch_rows)


@pytest.fixture
def run_env(monkeypatch):
    env = SimpleNamespace(
        run_batch=AsyncMock(side_effect=_stores_all),
        failure=AsyncMock(),
    )
    monkeypatch.setattr(agent_evals, "run_batch", env.run_batch)
    monkeypatch.setattr(agent_evals, "save_evaluation_failure", env.failure)
    return env


async def test_the_first_delivery_runs_every_custom_eval_and_marks_it(run_env):
    job = _job()

    failed = await agent_evals.run_agent_evals(job, _context(), [_row("a"), _row("b")])

    assert failed == []
    run_env.run_batch.assert_awaited_once()
    assert job.evals_done == ["a", "b"]


async def test_a_retry_runs_only_the_evals_not_marked_passed(run_env):
    job = _job(deliveries=1, evals_done=["a"])

    await agent_evals.run_agent_evals(job, _context(), [_row("a"), _row("b")])

    batch_rows, _, _, _ = run_env.run_batch.await_args.args
    assert [row["name"] for row in batch_rows] == ["b"]
    assert job.evals_done == ["a", "b"]


async def test_a_job_with_every_eval_passed_runs_none(run_env):
    job = _job(deliveries=1, evals_done=["a"])

    failed = await agent_evals.run_agent_evals(job, _context(), [_row("a")])

    assert failed == []
    run_env.run_batch.assert_not_awaited()


async def test_a_failed_batch_is_handed_back_for_the_job_to_retry(run_env):
    run_env.run_batch.side_effect = RuntimeError("reply unreadable")
    job = _job(deliveries=1)

    failed = await agent_evals.run_agent_evals(job, _context(), [_row("a")])

    assert [row["name"] for row in failed] == ["a"]
    assert job.evals_done == []
    run_env.failure.assert_not_awaited()


async def test_a_batch_the_provider_failed_gets_its_failed_rows_at_once(run_env):
    # the provider already retried what it could; a missing key never succeeds
    run_env.run_batch.side_effect = ProviderError(
        "typesafe", "API key not found for config key: GRID_KEY"
    )
    job = _job()

    failed = await agent_evals.run_agent_evals(job, _context(), [_row("a")])

    # nothing for the worker to retry, and no later delivery runs it again
    assert failed == []
    assert job.evals_done == ["a"]
    (saved,) = run_env.failure.await_args_list
    assert saved.args[0] == "a-id"
    assert saved.args[-1] == (
        "provider error: typesafe: API key not found for config key: GRID_KEY"
    )


async def test_a_batch_failing_part_way_keeps_what_it_stored(run_env):
    async def stores_a_then_fails(batch_rows, context, channel, stored):
        stored.append("a")
        raise RuntimeError("db down")

    run_env.run_batch.side_effect = stores_a_then_fails
    job = _job()

    failed = await agent_evals.run_agent_evals(job, _context(), [_row("a"), _row("b")])

    assert [row["name"] for row in failed] == ["b"]
    assert job.evals_done == ["a"]


async def test_a_chat_runs_no_custom_evals(run_env):
    failed = await agent_evals.run_agent_evals(
        _job(channel=ConversationChannel.CHAT), _context(), [_row("a")]
    )

    assert failed == []
    run_env.run_batch.assert_not_awaited()


async def test_the_last_delivery_saves_a_failed_row_per_eval(run_env):
    await agent_evals.save_eval_failures(
        _job(deliveries=5), _context(), [_row("a"), _row("b")]
    )

    first, second = run_env.failure.await_args_list
    assert first.args[:3] == ("a-id", "CONVERSATION_EVALS", "lead-1")
    assert (second.args[0], second.args[-1]) == ("b-id", "failed after 5 deliveries")
