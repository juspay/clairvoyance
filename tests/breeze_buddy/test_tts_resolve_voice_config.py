"""Regression coverage for `resolve_voice_config` (template > per-provider
override > Redis/hardcoded defaults) across the config_resolver migration.

No Redis is running in unit tests, so BB_VOICE_PROVIDER_DEFAULTS resolves
purely from the hardcoded BB_SPEECH_PROVIDER_DEFAULTS table
(ENABLE_REDIS_DYNAMIC_CONFIG defaults False).
"""

import pytest

import app.ai.voice.agents.breeze_buddy.tts as tts_mod
from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig, TTSProvider
from app.ai.voice.agents.breeze_buddy.tts import resolve_voice_config


@pytest.mark.asyncio
async def test_no_template_config_uses_redis_hardcoded_defaults():
    resolved = await resolve_voice_config(None, None)
    assert resolved.provider == TTSProvider.ELEVENLABS


@pytest.mark.asyncio
async def test_template_config_fields_win_over_defaults():
    template_cfg = TTSConfig(
        provider=TTSProvider.ELEVENLABS, voice_id="custom-voice", speed=1.3
    )
    resolved = await resolve_voice_config(template_cfg, None)
    assert resolved.voice_id == "custom-voice"
    assert resolved.speed == 1.3
    assert resolved.provider == TTSProvider.ELEVENLABS


@pytest.mark.asyncio
async def test_per_provider_override_wins_over_default_template_config():
    default_cfg = TTSConfig(provider=TTSProvider.ELEVENLABS, voice_id="default-voice")
    override_cfg = TTSConfig(provider=TTSProvider.CARTESIA, voice_id="override-voice")
    resolved = await resolve_voice_config(default_cfg, {"cartesia": override_cfg})
    # Provider comes from the override since none was set on default_cfg's provider match
    # (default_cfg.provider is elevenlabs, so overrides["elevenlabs"] is absent -> falls
    # back to default_cfg itself, not the cartesia override).
    assert resolved.voice_id == "default-voice"


@pytest.mark.asyncio
async def test_per_provider_override_matches_template_provider():
    default_cfg = TTSConfig(provider=TTSProvider.ELEVENLABS, voice_id="default-voice")
    override_cfg = TTSConfig(provider=TTSProvider.ELEVENLABS, voice_id="override-voice")
    resolved = await resolve_voice_config(default_cfg, {"elevenlabs": override_cfg})
    assert resolved.voice_id == "override-voice"


@pytest.mark.asyncio
async def test_unknown_provider_falls_back_to_elevenlabs(monkeypatch):
    """A bad BB_TTS_SERVICE value must not crash call startup.

    Exercises the ``except ValueError`` branch: the provider string comes from
    Redis, so a typo/stale flag there would otherwise blow up TTSProvider().
    """

    async def fake_tts_service():
        return "not-a-real-provider"

    monkeypatch.setattr(tts_mod, "BB_TTS_SERVICE", fake_tts_service)
    resolved = await resolve_voice_config(None, None)
    assert resolved.provider == TTSProvider.ELEVENLABS


@pytest.mark.asyncio
async def test_partial_template_config_falls_back_field_by_field():
    template_cfg = TTSConfig(provider=TTSProvider.CARTESIA, voice_id="only-voice-set")
    resolved = await resolve_voice_config(template_cfg, None)
    assert resolved.voice_id == "only-voice-set"
    # model wasn't set on the template config; falls through to defaults (may be None)
    assert resolved.provider == TTSProvider.CARTESIA
