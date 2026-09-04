"""One end-to-end orchestration check for the topic queue and worker."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

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
        rpush=AsyncMock(),
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
    assert ConversationEvaluationJob.model_validate_json(queued[1]) == job

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
    chat_finalize = AsyncMock()
    monkeypatch.setattr(chat_cleanup, "finalize_guardrail_metrics", chat_finalize)
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
    chat_finalize.assert_awaited_once_with("session-id")


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


def test_enabled_evaluations_return_topic_enum_value() -> None:
    query, values = get_enabled_evaluations_query(TEMPLATE_ID)
    assert "SELECT id" in query
    assert "evaluation_type::text AS evaluation_type" in query
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
