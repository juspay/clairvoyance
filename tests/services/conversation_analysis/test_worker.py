"""One end-to-end orchestration check for the topic queue and worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis import (
    queue,
    worker,
)
from app.database.accessor.breeze_buddy import chat_session, lead_call_tracker
from app.schemas import LeadCallStatus


async def test_queue_id_is_evaluated_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        rpush=AsyncMock(),
        blpop=AsyncMock(return_value=(queue.TOPIC_ANALYSIS_QUEUE, "analysis-id")),
    )
    service = SimpleNamespace(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(queue, "get_redis_service", AsyncMock(return_value=service))

    await queue.enqueue_topic_analysis("analysis-id")
    assert await queue.dequeue_topic_analysis() == "analysis-id"
    client.rpush.assert_awaited_once_with(queue.TOPIC_ANALYSIS_QUEUE, "analysis-id")

    job = {
        "id": "analysis-id",
        "channel": "VOICE",
        "source_id": "call-id",
        "evaluation_configuration": {
            "model": "grid-model",
            "system_prompt": "Extract {max_topics}: {accepted_topics}",
        },
        "accepted_topics": [],
    }
    claim = AsyncMock(side_effect=[job, None])
    extract = AsyncMock(return_value=[{"type": "delivery_delay"}])
    complete = AsyncMock()
    monkeypatch.setattr(worker, "claim_analysis_by_id", claim)
    monkeypatch.setattr(
        worker,
        "get_analysis_transcript",
        AsyncMock(return_value=[{"role": "user", "content": "My order is late"}]),
    )
    monkeypatch.setattr(worker, "extract_topics", extract)
    monkeypatch.setattr(worker, "complete_analysis", complete)

    await asyncio.gather(worker._analyze(job["id"]), worker._analyze(job["id"]))

    extract.assert_awaited_once()
    complete.assert_awaited_once_with("analysis-id", [{"type": "delivery_delay"}])


async def test_analysis_retries_once_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = {
        "id": "analysis-id",
        "channel": "VOICE",
        "source_id": "call-id",
        "accepted_topics": [],
        "evaluation_configuration": {},
    }
    extract = AsyncMock(
        side_effect=[TimeoutError("Grid timed out"), [{"type": "payment_issue"}]]
    )
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(worker, "claim_analysis_by_id", AsyncMock(return_value=job))
    monkeypatch.setattr(worker, "get_analysis_transcript", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "extract_topics", extract)
    monkeypatch.setattr(worker, "complete_analysis", complete)
    monkeypatch.setattr(worker, "fail_analysis", fail)

    await worker._analyze("analysis-id")

    assert extract.await_count == 2
    complete.assert_awaited_once_with("analysis-id", [{"type": "payment_issue"}])
    fail.assert_not_awaited()


async def test_completion_survives_analysis_insert_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_result = object()
    monkeypatch.setattr(
        lead_call_tracker,
        "run_parameterized_query",
        AsyncMock(return_value=[{}]),
    )
    monkeypatch.setattr(
        lead_call_tracker, "decode_lead_call_tracker", lambda _: voice_result
    )
    monkeypatch.setattr(
        lead_call_tracker,
        "create_voice_analysis",
        AsyncMock(side_effect=RuntimeError("topic insert failed")),
    )
    assert (
        await lead_call_tracker.update_lead_call_completion_details(
            "lead-id", status=LeadCallStatus.FINISHED
        )
        is voice_result
    )

    chat_result = object()
    monkeypatch.setattr(
        chat_session,
        "run_parameterized_query",
        AsyncMock(return_value=[{"id": "session-id"}]),
    )
    monkeypatch.setattr(chat_session, "decode_chat_session", lambda _: chat_result)
    monkeypatch.setattr(
        chat_session,
        "create_chat_analysis",
        AsyncMock(side_effect=RuntimeError("topic insert failed")),
    )
    assert (
        await chat_session.end_chat_session("session-id", "user_ended") is chat_result
    )
