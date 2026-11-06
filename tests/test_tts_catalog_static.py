# tests/test_tts_catalog_static.py — invariants of the checked-in catalog.
import pytest

from app.ai.voice.tts.catalog import get_all_voices, get_enabled_voices
from app.ai.voice.tts.preview import content_key, resolve_preview_config
from app.core.config.dynamic import BB_SPEECH_PROVIDER_DEFAULTS
from app.schemas.breeze_buddy.tts_catalog import CATALOG_PROVIDERS


def test_catalog_parses_and_validates():
    voices = get_all_voices()
    assert voices, "catalog.json must not be empty"
    assert all(v.provider in CATALOG_PROVIDERS for v in voices)


def test_catalog_pairs_unique():
    pairs = [(v.provider, v.voice_id) for v in get_all_voices()]
    assert len(pairs) == len(set(pairs))


def test_enabled_subset():
    assert {(v.provider, v.voice_id) for v in get_enabled_voices()} <= {
        (v.provider, v.voice_id) for v in get_all_voices()
    }


def test_catalog_covers_static_provider_defaults():
    """Every hardcoded default voice must exist in the catalog — otherwise the
    picker can't show the voice a template actually falls back to. (The old
    seed script guaranteed this at seed time; now it's a code invariant.)"""
    catalog_pairs = {(v.provider, v.voice_id) for v in get_enabled_voices()}
    for provider in CATALOG_PROVIDERS:
        default_voice = BB_SPEECH_PROVIDER_DEFAULTS.get(provider, {}).get("voice_id")
        if default_voice:
            assert (
                provider,
                default_voice,
            ) in catalog_pairs, (
                f"static default {provider}/{default_voice} missing from catalog.json"
            )
    # gemini has no BB_SPEECH_PROVIDER_DEFAULTS entry; the handler falls back
    # to "Kore", which must therefore exist too.
    assert ("gemini", "Kore") in catalog_pairs


def test_catalog_has_voices_without_an_explicit_model():
    """The `model or <default>` fallback in resolve_preview_config is live, not
    dead: catalog entries are allowed to omit `models`, and several do. This
    pins that so the fallback never becomes untested-and-unreachable without
    someone noticing."""
    without = [v for v in get_enabled_voices() if not v.models]
    assert without, (
        "no catalog voice omits `models` — if that is now intentional, the "
        "model-fallback branches in resolve_preview_config are dead and should "
        "be removed rather than left untested"
    )


@pytest.mark.asyncio
async def test_model_fallback_resolves_for_every_modelless_voice():
    """A voice with no `models` must still resolve to a complete, hashable
    preview config. Exercises the `model=None` path per provider."""
    for v in (v for v in get_enabled_voices() if not v.models):
        cfg = await resolve_preview_config(
            v.provider, v.voice_id, None, (v.languages or ["en"])[0], v.style_params
        )
        assert cfg["provider"] == v.provider
        assert cfg["voice_id"] == v.voice_id
        assert "model" in cfg, f"{v.provider}/{v.voice_id} resolved without a model key"
        # google encodes the model in the voice name, so None is correct there;
        # every other provider must have filled the fallback in.
        if v.provider != "google":
            assert cfg["model"], (
                f"{v.provider}/{v.voice_id} has no catalog model and the "
                "fallback produced nothing"
            )
        assert content_key(cfg), "resolved config must be hashable to a content key"
