"""Regression coverage for STT per-provider config resolution across the
config_resolver migration.

Soniox: template (truthy-checked) > static env default.
Sarvam: template (truthy-checked) > Redis dynamic default (no Redis running
in unit tests, so dynamic resolves to its hardcoded default).
Empty-string template overrides must still fall through to the next tier
(the shared `or_none` truthy-preservation helper).
"""

import pytest

import app.ai.voice.agents.breeze_buddy.stt as stt_mod
from app.ai.voice.agents.breeze_buddy.template.types import (
    SarvamSTTConfig,
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
)
from app.core.config.resolver import or_none


@pytest.fixture(autouse=True)
def _fake_api_keys(monkeypatch):
    monkeypatch.setattr(stt_mod, "SONIOX_API_KEY", "test-soniox-key")
    monkeypatch.setattr(stt_mod, "SARVAM_API_KEY", "test-sarvam-key")
    monkeypatch.setattr(stt_mod, "DEEPGRAM_API_KEY", "test-deepgram-key")


@pytest.mark.asyncio
async def test_soniox_no_template_override_uses_static_defaults():
    config = STTConfiguration(provider=STTProvider.SONIOX)
    service = await stt_mod.create_stt_from_config(config)
    assert service is not None


@pytest.mark.asyncio
async def test_soniox_template_override_wins():
    soniox = SonioxSTTConfig(context="custom-context", model="stt-rt-v9")
    config = STTConfiguration(provider=STTProvider.SONIOX, soniox=soniox)
    resolved = await stt_mod.resolve_fields(
        [
            stt_mod.FieldSpec(
                "context",
                tiers=[
                    lambda: or_none(soniox.context),
                    lambda: stt_mod.BREEZE_BUDDY_SONIOX_CONTEXT,
                ],
            ),
            stt_mod.FieldSpec(
                "model",
                tiers=[
                    lambda: or_none(soniox.model),
                    lambda: stt_mod.BREEZE_BUDDY_SONIOX_MODEL,
                ],
            ),
        ]
    )
    assert resolved["context"] == "custom-context"
    assert resolved["model"] == "stt-rt-v9"


def test_or_none_preserves_truthy_semantics():
    # Empty string must fall through (truthy check), unlike `is not None`.
    assert or_none("") is None
    assert or_none(None) is None
    assert or_none("value") == "value"
    assert or_none(0) is None


@pytest.mark.asyncio
async def test_soniox_empty_string_override_falls_through_to_static_default():
    soniox = SonioxSTTConfig(context="")
    config = STTConfiguration(provider=STTProvider.SONIOX, soniox=soniox)
    resolved = await stt_mod.resolve_fields(
        [
            stt_mod.FieldSpec(
                "context",
                tiers=[
                    lambda: or_none(soniox.context),
                    lambda: stt_mod.BREEZE_BUDDY_SONIOX_CONTEXT,
                ],
            )
        ]
    )
    assert resolved["context"] == stt_mod.BREEZE_BUDDY_SONIOX_CONTEXT


@pytest.mark.asyncio
async def test_sarvam_no_template_override_uses_dynamic_defaults():
    config = STTConfiguration(provider=STTProvider.SARVAM)
    service = await stt_mod.create_stt_from_config(config)
    assert service is not None


@pytest.mark.asyncio
async def test_sarvam_template_override_wins_over_dynamic_default():
    sarvam = SarvamSTTConfig(model="saaras:v9", language_code="hi-IN")
    config = STTConfiguration(provider=STTProvider.SARVAM, sarvam=sarvam)
    resolved = await stt_mod.resolve_fields(
        [
            stt_mod.FieldSpec(
                "model",
                tiers=[
                    lambda: or_none(sarvam.model),
                    stt_mod.BB_SARVAM_STT_MODEL,
                ],
            ),
            stt_mod.FieldSpec(
                "language_code",
                tiers=[
                    lambda: or_none(sarvam.language_code),
                    stt_mod.BB_SARVAM_STT_LANGUAGE_CODE,
                ],
            ),
        ]
    )
    assert resolved["model"] == "saaras:v9"
    assert resolved["language_code"] == "hi-IN"


@pytest.mark.asyncio
async def test_deepgram_uses_whole_object_defaults_no_resolver():
    config = STTConfiguration(provider=STTProvider.DEEPGRAM)
    service = await stt_mod.create_stt_from_config(config)
    assert service is not None


@pytest.mark.asyncio
async def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(stt_mod, "SONIOX_API_KEY", None)
    config = STTConfiguration(provider=STTProvider.SONIOX)
    with pytest.raises(ValueError, match="SONIOX_API_KEY"):
        await stt_mod.create_stt_from_config(config)
