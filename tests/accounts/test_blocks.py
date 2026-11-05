"""Phase 2 of provider accounts (docs/PROVIDER_CREDENTIALS.md): the words on
a block, and the three block laws."""

import pytest

from app.ai.voice.agents.breeze_buddy.accounts import (
    AccountRefused,
    kind_of,
    unwrap_dragontts,
    vendor_of,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    STTConfiguration,
    TTSConfig,
)
from app.ai.voice.llm.types import LLMConfiguration, RealtimeConfig
from tests.accounts.conftest import ROW


def test_each_block_names_the_vendor_its_provider_word_means() -> None:
    assert vendor_of(LLMConfiguration()) == "azure_openai"
    assert vendor_of(LLMConfiguration(provider="google_vertex")) == "google_vertex"
    assert vendor_of(LLMConfiguration(provider="aws_bedrock")) == "aws_bedrock"
    assert vendor_of(RealtimeConfig(provider="azure")) == "azure_openai_realtime"
    assert vendor_of(STTConfiguration(provider="deepgram")) == "deepgram"
    assert vendor_of(TTSConfig(provider="gemini")) == "google"
    assert vendor_of(TTSConfig(provider="dragontts")) == "tts:dragontts"
    assert kind_of(RealtimeConfig(provider="openai")) == "realtime"


def test_a_dragontts_voice_with_an_account_is_its_nested_providers_voice() -> None:
    voice = TTSConfig(
        provider="dragontts", model="elevenlabs:eleven_flash_v2_5", credential_id=ROW
    )
    nested = unwrap_dragontts(voice)
    assert (nested.provider.value, nested.model) == ("elevenlabs", "eleven_flash_v2_5")
    plain = TTSConfig(provider="dragontts", model="cartesia:sonic-3")
    assert unwrap_dragontts(plain) is plain  # no account: the proxy path
    with pytest.raises(AccountRefused):
        unwrap_dragontts(TTSConfig(provider="dragontts", credential_id=ROW))


def test_the_blocks_carry_the_reference_in_one_spelling_and_never_an_endpoint():
    """Canonical credential_id at the edge, so an exact SQL match never misses
    an uppercase or hyphen-less spelling; and the endpoint law on the block."""
    block = LLMConfiguration(provider="azure", credential_id=ROW.upper())
    assert block.credential_id == ROW
    assert (
        TTSConfig(provider="cartesia", credential_id=ROW.replace("-", "")).credential_id
        == ROW
    )
    with pytest.raises(ValueError):
        LLMConfiguration(provider="azure", credential_id="not-a-uuid")
    with pytest.raises(ValueError):
        LLMConfiguration(provider="azure", credential_id=ROW, endpoint="https://x")
    with pytest.raises(ValueError):
        RealtimeConfig(provider="azure", credential_id=ROW, endpoint="wss://x")
    with pytest.raises(ValueError):
        LLMConfiguration(provider="google_vertex", region="attacker.example#")
    assert LLMConfiguration.__doc__ and RealtimeConfig.__doc__
