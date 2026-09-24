"""One end-to-end orchestration check for the topic queue and worker."""

import asyncio
import json
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from fastapi import HTTPException
from pipecat.services.openai.llm import OpenAILLMService

from app.ai.voice.agents.breeze_buddy.chat import cleanup as chat_cleanup
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis import (
    queue,
    worker,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics import (
    evaluator,
    extractor,
)
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.ai.voice.llm._pools import get_openai_httpx_client
from app.api.routers.breeze_buddy.analytics.handlers import _validate_topic_filters
from app.database.accessor.breeze_buddy.analytics import evaluation_result
from app.database.queries.breeze_buddy.analytics.evaluation_result import (
    get_topic_dashboard_rows_query,
)
from app.database.queries.breeze_buddy.evaluation_config import (
    add_discovered_topics_query,
    get_enabled_evaluations_query,
    has_enabled_evaluations_query,
    initialize_evaluation_config_query,
)
from app.database.queries.breeze_buddy.evaluation_result import (
    save_evaluation_failure_query,
    save_evaluation_results_query,
)
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.chat import ChatSessionStatus
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
    EvaluationType,
)

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"


def _context(
    channel: ConversationChannel = ConversationChannel.VOICE,
) -> dict:
    return {
        "source_id": "call-id",
        "channel": channel.value,
        "reseller_id": "reseller",
        "merchant_id": "merchant",
        "template_id": TEMPLATE_ID,
        "started_at": datetime.now(timezone.utc),
        "transcript": [{"role": "user", "content": "My order is late"}],
    }


async def test_prompt_replacement_preserves_json_braces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SimpleNamespace(
        run_inference=AsyncMock(return_value='{"customer_needs": [], "topics": []}')
    )
    get_llm = AsyncMock(return_value=llm)
    monkeypatch.setattr(
        extractor,
        "get_config",
        AsyncMock(return_value="https://grid.example/v1/chat/completions"),
    )
    monkeypatch.setattr(extractor, "get_llm_service", get_llm)

    await extractor.extract_topics(
        [{"role": "user", "content": "My order is late"}],
        ["Delivery Delay"],
        {
            "model": "minimaxai/minimax-m2",
            "system_prompt": (
                'Return {"topics": []}. Limit {max_topics}. ' "Known {accepted_topics}"
            ),
            "settings": {"max_topics": 2},
        },
    )

    prompt = llm.run_inference.await_args.kwargs["system_instruction"]
    assert 'Return {"topics": []}' in prompt
    assert "Limit 2" in prompt
    assert '"type": "delivery_delay"' in prompt
    llm_call = get_llm.await_args
    assert llm_call is not None
    llm_config = llm_call.args[0]
    assert llm_config.model == "minimaxai/minimax-m2"
    assert llm_config.endpoint == "https://grid.example/v1"
    assert llm_config.api_key_name == "GRID_API_KEY"


async def test_non_list_transcript_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        worker,
        "get_lead_by_id",
        AsyncMock(
            return_value=SimpleNamespace(
                id="lead-id",
                reseller_id="reseller",
                merchant_id="merchant",
                template_id=TEMPLATE_ID,
                call_initiated_time=now,
                created_at=now,
                status=LeadCallStatus.FINISHED,
                outcome=None,
                payload=None,
                metaData={"transcription": {"role": "user"}},
            )
        ),
    )
    assert (
        await worker.get_analysis_context(
            ConversationEvaluationJob(
                source_id="lead-id",
                channel=ConversationChannel.VOICE,
                template_id=TEMPLATE_ID,
            )
        )
        is None
    )


async def test_chat_context_uses_existing_accessors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    get_session = AsyncMock(
        return_value=SimpleNamespace(
            id="session-id",
            reseller_id="reseller",
            merchant_id="merchant",
            template_id=TEMPLATE_ID,
            created_at=now,
            status=ChatSessionStatus.ENDED,
            metadata={},
        )
    )
    list_messages = AsyncMock(
        return_value=[
            SimpleNamespace(
                idx=1,
                role=SimpleNamespace(value="user"),
                content="My order is late",
            )
        ]
    )
    monkeypatch.setattr(worker, "get_chat_session_by_id", get_session)
    monkeypatch.setattr(worker, "list_chat_messages_for_session", list_messages)

    context = await worker.get_analysis_context(
        ConversationEvaluationJob(
            source_id="session-id",
            channel=ConversationChannel.CHAT,
            template_id=TEMPLATE_ID,
        )
    )

    assert context and context["transcript"][0]["content"] == "My order is late"
    list_messages.assert_awaited_once_with("session-id")


