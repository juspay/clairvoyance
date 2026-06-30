"""Tests for the data source connector seam: registry, Google Sheets connector,
and the load_data_source global builtin (on-demand keyed loading).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from app.services.data_sources import (
    DATA_SOURCE_UNAVAILABLE,
    get_connector,
    register_connector,
)

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext


class _FakeConnector:
    def __init__(self, content: str = "fake-content", keys: Optional[List[str]] = None):
        self._content = content
        self._keys = keys or ["k1", "k2"]

    async def fetch(self, config: Dict[str, Any], key: Optional[str]) -> str:
        return self._content

    async def list_keys(self, config: Dict[str, Any]) -> List[str]:
        return self._keys

    def cache_key(self, config: Dict[str, Any], key: Optional[str]) -> str:
        return f"fake:{key}"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_register_and_get_connector():
    connector = _FakeConnector()
    register_connector("fake_type", connector)
    assert get_connector("fake_type") is connector


def test_get_unknown_connector_returns_none():
    assert get_connector("does_not_exist") is None


def test_builtin_connectors_register_without_explicit_import():
    """Production wiring: a cold process that imports ONLY the registry (never the
    connector module) must still resolve 'google_sheet'. Guards against the
    self-register-on-import connector never being imported in prod.
    """
    code = (
        "from app.services.data_sources import get_connector; "
        "assert get_connector('google_sheet') is not None, 'not registered'; "
        "print('REGISTERED')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT},
        capture_output=True,
        text=True,
        timeout=60,  # never hang CI if the import wedges
    )
    assert proc.returncode == 0, proc.stderr
    assert "REGISTERED" in proc.stdout


# ---------------------------------------------------------------------------
# GoogleSheetConnector
# ---------------------------------------------------------------------------

_VALID_URL = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"


@pytest.fixture
def sheet_connector():
    from app.services.google.sheets_connector import GoogleSheetConnector

    return GoogleSheetConnector()


def test_google_sheet_connector_is_registered():
    # importing the module self-registers the connector under "google_sheet"
    import app.services.google.sheets_connector  # noqa: F401

    assert get_connector("google_sheet") is not None


@pytest.mark.asyncio
async def test_fetch_uses_key_as_tab(monkeypatch, sheet_connector):
    captured: Dict[str, Any] = {}

    async def _fake_fetch_formatted(*, spreadsheet_id, sheet_name, columns, format):
        captured.update(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            columns=columns,
            format=format,
        )
        return "TAB-CONTENT"

    monkeypatch.setattr(
        "app.services.google.sheets_connector.fetch_formatted",
        _fake_fetch_formatted,
    )

    config = {"spreadsheet_url": _VALID_URL, "format": "csv"}
    result = await sheet_connector.fetch(config, key="INF-001")

    assert result == "TAB-CONTENT"
    assert captured["spreadsheet_id"] == "abc123"
    assert captured["sheet_name"] == "INF-001"  # key wins over config sheet_name
    assert captured["format"] == "csv"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_config_sheet_name_when_no_key(
    monkeypatch, sheet_connector
):
    captured: Dict[str, Any] = {}

    async def _fake_fetch_formatted(*, spreadsheet_id, sheet_name, columns, format):
        captured["sheet_name"] = sheet_name
        return "X"

    monkeypatch.setattr(
        "app.services.google.sheets_connector.fetch_formatted",
        _fake_fetch_formatted,
    )

    config = {"spreadsheet_url": _VALID_URL, "sheet_name": "Index"}
    await sheet_connector.fetch(config, key=None)
    assert captured["sheet_name"] == "Index"


@pytest.mark.asyncio
async def test_fetch_invalid_url_returns_unavailable(monkeypatch, sheet_connector):
    called = False

    async def _fake_fetch_formatted(**kwargs):
        nonlocal called
        called = True
        return "should-not-happen"

    monkeypatch.setattr(
        "app.services.google.sheets_connector.fetch_formatted",
        _fake_fetch_formatted,
    )

    result = await sheet_connector.fetch({"spreadsheet_url": "not-a-url"}, key="t")
    assert result == DATA_SOURCE_UNAVAILABLE
    assert called is False


@pytest.mark.asyncio
async def test_list_keys_returns_tabs(monkeypatch, sheet_connector):
    async def _fake_list_tabs(spreadsheet_id):
        assert spreadsheet_id == "abc123"
        return ["Index", "INF-001", "INF-002"]

    monkeypatch.setattr(
        "app.services.google.sheets_connector.list_tabs",
        _fake_list_tabs,
    )

    keys = await sheet_connector.list_keys({"spreadsheet_url": _VALID_URL})
    assert keys == ["Index", "INF-001", "INF-002"]


@pytest.mark.asyncio
async def test_list_keys_invalid_url_returns_empty(sheet_connector):
    keys = await sheet_connector.list_keys({"spreadsheet_url": "nope"})
    assert keys == []


def test_cache_key_is_keyed_and_stable(sheet_connector):
    config = {"spreadsheet_url": _VALID_URL, "format": "csv"}
    k1 = sheet_connector.cache_key(config, key="INF-001")
    k2 = sheet_connector.cache_key(config, key="INF-001")
    k3 = sheet_connector.cache_key(config, key="INF-002")
    assert k1 == k2  # stable
    assert k1 != k3  # keyed
    assert "abc123" in k1
    assert "INF-001" in k1


# ---------------------------------------------------------------------------
# DataSourceRef: type + mode + connector_config (backward compatible)
# ---------------------------------------------------------------------------


def _ref(**overrides):
    from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef

    base = {"name": "protocols", "spreadsheet_url": _VALID_URL}
    base.update(overrides)
    return DataSourceRef.model_validate(base)


def test_ref_defaults_to_google_sheet_eager():
    ref = _ref()
    assert ref.type == "google_sheet"
    assert ref.mode == "eager"  # Phase-1 behavior preserved


def test_ref_can_be_on_demand():
    ref = _ref(mode="on_demand")
    assert ref.mode == "on_demand"


def test_ref_connector_config_maps_flat_fields():
    ref = _ref(sheet_name="Index", columns=["a", "b"], format="csv")
    config = ref.connector_config()
    assert config == {
        "spreadsheet_url": _VALID_URL,
        "sheet_name": "Index",
        "columns": ["a", "b"],
        "format": "csv",
    }


# ---------------------------------------------------------------------------
# load_data_source builtin (on-demand keyed loading)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.setex_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, value, ttl_seconds=None):
        self.setex_calls.append((key, value, ttl_seconds))
        self.store[key] = value
        return True


class _FakeCtx:
    """Duck-typed stand-in for TemplateContext."""

    def __init__(self, data_sources):
        template = type("T", (), {"data_sources": data_sources})()
        self.bot = type("B", (), {"template": template})()
        self.call_sid = "test-sid"


def _ctx(data_sources) -> "TemplateContext":
    """Build a duck-typed context typed as TemplateContext for the handler."""
    return cast("TemplateContext", _FakeCtx(data_sources))


def _on_demand_ref(name="protocols", type_="fake_src"):
    from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef

    return DataSourceRef.model_validate(
        {
            "name": name,
            "type": type_,
            "mode": "on_demand",
            "spreadsheet_url": _VALID_URL,
        }
    )


@pytest.fixture
def patched_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source"
        ".get_redis_service",
        _get_redis,
    )
    return fake


@pytest.mark.asyncio
async def test_load_data_source_fetches_on_cache_miss(patched_redis):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    register_connector("fake_src", _FakeConnector(content="PROTOCOL-DATA"))
    ctx = _ctx([_on_demand_ref()])

    result = await load_data_source(ctx, {"source": "protocols", "key": "INF-001"})

    assert result["status"] == "success"
    assert result["content"] == "PROTOCOL-DATA"
    assert result["key"] == "INF-001"
    # fetched value was written back to cache
    assert patched_redis.setex_calls, "expected fetched content to be cached"


@pytest.mark.asyncio
async def test_load_data_source_uses_cache_when_present(patched_redis, monkeypatch):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    connector = _FakeConnector(content="FROM-CONNECTOR")
    register_connector("fake_src", connector)
    ctx = _ctx([_on_demand_ref()])

    # Pre-seed the cache under the connector's key so fetch must NOT be called.
    key = connector.cache_key({}, "INF-001")
    patched_redis.store[key] = "FROM-CACHE"

    called = {"fetch": False}
    orig_fetch = connector.fetch

    async def _spy_fetch(config, k):
        called["fetch"] = True
        return await orig_fetch(config, k)

    monkeypatch.setattr(connector, "fetch", _spy_fetch)

    result = await load_data_source(ctx, {"source": "protocols", "key": "INF-001"})

    assert result["content"] == "FROM-CACHE"
    assert called["fetch"] is False


@pytest.mark.asyncio
async def test_load_data_source_unknown_source_returns_error(patched_redis):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    ctx = _ctx([_on_demand_ref(name="protocols")])
    result = await load_data_source(ctx, {"source": "nope", "key": "x"})
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_load_data_source_unknown_type_returns_error(patched_redis):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    ctx = _ctx([_on_demand_ref(type_="no_such_connector")])
    result = await load_data_source(ctx, {"source": "protocols", "key": "x"})
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_load_data_source_fails_open_on_connector_error(patched_redis):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    class _BoomConnector(_FakeConnector):
        async def fetch(self, config, key):
            raise RuntimeError("network down")

    register_connector("fake_src", _BoomConnector())
    ctx = _ctx([_on_demand_ref()])

    result = await load_data_source(ctx, {"source": "protocols", "key": "INF-001"})
    assert result["status"] == "unavailable"
    assert result["content"] == DATA_SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_load_data_source_unavailable_content_reported(patched_redis):
    from app.ai.voice.agents.breeze_buddy.handlers.internal.load_data_source import (
        load_data_source,
    )

    register_connector("fake_src", _FakeConnector(content=DATA_SOURCE_UNAVAILABLE))
    ctx = _ctx([_on_demand_ref()])

    result = await load_data_source(ctx, {"source": "protocols", "key": "INF-001"})
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_load_data_source_registered_in_dispatcher():
    from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
        BUILTIN_HANDLERS,
    )

    assert "load_data_source" in BUILTIN_HANDLERS


# ---------------------------------------------------------------------------
# eager-injection filter: on_demand sources are excluded from static injection
# ---------------------------------------------------------------------------


def test_eager_data_sources_excludes_on_demand():
    from app.ai.voice.agents.breeze_buddy.template.loader import eager_data_sources

    eager = _ref(name="catalog")  # default mode == eager
    on_demand = _on_demand_ref(name="protocols", type_="google_sheet")

    selected = eager_data_sources([eager, on_demand])
    names = [r.name for r in selected]
    assert names == ["catalog"]


def test_eager_data_sources_handles_none():
    from app.ai.voice.agents.breeze_buddy.template.loader import eager_data_sources

    assert eager_data_sources(None) == []


# ---------------------------------------------------------------------------
# prefetch: on_demand sources warm every tab (so first mid-call load is a hit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_on_demand_caches_every_key(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.managers import data_source_prefetch as mod

    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(mod, "get_redis_service", _get_redis)
    register_connector("fake_src", _FakeConnector(content="DATA", keys=["A", "B", "C"]))

    await mod._prefetch_on_demand(_on_demand_ref(type_="fake_src"))

    cached_keys = {call[0] for call in fake.setex_calls}
    assert cached_keys == {"fake:A", "fake:B", "fake:C"}


@pytest.mark.asyncio
async def test_prefetch_routes_eager_and_on_demand(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.managers import data_source_prefetch as mod

    seen = {"eager": [], "on_demand": []}

    async def _fake_eager(ref):
        seen["eager"].append(ref.name)

    async def _fake_on_demand(ref):
        seen["on_demand"].append(ref.name)

    monkeypatch.setattr(mod, "_prefetch_one", _fake_eager)
    monkeypatch.setattr(mod, "_prefetch_on_demand", _fake_on_demand)

    template = type(
        "T",
        (),
        {"data_sources": [_ref(name="catalog"), _on_demand_ref(name="protocols")]},
    )()

    await mod.prefetch_data_sources(template)

    assert seen["eager"] == ["catalog"]
    assert seen["on_demand"] == ["protocols"]


# ---------------------------------------------------------------------------
# review fixes: byte cap, fail-open on None url, eager routes via connector
# ---------------------------------------------------------------------------


def test_within_cache_limit():
    from app.services.data_sources import (
        MAX_DATA_SOURCE_CACHE_BYTES,
        within_cache_limit,
    )

    assert within_cache_limit("small") is True
    assert within_cache_limit("x" * MAX_DATA_SOURCE_CACHE_BYTES) is True
    assert within_cache_limit("x" * (MAX_DATA_SOURCE_CACHE_BYTES + 1)) is False


@pytest.mark.asyncio
async def test_fetch_none_url_fails_open(sheet_connector):
    # spreadsheet_url present but None must not crash extract_spreadsheet_id.
    result = await sheet_connector.fetch({"spreadsheet_url": None}, key="t")
    assert result == DATA_SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_oversized_slice_is_not_cached(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.managers import data_source_prefetch as mod
    from app.services.data_sources import MAX_DATA_SOURCE_CACHE_BYTES

    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(mod, "get_redis_service", _get_redis)
    huge = "x" * (MAX_DATA_SOURCE_CACHE_BYTES + 1)
    register_connector("fake_src", _FakeConnector(content=huge))

    await mod._prefetch_on_demand(_on_demand_ref(type_="fake_src"))

    assert fake.setex_calls == []  # too large → never cached


@pytest.mark.asyncio
async def test_prefetch_one_eager_routes_through_connector(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.managers import data_source_prefetch as mod

    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(mod, "get_redis_service", _get_redis)
    register_connector("fake_src", _FakeConnector(content="EAGER-DATA"))

    # eager ref (default mode) of a non-google_sheet type still works.
    await mod._prefetch_one(_ref(type="fake_src"))

    cached_keys = {call[0] for call in fake.setex_calls}
    assert cached_keys == {"fake:None"}  # connector.cache_key(config, None)
