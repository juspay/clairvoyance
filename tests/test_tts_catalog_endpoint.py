# tests/test_tts_catalog_endpoint.py
# JWT env bootstrap lives in tests/conftest.py — it has to run before any test
# module imports `app.`, which a module-level setdefault here cannot guarantee.
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.breeze_buddy.tts_catalog import ProviderFilter, handlers, router
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas.breeze_buddy.tts_catalog import CATALOG_PROVIDERS, CatalogVoiceEntry

VOICES = [
    CatalogVoiceEntry(
        provider="elevenlabs",
        voice_id="fG9s0SXJb213f4UxVHyG",
        display_name="Rachel",
        models=["eleven_flash_v2_5"],
        languages=["en-IN", "hi"],
        tags=["female"],
    ),
    CatalogVoiceEntry(
        provider="gemini",
        voice_id="Kore",
        display_name="Kore",
        languages=[],
    ),
]

MANIFEST = {
    "elevenlabs/fG9s0SXJb213f4UxVHyG": [
        {
            "language": "en-IN",
            "url": "https://p/1.wav",
            "content_key": "k",
            "format": "wav",
        }
    ],
    "gemini/Kore": [{"language": "en", "error": "boom", "content_key": "k2"}],
}


@pytest.fixture()
def client(monkeypatch):
    async def fake_manifest():
        return MANIFEST

    async def fake_defaults(provider):
        return {"voice_id": "fG9s0SXJb213f4UxVHyG"} if provider == "elevenlabs" else {}

    monkeypatch.setattr(handlers, "get_enabled_voices", lambda: VOICES)
    monkeypatch.setattr(handlers, "load_manifest", fake_manifest)
    monkeypatch.setattr(handlers, "BB_VOICE_PROVIDER_DEFAULTS", fake_defaults)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: {"user": "t"}
    return TestClient(app)


def test_contract_shape_and_is_default(client):
    r = client.get("/tts/voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    el = next(v for v in voices if v["provider"] == "elevenlabs")
    assert el["is_default"] is True
    assert el["previews"] == [{"language": "en-IN", "url": "https://p/1.wav"}]
    assert "content_key" not in str(r.json())  # internal fields never leak


def test_provider_filter_rejects_dragontts(client):
    assert (
        client.get("/tts/voices", params={"provider": "dragontts"}).status_code == 422
    )


def test_language_filter_bare_code(client):
    voices = client.get("/tts/voices", params={"language": "en"}).json()["voices"]
    assert {v["provider"] for v in voices} == {
        "elevenlabs",
        "gemini",
    }  # gemini = agnostic
    voices = client.get("/tts/voices", params={"language": "ta"}).json()["voices"]
    assert {v["provider"] for v in voices} == {"gemini"}


def test_etag_304(client):
    r1 = client.get("/tts/voices")
    etag = r1.headers["etag"]
    assert r1.headers["cache-control"] == "private, max-age=300"
    r2 = client.get("/tts/voices", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_etag_differs_by_filter(client):
    r1 = client.get("/tts/voices")
    r2 = client.get("/tts/voices", params={"provider": "gemini"})
    assert r1.headers["etag"] != r2.headers["etag"]


def test_etag_304_respects_filter(client):
    r1 = client.get("/tts/voices", params={"provider": "gemini"})
    etag = r1.headers["etag"]
    r2 = client.get(
        "/tts/voices", params={"provider": "gemini"}, headers={"If-None-Match": etag}
    )
    assert r2.status_code == 304


def test_etag_changes_when_default_changes(client, monkeypatch):
    """A Redis default flip must invalidate cached representations — the ETag
    hashes the response content, which folds in is_default."""
    etag_before = client.get("/tts/voices").headers["etag"]

    async def other_defaults(provider):
        return {}  # elevenlabs default removed

    monkeypatch.setattr(handlers, "BB_VOICE_PROVIDER_DEFAULTS", other_defaults)
    etag_after = client.get("/tts/voices").headers["etag"]
    assert etag_before != etag_after


def test_error_only_previews_render_as_no_previews(client):
    r = client.get("/tts/voices", params={"provider": "gemini"})
    assert r.json()["voices"][0]["previews"] == []


def test_provider_filter_literal_matches_catalog_providers():
    assert set(get_args(ProviderFilter)) == set(CATALOG_PROVIDERS)
