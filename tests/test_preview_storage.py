"""Preview storage tests.

GCS is the only backend, so these run against an in-memory stub bucket rather
than a temp directory. The stub records blob paths/content types and keeps
bytes in a dict, which is enough to exercise URL construction, manifest
round-tripping, corruption self-healing and the fail-soft/fail-hard split.
"""

import pytest

from app.services import preview_storage

BUCKET = "my-preview-bucket"


class _StubBlob:
    def __init__(self, store: dict, path: str, calls: dict):
        self._store = store
        self._path = path
        self._calls = calls

    def upload_from_string(self, data, content_type=None):
        self._store[self._path] = (
            data if isinstance(data, bytes) else str(data).encode("utf-8")
        )
        self._calls["path"] = self._path
        self._calls["data"] = self._store[self._path]
        self._calls["content_type"] = content_type

    def exists(self):
        return self._path in self._store

    def download_as_bytes(self):
        return self._store[self._path]


class _StubBucket:
    def __init__(self, store: dict, calls: dict):
        self._store = store
        self._calls = calls

    def blob(self, path):
        return _StubBlob(self._store, path, self._calls)


@pytest.fixture()
def gcs(monkeypatch):
    """In-memory GCS: returns the blob store plus a record of the last call."""
    store: dict = {}
    calls: dict = {}

    def _get_bucket(bucket_name):
        calls["bucket_name"] = bucket_name
        return _StubBucket(store, calls)

    monkeypatch.setattr(preview_storage, "get_gcs_bucket", _get_bucket)
    monkeypatch.setattr(preview_storage, "TTS_PREVIEW_GCS_BUCKET", BUCKET)
    monkeypatch.setattr(preview_storage, "TTS_PREVIEW_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(preview_storage, "_manifest_cache", None)
    return {"store": store, "calls": calls}


# ── URL construction ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_store_returns_bucket_public_url_by_default(gcs):
    """With no CDN override the URL points straight at the bucket — previews
    are served by GCS, not by this app."""
    url = await preview_storage.store_preview(
        "elevenlabs", "v9", "hi", "abc", b"RIFFdata"
    )
    assert url == (
        f"https://storage.googleapis.com/{BUCKET}/tts-previews/elevenlabs/v9/hi-abc.wav"
    )
    assert gcs["calls"]["bucket_name"] == BUCKET
    assert gcs["calls"]["path"] == "tts-previews/elevenlabs/v9/hi-abc.wav"
    assert gcs["calls"]["data"] == b"RIFFdata"
    assert gcs["calls"]["content_type"] == "audio/wav"


@pytest.mark.asyncio
async def test_public_base_url_overrides_bucket_url(gcs, monkeypatch):
    """Deployments fronting the bucket with a CDN/custom domain override the
    host; the object path is unchanged."""
    monkeypatch.setattr(
        preview_storage, "TTS_PREVIEW_PUBLIC_BASE_URL", "https://cdn.example.com"
    )
    url = await preview_storage.store_preview(
        "cartesia", "v1", "en", "k123", b"RIFFdata"
    )
    assert url == "https://cdn.example.com/tts-previews/cartesia/v1/en-k123.wav"


@pytest.mark.asyncio
async def test_missing_bucket_raises_naming_the_variable(gcs, monkeypatch):
    """Unconfigured previews must fail with an actionable message rather than
    a generic GCS error — config is validated lazily so unrelated deployments
    still boot."""
    monkeypatch.setattr(preview_storage, "TTS_PREVIEW_GCS_BUCKET", "")
    with pytest.raises(RuntimeError, match="TTS_PREVIEW_GCS_BUCKET"):
        await preview_storage.store_preview("cartesia", "v1", "en", "k", b"x")


@pytest.mark.asyncio
async def test_non_url_safe_bucket_rejected(gcs, monkeypatch):
    """The bucket name is interpolated into the public URL unencoded. A name
    outside [A-Za-z0-9._-] must fail loudly here — percent-encoding it would
    turn a misconfiguration into a well-formed URL that 404s at GCS."""
    monkeypatch.setattr(preview_storage, "TTS_PREVIEW_GCS_BUCKET", "bad bucket/../x")
    with pytest.raises(RuntimeError, match="not a valid GCS bucket name"):
        await preview_storage.store_preview("cartesia", "v1", "en", "k", b"x")
    assert gcs["store"] == {}


@pytest.mark.asyncio
async def test_path_traversal_component_rejected(gcs):
    with pytest.raises(ValueError):
        await preview_storage.store_preview("cartesia", "../evil", "en", "k", b"x")
    assert gcs["store"] == {}


# ── manifest ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_manifest_missing_reads_as_empty(gcs):
    assert await preview_storage.load_manifest() == {}


@pytest.mark.asyncio
async def test_update_previews_round_trips_through_manifest(gcs):
    entries = [
        {"language": "en", "url": "https://cdn/x.wav", "content_key": "k1"},
    ]
    await preview_storage.update_previews("cartesia", "v1", entries)
    assert await preview_storage.load_manifest() == {"cartesia/v1": entries}
    # persisted, not just cached: a fresh read from the backend sees it too
    assert (await preview_storage.load_manifest_fresh()) == {"cartesia/v1": entries}


@pytest.mark.asyncio
async def test_update_previews_composes_across_voices(gcs):
    """Sequential updates read-modify-write fresh — the second voice must not
    clobber the first."""
    await preview_storage.update_previews("cartesia", "v1", [{"language": "en"}])
    await preview_storage.update_previews("sarvam", "shreya", [{"language": "hi"}])
    manifest = await preview_storage.load_manifest_fresh()
    assert set(manifest) == {"cartesia/v1", "sarvam/shreya"}


@pytest.mark.asyncio
async def test_load_manifest_fails_soft_but_update_fails_hard(gcs, monkeypatch):
    """GET path degrades to {} on storage errors; the reconcile write path
    must abort instead of overwriting good state with a partial manifest."""

    async def boom():
        raise RuntimeError("storage down")

    monkeypatch.setattr(preview_storage, "load_manifest_fresh", boom)
    assert await preview_storage.load_manifest() == {}
    with pytest.raises(RuntimeError):
        await preview_storage.update_previews("cartesia", "v1", [])


@pytest.mark.asyncio
async def test_corrupt_manifest_self_heals(gcs):
    """A manifest that exists but doesn't parse loads as {} (logged) instead
    of raising — so reconcile can regenerate and its successful write repairs
    the file, rather than every future write failing forever."""
    gcs["store"]["tts-previews/manifest.json"] = b"{not json"
    assert await preview_storage.load_manifest_fresh() == {}

    await preview_storage.update_previews("cartesia", "v1", [{"language": "en"}])
    assert await preview_storage.load_manifest_fresh() == {
        "cartesia/v1": [{"language": "en"}]
    }