async def test_queue_job_is_evaluated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ConversationEvaluationJob(
        source_id="call-id",
        channel=ConversationChannel.VOICE,
        template_id=TEMPLATE_ID,
    )
    client = SimpleNamespace(
        rpush=AsyncMock(return_value=1),
        blpop=AsyncMock(
            return_value=(queue.CONVERSATION_EVALUATION_QUEUE, job.model_dump_json())
        ),
    )
    service = SimpleNamespace(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(queue, "get_redis_service", AsyncMock(return_value=service))
    monkeypatch.setattr(
        queue,
        "has_enabled_evaluations",
        AsyncMock(return_value=True),
    )

    await queue.enqueue_conversation_evaluation(
        job.source_id,
        job.channel,
        str(job.template_id),
    )
    assert await queue.dequeue_conversation_evaluation() == job
    queued = client.rpush.await_args.args
    assert queued[0] == queue.CONVERSATION_EVALUATION_QUEUE
    # enqueued_at is stamped at enqueue, so it is the one field that differs.
    assert ConversationEvaluationJob.model_validate_json(queued[1]).model_dump(
        exclude={"enqueued_at"}
    ) == job.model_dump(exclude={"enqueued_at"})

    evaluation = {
        "id": "00000000-0000-0000-0000-000000000010",
        "evaluation_type": "TOPIC",
        "configuration": {
            "model": "grid-model",
            "system_prompt": "Extract {max_topics}: {accepted_topics}",
        },
        "topics": [],
    }
    context = _context()
    extract = AsyncMock(return_value=[{"type": "delivery_delay"}])
    save = AsyncMock()
    monkeypatch.setattr(
        worker,
        "get_enabled_evaluations",
        AsyncMock(return_value=[evaluation]),
    )
    monkeypatch.setattr(
        worker,
        "get_analysis_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    await worker._evaluate(job)

    extract.assert_awaited_once()
    save.assert_awaited_once_with(
        evaluation["id"],
        EvaluationType.TOPIC.value,
        context["source_id"],
        context["reseller_id"],
        context["merchant_id"],
        context["template_id"],
        context["started_at"],
        [{"type": "delivery_delay"}],
    )


async def test_enqueue_failure_does_not_break_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        queue,
        "has_enabled_evaluations",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        queue,
        "get_redis_service",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )
    await queue.enqueue_conversation_evaluation(
        "call-id",
        ConversationChannel.VOICE,
        TEMPLATE_ID,
    )


async def test_disabled_template_is_not_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_redis = AsyncMock()
    monkeypatch.setattr(queue, "get_redis_service", get_redis)
    monkeypatch.setattr(
        queue,
        "has_enabled_evaluations",
        AsyncMock(return_value=False),
    )

    await queue.enqueue_conversation_evaluation(
        "call-id",
        ConversationChannel.VOICE,
        TEMPLATE_ID,
    )

    get_redis.assert_not_awaited()


async def test_disabled_template_does_not_create_evaluation_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ConversationEvaluationJob(
        source_id="call-id",
        channel=ConversationChannel.VOICE,
        template_id=TEMPLATE_ID,
    )
    get_context = AsyncMock()
    monkeypatch.setattr(
        worker,
        "get_enabled_evaluations",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(worker, "get_analysis_context", get_context)

    await worker._evaluate(job)

    get_context.assert_not_awaited()


async def test_analysis_retries_once_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    evaluation = {
        "id": "00000000-0000-0000-0000-000000000010",
        "evaluation_type": "TOPIC",
        "topics": [],
        "configuration": {},
    }
    extract = AsyncMock(
        side_effect=[TimeoutError("Grid timed out"), [{"type": "payment_issue"}]]
    )
    save = AsyncMock()
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    await evaluator.analyze_topics(context, evaluation)

    assert extract.await_count == 2
    save.assert_awaited_once_with(
        evaluation["id"],
        EvaluationType.TOPIC.value,
        context["source_id"],
        context["reseller_id"],
        context["merchant_id"],
        context["template_id"],
        context["started_at"],
        [{"type": "payment_issue"}],
    )


async def test_completion_enqueues_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import import_module

    from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext

    end_conversation_module = import_module(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation"
    )
    voice_result = SimpleNamespace(id="lead-id", template_id=TEMPLATE_ID)
    voice_enqueue = AsyncMock()
    completion = AsyncMock(return_value=voice_result)
    monkeypatch.setattr(
        end_conversation_module,
        "enqueue_conversation_evaluation",
        voice_enqueue,
    )
    monkeypatch.setattr(
        end_conversation_module,
        "update_span_with_evaluation_data",
        lambda _context: None,
    )

    for transport_type, call_sid in (("plivo", "call-id"), ("daily", None)):
        bot = SimpleNamespace(
            approval_manager=None,
            call_sid=call_sid,
            completion_function=completion,
            configurations=SimpleNamespace(knowledge_base=None),
            context=SimpleNamespace(
                messages=[{"role": "user", "content": "My order is late"}]
            ),
            conversation_ended=False,
            end_conversation_callbacks=[],
            errors=[],
            lead=SimpleNamespace(
                id="lead-id",
                metaData={},
                outcome="ISSUE_REPORTED",
                payload={},
            ),
            metrics_collector=None,
            pending_transfer=None,
            prior_generation_messages=[],
            task=None,
            transport_type=transport_type,
        )
        await end_conversation_module.end_conversation(TemplateContext(bot), {})

    assert completion.await_count == 2
    assert voice_enqueue.await_count == 2
    for queued in voice_enqueue.await_args_list:
        assert queued.args == (
            "lead-id",
            ConversationChannel.VOICE,
            TEMPLATE_ID,
        )

    chat_result = SimpleNamespace(id="session-id", template_id=TEMPLATE_ID)
    monkeypatch.setattr(
        chat_cleanup,
        "CHAT_SESSION_END_TIMEOUT_SECONDS",
        AsyncMock(return_value=3600),
    )
    monkeypatch.setattr(
        chat_cleanup,
        "list_idle_chat_sessions",
        AsyncMock(return_value=[SimpleNamespace(id="session-id")]),
    )
    monkeypatch.setattr(
        chat_cleanup, "end_chat_session", AsyncMock(return_value=chat_result)
    )
    monkeypatch.setattr(chat_cleanup, "terminate_pending_approvals", AsyncMock())
    lock = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
    monkeypatch.setattr(chat_cleanup, "RedisLock", lambda *_args, **_kwargs: lock)
    chat_enqueue = AsyncMock()
    monkeypatch.setattr(chat_cleanup, "enqueue_conversation_evaluation", chat_enqueue)

    await chat_cleanup.end_idle_chat_sessions()

    chat_enqueue.assert_awaited_once_with(
        "session-id",
        ConversationChannel.CHAT,
        TEMPLATE_ID,
    )


def test_topic_evaluation_requires_explicit_template_flag() -> None:
    assert "enable_topic_evaluation" not in ConfigurationModel().model_dump(
        exclude_none=True
    )
    assert ConfigurationModel(enable_topic_evaluation=True).enable_topic_evaluation


def test_evaluation_config_initializes_from_explicit_template_flag() -> None:
    query, values = initialize_evaluation_config_query("template-id")
    assert "enable_topic_evaluation" in query
    assert "defaults.template_id IS NULL" in query
    assert "defaults.evaluation_type = 'TOPIC'" in query
    assert "ON CONFLICT (template_id, evaluation_type) DO NOTHING" in query
    assert values == ["template-id"]


def test_evaluation_result_is_saved_after_evaluation() -> None:
    started_at = datetime.now(timezone.utc)
    query, values = save_evaluation_results_query(
        "00000000-0000-0000-0000-000000000010",
        EvaluationType.TOPIC.value,
        "source-id",
        "reseller",
        "merchant",
        TEMPLATE_ID,
        started_at,
        '[{"type": "delivery_delay"}]',
    )
    assert "INSERT INTO evaluation_result" in query
    assert "$1::uuid, $2::evaluation_type" in query
    assert "evaluation_config_id" in query
    assert "result, metadata" in query
    assert "'COMPLETED'" in query
    assert "channel" not in query
    assert "PROCESSING" not in query
    assert values == [
        "00000000-0000-0000-0000-000000000010",
        EvaluationType.TOPIC.value,
        "source-id",
        "reseller",
        "merchant",
        TEMPLATE_ID,
        started_at,
        '[{"type": "delivery_delay"}]',
    ]


def test_a_source_gets_at_most_one_failed_row() -> None:
    query, values = save_evaluation_failure_query(
        "00000000-0000-0000-0000-000000000010",
        EvaluationType.TOPIC.value,
        "source-id",
        "reseller",
        "merchant",
        TEMPLATE_ID,
        datetime.now(timezone.utc),
        "MODEL_BAD_RESPONSE after 2 attempt(s): no content",
    )
    assert "'FAILED'" in query
    assert "WHERE NOT EXISTS" in query
    assert "evaluation_config_id = $1::uuid AND source_id = $3::text" in query
    assert "AND status = 'FAILED'" in query
    assert values[2] == "source-id"


def test_enabled_evaluations_return_topic_enum_value() -> None:
    query, values = get_enabled_evaluations_query(TEMPLATE_ID)
    assert "SELECT id" in query
    assert "evaluation_type::text AS evaluation_type" in query
    assert "configuration ->> 'model' AS model" in query
    assert "AND enabled" in query
    assert values == [TEMPLATE_ID]
    assert EvaluationType.TOPIC.value == "TOPIC"

    query, values = has_enabled_evaluations_query(TEMPLATE_ID)
    assert "SELECT EXISTS" in query
    assert "AND enabled" in query
    assert values == [TEMPLATE_ID]


def test_topic_query_review_guards() -> None:
    catalog_query, _ = add_discovered_topics_query("template-id", ["Delivery"])
    assert "config.evaluation_type = 'TOPIC'" in catalog_query

    dashboard_query, _ = get_topic_dashboard_rows_query(
        {"date_from": date(2026, 8, 1), "date_to": date(2026, 8, 2)}
    )
    assert "WITH" not in dashboard_query
    assert "UNION ALL" not in dashboard_query
    assert "jsonb_array_elements" not in dashboard_query
    assert "voice_count" not in dashboard_query
    assert "chat_count" not in dashboard_query
    assert "evaluation_type = 'TOPIC'" in dashboard_query

    result_query, _ = save_evaluation_results_query(
        "00000000-0000-0000-0000-000000000010",
        EvaluationType.TOPIC.value,
        "source-id",
        "reseller",
        None,
        TEMPLATE_ID,
        datetime.now(timezone.utc),
        '[{"type": "delivery"}]',
    )
    assert "jsonb_array_elements" in result_query


async def test_topic_dashboard_aggregates_rows_after_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        {
            "source_id": f"source-{index}" if index < 10 else "source-other",
            "template_id": TEMPLATE_ID,
            "template_name": "Agent",
            "started_at": started_at,
            "raw_topic_type": f"topic_{index:02d}",
            "raw_label": f"Topic {index:02d}",
        }
        for index in range(12)
    ]
    monkeypatch.setattr(
        evaluation_result,
        "run_parameterized_query",
        AsyncMock(return_value=rows),
    )

    dashboard = await evaluation_result.get_topic_dashboard(
        {"date_from": date(2026, 8, 1), "date_to": date(2026, 8, 2)}
    )

    other = next(
        row
        for row in dashboard
        if row["result_type"] == "summary" and row["topic_type"] == "__other__"
    )
    assert other["underlying_topic_count"] == 2
    assert other["conversation_count"] == 1


def test_topic_filter_normalizes_template_alias() -> None:
    filters: dict[str, Any] = {
        "date_from": date(2026, 8, 1),
        "date_to": date(2026, 8, 2),
        "template": TEMPLATE_ID,
        "topic_type": "delivery_delay",
    }
    _validate_topic_filters(filters, drilldown=True)
    assert filters["template_id"] == TEMPLATE_ID

    filters["template_id"] = "00000000-0000-0000-0000-000000000002"
    with pytest.raises(HTTPException):
        _validate_topic_filters(filters, drilldown=True)


EVALUATION = {
    "id": "00000000-0000-0000-0000-000000000010",
    "evaluation_type": "TOPIC",
    "topics": [],
    "configuration": {"model": "open-large-sa"},
    "model": "open-large-sa",
}


def _status_error(
    error_class: Any, status_code: int, headers: dict | None = None
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://grid.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request, headers=headers)
    return error_class("model call failed", response=response, body=None)


def _job() -> ConversationEvaluationJob:
    return ConversationEvaluationJob(
        source_id="call-id",
        channel=ConversationChannel.VOICE,
        template_id=TEMPLATE_ID,
    )


def test_model_failures_are_classified() -> None:
    request = httpx.Request("POST", "https://grid.example/v1/chat/completions")
    classify = evaluator.classify_failure

    assert classify(TimeoutError()) == evaluator.MODEL_TIMEOUT
    assert classify(httpx.ReadTimeout("slow")) == evaluator.MODEL_UNAVAILABLE
    assert (
        classify(openai.APITimeoutError(request=request)) == evaluator.MODEL_UNAVAILABLE
    )
    assert (
        classify(openai.APIConnectionError(request=request))
        == evaluator.MODEL_UNAVAILABLE
    )
    assert (
        classify(_status_error(openai.InternalServerError, 503))
        == evaluator.MODEL_UNAVAILABLE
    )
    assert (
        classify(_status_error(openai.RateLimitError, 429))
        == evaluator.MODEL_UNAVAILABLE
    )
    assert (
        classify(_status_error(openai.AuthenticationError, 401))
        == evaluator.EVALUATION_ERROR
    )
    assert (
        classify(extractor.TopicModelResponseError("no content"))
        == evaluator.MODEL_BAD_RESPONSE
    )
    assert (
        classify(json.JSONDecodeError("Expecting value", "", 0))
        == evaluator.MODEL_BAD_RESPONSE
    )
    assert (
        classify(ValueError("evaluation_config.model is required"))
        == evaluator.EVALUATION_ERROR
    )
    assert classify(KeyError("transcript")) == evaluator.EVALUATION_ERROR


async def test_unreachable_model_is_raised_for_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extract = AsyncMock(
        side_effect=[
            _status_error(openai.InternalServerError, 503),
            _status_error(openai.RateLimitError, 429, {"retry-after": "7"}),
        ]
    )
    save = AsyncMock()
    save_failure = AsyncMock()
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    monkeypatch.setattr(evaluator, "save_evaluation_failure", save_failure)

    with pytest.raises(evaluator.ModelUnavailableError) as raised:
        await evaluator.analyze_topics(_context(), EVALUATION)

    assert extract.await_count == 2
    assert raised.value.retry_after == 7
    assert "model=open-large-sa" in str(raised.value)
    save.assert_not_awaited()
    save_failure.assert_not_awaited()


async def test_bad_model_response_saves_a_failed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extract = AsyncMock(
        side_effect=extractor.TopicModelResponseError(
            "Topic evaluator returned no content"
        )
    )
    save = AsyncMock()
    save_failure = AsyncMock()
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    monkeypatch.setattr(evaluator, "save_evaluation_failure", save_failure)

    # The model answered, so it is reachable: this ends an outage.
    assert await evaluator.analyze_topics(_context(), EVALUATION) is True

    assert extract.await_count == 2
    save.assert_not_awaited()
    failure_call = save_failure.await_args
    assert failure_call is not None
    assert failure_call.args[:3] == (EVALUATION["id"], "TOPIC", "call-id")
    assert failure_call.args[-1] == (
        "MODEL_BAD_RESPONSE after 2 attempt(s): "
        "TopicModelResponseError: Topic evaluator returned no content"
    )


async def test_our_own_timeout_fails_the_job_without_pausing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript too slow for our ceiling says nothing about the gateway:
    one retry, then a FAILED row, and no ModelUnavailableError to pause on."""
    extract = AsyncMock(side_effect=TimeoutError())
    save_failure = AsyncMock()
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_failure", save_failure)

    assert await evaluator.analyze_topics(_context(), EVALUATION) is False

    assert extract.await_count == 2
    failure_call = save_failure.await_args
    assert failure_call is not None
    assert (
        failure_call.args[-1] == "MODEL_TIMEOUT after 2 attempt(s): timeout after 60s"
    )


async def test_config_error_is_saved_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extract = AsyncMock(side_effect=_status_error(openai.AuthenticationError, 401))
    save_failure = AsyncMock()
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_failure", save_failure)

    await evaluator.analyze_topics(_context(), EVALUATION)

    assert extract.await_count == 1
    failure_call = save_failure.await_args
    assert failure_call is not None
    assert failure_call.args[-1].startswith(
        "EVALUATION_ERROR after 1 attempt(s): AuthenticationError"
    )


def _patch_evaluate_dependencies(
    monkeypatch: pytest.MonkeyPatch, analyze: AsyncMock
) -> AsyncMock:
    requeue = AsyncMock()
    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "_paused_until", 0.0)
    monkeypatch.setattr(
        worker, "get_enabled_evaluations", AsyncMock(return_value=[EVALUATION])
    )
    monkeypatch.setattr(
        worker, "get_analysis_context", AsyncMock(return_value=_context())
    )
    monkeypatch.setattr(worker, "analyze_topics", analyze)
    monkeypatch.setattr(worker, "requeue_conversation_evaluation", requeue)
    return requeue


async def test_unreachable_model_requeues_job_and_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyze = AsyncMock(
        side_effect=[
            evaluator.ModelUnavailableError("timeout after 120s"),
            evaluator.ModelUnavailableError("timeout after 120s"),
            evaluator.ModelUnavailableError("rate limited", retry_after=7),
            True,
        ]
    )
    requeue = _patch_evaluate_dependencies(monkeypatch, analyze)
    job = _job()

    pauses = []
    for _ in range(3):
        # A consumer only retries after sleeping the pause out, so expire it
        # here. Failures arriving *during* a pause are one outage, not three.
        worker._paused_until = time.monotonic()
        await worker._evaluate(job)
        pauses.append(round(worker._paused_until - time.monotonic()))

    assert pauses == [30, 60, 120]
    assert worker._consecutive_failures == 3
    assert requeue.await_count == 3
    requeue.assert_awaited_with(job)

    await worker._evaluate(job)

    assert worker._consecutive_failures == 0
    assert worker._paused_until == 0.0


async def test_consumers_evaluate_jobs_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs: asyncio.Queue = asyncio.Queue()
    for index in range(4):
        jobs.put_nowait(
            ConversationEvaluationJob(
                source_id=f"call-{index}",
                channel=ConversationChannel.CHAT,
                template_id=TEMPLATE_ID,
            )
        )
    finished = []
    in_flight = 0
    peak = 0

    async def slow_evaluate(job: ConversationEvaluationJob) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.2)
        in_flight -= 1
        finished.append(job.source_id)

    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=4))
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "_evaluate", slow_evaluate)

    started_at = time.monotonic()
    await worker.start_analysis_worker()
    try:
        while len(finished) < 4 and time.monotonic() - started_at < 2:
            await asyncio.sleep(0.01)
    finally:
        await worker.stop_analysis_worker()

    assert sorted(finished) == ["call-0", "call-1", "call-2", "call-3"]
    assert peak == 4
    assert worker._consumer_tasks == []


async def test_consumer_waiting_for_a_job_honours_a_new_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs: asyncio.Queue = asyncio.Queue()
    evaluated_at = []

    async def record_evaluate(job: ConversationEvaluationJob) -> None:
        evaluated_at.append(time.monotonic())

    monkeypatch.setattr(worker, "_consecutive_failures", 1)
    monkeypatch.setattr(worker, "_paused_until", 0.0)
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "_evaluate", record_evaluate)

    await worker.start_analysis_worker()
    try:
        await asyncio.sleep(0.05)
        paused_at = time.monotonic()
        monkeypatch.setattr(worker, "_paused_until", paused_at + 0.3)
        jobs.put_nowait(_job())
        while not evaluated_at and time.monotonic() - paused_at < 2:
            await asyncio.sleep(0.01)
    finally:
        await worker.stop_analysis_worker()

    assert evaluated_at
    assert evaluated_at[0] - paused_at >= 0.29


# --- concurrency, crash-safety and recovery regressions ---------------------


async def test_concurrent_jobs_keep_their_own_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four jobs evaluated at once: each save must carry its own source_id.

    Latencies are deliberately shuffled so the finish order differs from the
    start order. If any per-job state were shared, results would cross over.
    """
    delays = {"call-0": 0.30, "call-1": 0.05, "call-2": 0.20, "call-3": 0.10}

    async def context_for(job: ConversationEvaluationJob) -> dict:
        return {
            "source_id": job.source_id,
            "reseller_id": f"reseller-{job.source_id}",
            "merchant_id": f"merchant-{job.source_id}",
            "template_id": str(job.template_id),
            "started_at": datetime.now(timezone.utc),
            "transcript": [{"role": "user", "content": job.source_id}],
        }

    in_flight = 0
    peak = 0

    async def extract(transcript: Any, topics: Any, configuration: Any) -> list:
        nonlocal in_flight, peak
        source_id = transcript[0]["content"]
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(delays[source_id])
        in_flight -= 1
        return [{"type": "t", "label": f"label-{source_id}"}]

    saved: list = []

    async def save(
        evaluation_id: str,
        evaluation_type: str,
        source_id: str,
        reseller_id: str,
        merchant_id: Any,
        template_id: str,
        started_at: datetime,
        results: list,
    ) -> None:
        saved.append((source_id, reseller_id, results))

    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(
        worker, "get_enabled_evaluations", AsyncMock(return_value=[EVALUATION])
    )
    monkeypatch.setattr(worker, "get_analysis_context", context_for)
    monkeypatch.setattr(evaluator, "extract_topics", extract)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    monkeypatch.setattr(evaluator, "add_discovered_topics", AsyncMock())

    jobs = [
        ConversationEvaluationJob(
            source_id=f"call-{index}",
            channel=ConversationChannel.CHAT,
            template_id=TEMPLATE_ID,
        )
        for index in range(4)
    ]
    await asyncio.gather(*(worker._evaluate(job) for job in jobs))

    # All four were at the model at once, not one after another.
    assert peak == 4
    assert sorted(saved) == [
        (
            f"call-{index}",
            f"reseller-call-{index}",
            [{"type": "t", "label": f"label-call-{index}"}],
        )
        for index in range(4)
    ]


async def test_pod_death_mid_evaluation_loses_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLPOP removes the job, so a pod dying mid-evaluation drops it.

    Documents the at-most-once delivery we ship with: there is no in-flight
    list and no reaper, unlike the dispatcher.
    """
    pending = [_job()]
    completed: list = []

    async def dequeue() -> ConversationEvaluationJob:
        if pending:
            return pending.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def still_working(job: ConversationEvaluationJob) -> None:
        await asyncio.sleep(3600)
        completed.append(job.source_id)

    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", dequeue)
    monkeypatch.setattr(worker, "_evaluate", still_working)

    await worker.start_analysis_worker()
    await asyncio.sleep(0.05)
    await worker.stop_analysis_worker()

    assert pending == []
    assert completed == []


async def test_concurrent_failures_do_not_inflate_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One outage is one rung on the ladder, however many consumers hit it."""

    _patch_evaluate_dependencies(
        monkeypatch,
        AsyncMock(side_effect=evaluator.ModelUnavailableError("model down")),
    )

    await asyncio.gather(*(worker._evaluate(_job()) for _ in range(4)))

    pause = worker._paused_until - time.monotonic()
    assert pause <= worker._FIRST_PAUSE_SECONDS


async def test_a_no_op_job_does_not_reset_the_backoff_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with nothing to evaluate never reached the model.

    Clearing the outage here would drop the ladder back to its first rung and
    hammer a model that is still down, so the pause must survive it.
    """
    monkeypatch.setattr(worker, "_consecutive_failures", 3)
    paused_until = time.monotonic() + 300
    monkeypatch.setattr(worker, "_paused_until", paused_until)
    monkeypatch.setattr(
        worker, "get_enabled_evaluations", AsyncMock(return_value=[EVALUATION])
    )
    monkeypatch.setattr(worker, "get_analysis_context", AsyncMock(return_value=None))

    await worker._evaluate(_job())

    assert worker._consecutive_failures == 3
    assert worker._paused_until == paused_until


async def test_consumer_count_comes_from_dynamic_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluation concurrency is a Redis-turnable knob, not a constant."""
    jobs: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)

    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=7))
    await worker.start_analysis_worker()
    try:
        assert len(worker._consumer_tasks) == 7
    finally:
        await worker.stop_analysis_worker()

    # A zero in Redis must not silently stop evaluations altogether.
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=0))
    await worker.start_analysis_worker()
    try:
        assert len(worker._consumer_tasks) == 1
    finally:
        await worker.stop_analysis_worker()


