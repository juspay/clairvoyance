"""The per-lead and per-call template reads go through the Redis cache.

Five paths read a template once per claimed lead, pushed lead, answered call
or finished call: the dispatcher, the push door, the answer webhook, the CRM
mirror and the Plivo account lookup. They used the raw accessor — a full row
with flow, configurations and secrets, plus a Pydantic decode — while chat and
widget already read the same templates through ``get_template_by_id_cached``.

Switching is only safe if a cache hit IS the template the database returned.
These paths decide what gets dialled and how, so a hit that differed would
change a call, not a log line. The tests below hold every example template in
the repo to that, and pin the one field that does not survive (timestamps).
"""

import asyncio
import importlib
import inspect
import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.template import cache
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

EXAMPLES = sorted(
    (pathlib.Path(cache.__file__).parent.parent / "examples" / "templates").glob(
        "*.json"
    )
)

HOT_PATHS = [
    "app.ai.voice.agents.breeze_buddy.dispatch.worker",
    "app.api.routers.breeze_buddy.leads.handlers",
    "app.api.routers.breeze_buddy.telephony.answer.handlers",
    "app.ai.voice.agents.breeze_buddy.crm_mirror",
    "app.ai.voice.agents.breeze_buddy.services.telephony.plivo.account",
]


class _Redis:
    """The two calls the cache makes, over a dict."""

    def __init__(self, fail: bool = False) -> None:
        self.store: Dict[str, Any] = {}
        self.fail = fail

    async def get(self, key: str) -> Optional[str]:
        if self.fail:
            raise ConnectionError("redis is down")
        return self.store.get(key)

    async def setex(self, key: str, value: str, ttl_seconds: int) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.store[key] = value


def _row(example: pathlib.Path, **extra: Any) -> TemplateModel:
    d = json.loads(example.read_text())
    return TemplateModel.model_validate(
        {
            "id": "tpl-1",
            "reseller_id": "res-1",
            "merchant_id": "mer-1",
            "name": d.get("template_name") or d.get("name") or example.stem,
            "flow": d["flow"],
            "configurations": d.get("configurations"),
            "secrets": d.get("secrets"),
            "expected_payload_schema": d.get("expected_payload_schema"),
            "expected_callback_response_schema": d.get(
                "expected_callback_response_schema"
            ),
            **extra,
        }
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, row: TemplateModel, redis: _Redis
) -> List[str]:
    reads: List[str] = []

    async def db_read(template_id: str) -> TemplateModel:
        reads.append(template_id)
        return row

    async def redis_service() -> _Redis:
        return redis

    monkeypatch.setattr(cache, "is_redis_configured", lambda: True)
    monkeypatch.setattr(cache, "get_redis_service", redis_service)
    monkeypatch.setattr(cache, "_db_get_template_by_id", db_read)
    return reads


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_a_cache_hit_is_the_template_the_database_read(
    example: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = _Redis()
    reads = _wire(monkeypatch, _row(example), redis)

    miss = asyncio.run(cache.get_template_by_id_cached("tpl-1"))
    hit = asyncio.run(cache.get_template_by_id_cached("tpl-1"))

    assert reads == ["tpl-1"], "the second read should have been served by Redis"
    assert hit is not miss  # really decoded from Redis, not the same object
    assert hit == miss


def test_secrets_come_back_from_a_hit_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets ride in a plain dict, so the cache must store them revealed —
    a masked value here would send ``**********`` to a merchant's API."""
    secrets = {"api_key": "sk-live-123", "nested": {"token": "abc", "n": 7}}
    redis = _Redis()
    _wire(monkeypatch, _row(EXAMPLES[0], secrets=secrets), redis)

    asyncio.run(cache.get_template_by_id_cached("tpl-1"))
    hit = asyncio.run(cache.get_template_by_id_cached("tpl-1"))

    assert hit is not None and hit.secrets == secrets


def test_timestamps_come_back_as_text_and_nothing_else_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``created_at``/``updated_at`` are typed ``Any``: the database gives a
    datetime, a hit gives its ISO string. None of the hot paths reads them —
    if one ever needs to, it must parse, or read the row uncached."""
    when = datetime(2026, 10, 2, 4, 30, tzinfo=timezone.utc)
    redis = _Redis()
    _wire(monkeypatch, _row(EXAMPLES[0], created_at=when, updated_at=when), redis)

    miss = asyncio.run(cache.get_template_by_id_cached("tpl-1"))
    hit = asyncio.run(cache.get_template_by_id_cached("tpl-1"))

    assert miss is not None and hit is not None
    assert hit.created_at == when.isoformat().replace("+00:00", "Z")
    stamps = {"created_at", "updated_at"}
    assert hit.model_dump(exclude=stamps) == miss.model_dump(exclude=stamps)


def test_with_redis_down_every_read_is_the_database_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache is best-effort: an outage must cost latency, never a call."""
    row = _row(EXAMPLES[0])
    reads = _wire(monkeypatch, row, _Redis(fail=True))

    got = [asyncio.run(cache.get_template_by_id_cached("tpl-1")) for _ in range(2)]

    assert got == [row, row]
    assert reads == ["tpl-1", "tpl-1"]


@pytest.mark.parametrize("module", HOT_PATHS)
def test_each_hot_path_reads_templates_through_the_cache(module: str) -> None:
    mod = importlib.import_module(module)
    assert mod.get_template_by_id_cached is cache.get_template_by_id_cached
    raw_call = re.search(
        r"(?<![\w.])get_template_by_id\(|\.get_template_by_id\(", inspect.getsource(mod)
    )
    assert raw_call is None, f"{module} reads a template uncached again"
