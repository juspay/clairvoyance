# tests/test_tts_catalog_reconcile.py
# JWT env bootstrap lives in tests/conftest.py — see the note there.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.tts.preview import PreviewGenerationError, content_key
from app.api.routers.breeze_buddy.tts_catalog import handlers, router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.tts_catalog import CatalogVoiceEntry

ADMIN_USER = UserInfo(id="admin-1", username="admin", role=UserRole.ADMIN)
NON_ADMIN_USER = UserInfo(id="merchant-1", username="merchant1", role=UserRole.MERCHANT)


async def fake_resolve_preview_config(provider, voice_id, model, language, style):
    """Pure stand-in for resolve_preview_config — no dynamic-config lookups."""
    return {
        "provider": provider,
        "voice_id": voice_id,
        "model": model,
        "language": language,
        "style_params": style or {},
        "format": "wav",
    }


CURRENT_KEY = content_key(
    {
        "provider": "elevenlabs",
        "voice_id": "current",
        "model": "m1",
        "language": "en",
        "style_params": {},
        "format": "wav",
    }
)

VOICES = [
    CatalogVoiceEntry(
        provider="elevenlabs",
        voice_id="current",
        display_name="Current",
        models=["m1"],
        languages=["en"],
    ),
    CatalogVoiceEntry(
        provider="elevenlabs",
        voice_id="missing",
        display_name="Missing",
        models=["m1"],
        languages=["en"],
    ),
    CatalogVoiceEntry(
        provider="elevenlabs",
        voice_id="broken",
        display_name="Broken",
        models=["m1"],
        languages=["en"],
    ),
    # Has a working preview under a STALE content key, and regeneration fails:
    # the last-known-good entry must survive.
    CatalogVoiceEntry(
        provider="elevenlabs",
        voice_id="degraded",
        display_name="Degraded",
        models=["m1"],
        languages=["en"],
    ),
]

MANIFEST = {
    "elevenlabs/current": [
        {
            "language": "en",
            "url": "https://p/current.wav",
            "content_key": CURRENT_KEY,
            "format": "wav",
        }
    ],
    "elevenlabs/degraded": [
        {
            "language": "en",
            "url": "https://p/degraded-old.wav",
            "content_key": "stale-key",
            "format": "wav",
        }
    ],
}


@pytest.fixture()
def updates(monkeypatch):
    """Captures every update_previews(provider, voice_id, previews) call."""
    captured: dict[tuple[str, str], list[dict]] = {}

    async def fake_load_manifest_fresh():
        return dict(MANIFEST)

    async def fake_generate_preview(config):
        if config["voice_id"] in ("broken", "degraded"):
            raise PreviewGenerationError(
                config["provider"], config["voice_id"], "synth failed"
            )
        return b"RIFF-fake-wav", content_key(config)

    async def fake_store_preview(provider, voice_id, language, key, wav):
        return f"https://p/{voice_id}.wav"

    async def fake_update_previews(provider, voice_id, previews):
        captured[(provider, voice_id)] = previews

    monkeypatch.setattr(handlers, "get_enabled_voices", lambda: VOICES)
    monkeypatch.setattr(handlers, "load_manifest_fresh", fake_load_manifest_fresh)
    monkeypatch.setattr(handlers, "resolve_preview_config", fake_resolve_preview_config)
    monkeypatch.setattr(handlers, "generate_preview", fake_generate_preview)
    monkeypatch.setattr(handlers, "store_preview", fake_store_preview)
    monkeypatch.setattr(handlers, "update_previews", fake_update_previews)
    monkeypatch.setattr(handlers, "RECONCILE_DELAY_SECS", 0)
    return captured


@pytest.fixture()
def client_with_fakes(updates):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: ADMIN_USER
    client = TestClient(app)
    client.updates = updates  # type: ignore[attr-defined]
    return client


def test_reconcile_actions(client_with_fakes):
    r = client_with_fakes.post("/tts/voices/reconcile")
    assert r.status_code == 200
    actions = {(x["voice_id"], x["language"]): x["action"] for x in r.json()["report"]}
    assert actions[("current", "en")] == "skipped"
    assert actions[("missing", "en")] == "generated"
    assert actions[("broken", "en")] == "failed"


def test_reconcile_skip_leaves_previews_unpersisted(client_with_fakes):
    client_with_fakes.post("/tts/voices/reconcile")
    assert ("elevenlabs", "current") not in client_with_fakes.updates


def test_reconcile_generated_persists_content_key_and_url(client_with_fakes):
    client_with_fakes.post("/tts/voices/reconcile")
    previews = client_with_fakes.updates[("elevenlabs", "missing")]
    entry = next(p for p in previews if p["language"] == "en")
    assert entry["url"] == "https://p/missing.wav"
    assert entry["content_key"]
    assert entry["format"] == "wav"


def test_reconcile_failed_persists_error_entry(client_with_fakes):
    r = client_with_fakes.post("/tts/voices/reconcile")
    broken_report = next(x for x in r.json()["report"] if x["voice_id"] == "broken")
    assert broken_report["action"] == "failed"
    assert "error" in broken_report and broken_report["error"]

    previews = client_with_fakes.updates[("elevenlabs", "broken")]
    entry = next(p for p in previews if p["language"] == "en")
    assert "error" in entry
    assert entry["content_key"]
    assert "url" not in entry


def test_reconcile_manifest_error_carries_no_provider_message(client_with_fakes):
    """The manifest is world-readable, so a failed entry may record only the
    exception's class name. The provider's message stays in the admin-only
    report (and the server log), never in storage."""
    r = client_with_fakes.post("/tts/voices/reconcile")

    broken_report = next(x for x in r.json()["report"] if x["voice_id"] == "broken")
    assert "synth failed" in broken_report["error"]  # admin caller keeps detail

    entry = next(
        p
        for p in client_with_fakes.updates[("elevenlabs", "broken")]
        if p["language"] == "en"
    )
    assert entry["error"] == "PreviewGenerationError"
    assert "synth failed" not in entry["error"]


def test_reconcile_failure_preserves_last_known_good_preview(client_with_fakes):
    """A failed regeneration must not evict a previously working preview:
    the stale-key entry stays (so GET keeps serving the old audio) and the
    stale key makes the next reconcile retry."""
    r = client_with_fakes.post("/tts/voices/reconcile")
    rep = next(x for x in r.json()["report"] if x["voice_id"] == "degraded")
    assert rep["action"] == "failed"
    # nothing persisted for this voice — the manifest entry is untouched
    assert ("elevenlabs", "degraded") not in client_with_fakes.updates


def test_reconcile_manifest_write_failure_is_isolated(updates, monkeypatch):
    """A failing manifest write for one voice is reported, not raised — the
    remaining voices still reconcile."""

    async def failing_update_previews(provider, voice_id, previews):
        if voice_id == "missing":
            raise RuntimeError("gcs down")
        updates[(provider, voice_id)] = previews

    monkeypatch.setattr(handlers, "update_previews", failing_update_previews)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: ADMIN_USER
    client = TestClient(app)

    r = client.post("/tts/voices/reconcile")
    assert r.status_code == 200
    failures = [x for x in r.json()["report"] if x["action"] == "manifest_write_failed"]
    assert [f["voice_id"] for f in failures] == ["missing"]
    # the voice after the failing one still got persisted
    assert ("elevenlabs", "broken") in updates


def test_reconcile_requires_admin():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: NON_ADMIN_USER
    client = TestClient(app)
    r = client.post("/tts/voices/reconcile")
    assert r.status_code == 403