async def test_every_evaluation_reuses_one_connection_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-request service must not bring its own pool: that pool is never
    closed, so its connection stays open until the garbage collector runs."""
    pools = []

    async def fresh_service(llm_config: Any) -> OpenAILLMService:
        llm = OpenAILLMService(api_key="key", base_url=llm_config.endpoint)

        async def run_inference(context: Any, system_instruction: str) -> str:
            pools.append(llm._client._client)
            return '{"customer_needs": [], "topics": []}'

        llm.run_inference = run_inference  # type: ignore[method-assign]
        return llm

    monkeypatch.setattr(
        extractor, "get_config", AsyncMock(return_value="https://grid.example/v1")
    )
    monkeypatch.setattr(extractor, "get_llm_service", fresh_service)

    for _ in range(2):
        await extractor.extract_topics(
            [{"role": "user", "content": "My order is late"}],
            [],
            {
                "model": "grid-model",
                "system_prompt": "Extract {max_topics}",
                "settings": {"max_topics": 2},
            },
        )

    assert pools[0] is pools[1] is get_openai_httpx_client()


async def test_consumers_waiting_on_a_probe_run_in_parallel_once_it_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumers that queued behind the probe must not stay single-file after
    the probe finds the model healthy again."""
    jobs: asyncio.Queue = asyncio.Queue()
    for index in range(4):
        jobs.put_nowait(
            ConversationEvaluationJob(
                source_id=f"call-{index}",
                channel=ConversationChannel.CHAT,
                template_id=TEMPLATE_ID,
            )
        )
    finished = []
    in_flight = 0
    peak_after_probe = 0

    async def evaluate(job: ConversationEvaluationJob) -> None:
        nonlocal in_flight, peak_after_probe
        if not finished:
            await asyncio.sleep(0.05)  # the probe: model is back
            worker._consecutive_failures = 0
        else:
            in_flight += 1
            peak_after_probe = max(peak_after_probe, in_flight)
            await asyncio.sleep(0.2)
            in_flight -= 1
        finished.append(job.source_id)

    monkeypatch.setattr(worker, "_consecutive_failures", 1)
    monkeypatch.setattr(worker, "_paused_until", time.monotonic())
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=4))
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "_evaluate", evaluate)

    started_at = time.monotonic()
    await worker.start_analysis_worker()
    try:
        while len(finished) < 4 and time.monotonic() - started_at < 2:
            await asyncio.sleep(0.01)
    finally:
        await worker.stop_analysis_worker()

    assert len(finished) == 4
    # The three that waited behind the probe then ran together, not single-file.
    assert peak_after_probe == 3


