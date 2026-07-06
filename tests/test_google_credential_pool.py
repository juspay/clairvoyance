"""Regression tests for Google credential pooling and async error handling."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.template.generator import service as generator
from app.ai.voice.llm import _pools


@pytest.fixture(autouse=True)
def _clear_anthropic_vertex_pool():
    _pools._ANTHROPIC_VERTEX_POOLS.clear()
    yield
    _pools._ANTHROPIC_VERTEX_POOLS.clear()


def test_anthropic_vertex_pool_resolves_credentials_only_once(monkeypatch):
    calls = {"credentials": 0, "clients": 0}
    counter_lock = threading.Lock()

    def _credentials(**_):
        with counter_lock:
            calls["credentials"] += 1
        time.sleep(0.02)
        return SimpleNamespace(credentials=object())

    class _FakeClient:
        def __init__(self, **kwargs):
            with counter_lock:
                calls["clients"] += 1
            self.kwargs = kwargs

    monkeypatch.setattr(_pools, "get_google_credentials", _credentials)
    monkeypatch.setattr(_pools, "AsyncAnthropicVertex", _FakeClient)

    def _get_client():
        return _pools.get_anthropic_vertex_client(
            credentials_json="",
            project_id="project",
            region="region",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        clients = list(executor.map(lambda _: _get_client(), range(4)))

    assert all(client is clients[0] for client in clients)
    assert calls == {"credentials": 1, "clients": 1}


def test_anthropic_vertex_pool_keeps_distinct_fallback_configs(monkeypatch):
    calls = {"credentials": 0}

    def _credentials(**_):
        calls["credentials"] += 1
        return SimpleNamespace(credentials=object())

    class _FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(_pools, "get_google_credentials", _credentials)
    monkeypatch.setattr(_pools, "AsyncAnthropicVertex", _FakeClient)

    first = _pools.get_anthropic_vertex_client(
        credentials_json="first-json",
        project_id="project",
        region="region",
    )
    second = _pools.get_anthropic_vertex_client(
        credentials_json="second-json",
        project_id="project",
        region="region",
    )

    assert first is not second
    assert calls["credentials"] == 2


async def test_template_generator_offloads_and_sanitizes_missing_auth(monkeypatch):
    main_thread_id = threading.get_ident()
    worker_thread_ids: list[int] = []

    async def _credentials_json():
        return ""

    async def _project_id():
        return "project"

    def _missing_credentials(**_):
        worker_thread_ids.append(threading.get_ident())
        raise ValueError("private internal credential detail")

    monkeypatch.setattr(
        generator.dyn,
        "GOOGLE_VERTEX_CREDENTIALS_JSON",
        _credentials_json,
    )
    monkeypatch.setattr(generator.dyn, "GOOGLE_VERTEX_PROJECT_ID", _project_id)
    monkeypatch.setattr(
        generator,
        "get_anthropic_vertex_client",
        _missing_credentials,
    )

    frames = [
        frame
        async for frame in generator.TemplateGeneratorService(messages=[]).stream()
    ]
    error_payload = json.loads(frames[0].split("data: ", 1)[1])

    assert worker_thread_ids
    assert worker_thread_ids[0] != main_thread_id
    assert error_payload == {
        "code": "auth_error",
        "message": (
            "Vertex authentication is unavailable; configure Google ADC or "
            "fallback credentials"
        ),
    }
    assert "private internal credential detail" not in frames[0]
    assert frames[1].startswith("event: done")