async def test_only_a_model_answer_ends_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe whose config never reached the model proves nothing, so it
    must not reset the ladder; any model answer must, even an unusable one."""
    paused_until = time.monotonic() + 300
    monkeypatch.setattr(worker, "_consecutive_failures", 3)
    monkeypatch.setattr(worker, "_paused_until", paused_until)
    monkeypatch.setattr(
        worker, "get_enabled_evaluations", AsyncMock(return_value=[EVALUATION])
    )
    monkeypatch.setattr(
        worker, "get_analysis_context", AsyncMock(return_value=_context())
    )
    monkeypatch.setattr(evaluator, "save_evaluation_failure", AsyncMock())
    monkeypatch.setattr(evaluator, "save_evaluation_results", AsyncMock())
    monkeypatch.setattr(evaluator, "add_discovered_topics", AsyncMock())

    monkeypatch.setattr(
        evaluator,
        "extract_topics",
        AsyncMock(side_effect=ValueError("evaluation_config has no system_prompt")),
    )
    await worker._evaluate(_job())
    assert worker._consecutive_failures == 3
    assert worker._paused_until == paused_until

    monkeypatch.setattr(
        evaluator,
        "extract_topics",
        AsyncMock(side_effect=extractor.TopicModelResponseError("no content")),
    )
    await worker._evaluate(_job())
    assert worker._consecutive_failures == 0
    assert worker._paused_until == 0.0

    monkeypatch.setattr(worker, "_consecutive_failures", 3)
    monkeypatch.setattr(worker, "_paused_until", paused_until)
    monkeypatch.setattr(
        evaluator, "extract_topics", AsyncMock(return_value=[{"label": "late"}])
    )
    await worker._evaluate(_job())
    assert worker._consecutive_failures == 0
    assert worker._paused_until == 0.0


async def test_a_job_the_model_keeps_failing_on_stops_being_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requeued at the head, a job that fails every try (a transcript too
    long for the timeout, say) would be the next one tried forever and hold
    every job behind it. After _MAX_DELIVERIES it gets a FAILED row instead."""
    requeue = _patch_evaluate_dependencies(
        monkeypatch,
        AsyncMock(side_effect=evaluator.ModelUnavailableError("timeout after 60s")),
    )
    save_failure = AsyncMock()
    monkeypatch.setattr(worker, "save_topic_failure", save_failure)
    job = _job()

    for delivery in range(1, worker._MAX_DELIVERIES):
        worker._paused_until = time.monotonic()
        await worker._evaluate(job)
        assert job.deliveries == delivery
    assert requeue.await_count == worker._MAX_DELIVERIES - 1
    save_failure.assert_not_awaited()

    # The count survives the trip through Redis.
    job = ConversationEvaluationJob.model_validate_json(job.model_dump_json())
    await worker._evaluate(job)

    assert requeue.await_count == worker._MAX_DELIVERIES - 1
    failure_call = save_failure.await_args
    assert failure_call is not None
    assert failure_call.args[2] == (
        f"MODEL_UNAVAILABLE after {worker._MAX_DELIVERIES} deliveries: "
        "timeout after 60s"
    )


async def test_a_waiting_probe_wakes_when_another_consumer_ends_the_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause can be minutes long. If a consumer mid-call succeeds in the
    meantime, the probe holding the lock must not sleep the rest of it out
    while every other consumer waits behind it."""
    jobs: asyncio.Queue = asyncio.Queue()
    jobs.put_nowait(_job())
    evaluate = AsyncMock()

    monkeypatch.setattr(worker, "_consecutive_failures", 1)
    monkeypatch.setattr(worker, "_paused_until", time.monotonic() + 600)
    monkeypatch.setattr(worker, "_OUTAGE_CHECK_SECONDS", 0.02)
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=1))
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "_evaluate", evaluate)

    await worker.start_analysis_worker()
    try:
        await asyncio.sleep(0.05)
        evaluate.assert_not_awaited()
        monkeypatch.setattr(worker, "_consecutive_failures", 0)
        await asyncio.sleep(0.1)
    finally:
        await worker.stop_analysis_worker()

    evaluate.assert_awaited_once()


async def test_waiting_consumers_hold_no_jobs_during_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jobs stay in Redis while the model is paused. A consumer that pops one
    and then waits on the lock holds it in memory, and a deploy drops it."""
    jobs: asyncio.Queue = asyncio.Queue()
    for _ in range(4):
        jobs.put_nowait(_job())

    monkeypatch.setattr(worker, "_consecutive_failures", 1)
    monkeypatch.setattr(worker, "_paused_until", time.monotonic() + 10)
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=4))
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "_evaluate", AsyncMock())

    await worker.start_analysis_worker()
    try:
        await asyncio.sleep(0.1)
        assert jobs.qsize() == 4
    finally:
        await worker.stop_analysis_worker()


async def test_a_job_popped_as_an_outage_begins_goes_back_to_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer already waiting for a job when the pause starts must hand
    the job back instead of holding it until the outage ends."""
    jobs: asyncio.Queue = asyncio.Queue()
    requeue = AsyncMock()
    evaluate = AsyncMock()

    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "_paused_until", 0.0)
    monkeypatch.setattr(worker, "BB_ANALYSIS_CONSUMER_COUNT", AsyncMock(return_value=1))
    monkeypatch.setattr(worker, "dequeue_conversation_evaluation", jobs.get)
    monkeypatch.setattr(worker, "requeue_conversation_evaluation", requeue)
    monkeypatch.setattr(worker, "_evaluate", evaluate)

    await worker.start_analysis_worker()
    try:
        await asyncio.sleep(0.05)  # the consumer is now blocked on the queue
        monkeypatch.setattr(worker, "_consecutive_failures", 1)
        monkeypatch.setattr(worker, "_paused_until", time.monotonic() + 10)
        job = _job()
        jobs.put_nowait(job)
        await asyncio.sleep(0.05)
    finally:
        await worker.stop_analysis_worker()

    requeue.assert_awaited_once_with(job)
    evaluate.assert_not_awaited()


async def test_retry_after_never_shortens_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway answering every 429 with Retry-After: 1 must still back off."""
    _patch_evaluate_dependencies(
        monkeypatch,
        AsyncMock(
            side_effect=evaluator.ModelUnavailableError("rate limited", retry_after=1)
        ),
    )

    pauses = []
    for _ in range(3):
        worker._paused_until = time.monotonic()
        await worker._evaluate(_job())
        pauses.append(round(worker._paused_until - time.monotonic()))

    assert pauses == [30, 60, 120]


async def test_a_long_retry_after_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_evaluate_dependencies(
        monkeypatch,
        AsyncMock(
            side_effect=evaluator.ModelUnavailableError("rate limited", retry_after=300)
        ),
    )

    await worker._evaluate(_job())

    assert round(worker._paused_until - time.monotonic()) == 300
