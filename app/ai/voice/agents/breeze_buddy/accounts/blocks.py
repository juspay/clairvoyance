"""The template side of the vocabulary: which service a block configures,
which vendor its provider word names, and every block of a template that
may name an account."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.types import (
    STTConfiguration,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.llm.types import LLMConfiguration, RealtimeConfig

# Which vendor each block's provider word names.
LLM_VENDOR: Dict[str, str] = {
    "azure": "azure_openai",
    "openai": "openai",
    "google_vertex": "google_vertex",
    "aws_bedrock": "aws_bedrock",
}
REALTIME_VENDOR: Dict[str, str] = {
    "openai": "openai_realtime",
    "xai": "xai_realtime",
    "azure": "azure_openai_realtime",
    "gemini": "gemini",
}
# STT and TTS provider words already ARE the vendor names — except the
# Google speech services and Gemini TTS, which all run on one service
# account. DragonTTS is a proxy over a nested provider and is unwrapped
# BEFORE a block reaches here (unwrap_dragontts).
SPEECH_VENDOR: Dict[str, str] = {
    "deepgram": "deepgram",
    "soniox": "soniox",
    "sarvam": "sarvam",
    "assemblyai": "assemblyai",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
    "openai": "openai",
    "google": "google",
    "gemini": "google",
}


class AccountRefused(ValueError):
    """The block names an account it may not use, one that cannot serve the
    provider it names, or an environment that has no account for it. Fail
    closed: no service is built on it."""


def _word(value: Any) -> Optional[str]:
    return getattr(value, "value", value)


def kind_of(block: Any) -> str:
    """Which service a block configures: llm · realtime · stt · tts."""
    if isinstance(block, RealtimeConfig):
        return "realtime"
    if isinstance(block, LLMConfiguration):
        return "llm"
    if isinstance(block, STTConfiguration):
        return "stt"
    if isinstance(block, TTSConfig):
        return "tts"
    raise TypeError(f"not a provider block: {type(block).__name__}")


def vendor_of(block: Any) -> str:
    """PURE: the vendor a block's provider word names. An unknown word is
    named ``<kind>:<word>`` so a refusal can say so; no block = Azure,
    today's default for the text LLM."""
    kind = kind_of(block)
    word = _word(getattr(block, "provider", None))
    if kind == "llm":
        word = word or "azure"
        return LLM_VENDOR.get(word) or f"llm:{word}"
    if kind == "realtime":
        return REALTIME_VENDOR.get(str(word)) or f"realtime:{word}"
    return SPEECH_VENDOR.get(str(word)) or f"{kind}:{word}"


def unwrap_dragontts(voice: TTSConfig) -> TTSConfig:
    """PURE: a DragonTTS voice WITH an account is synthesized by its nested
    provider directly (the proxy holds its own keys and would bill its own
    account), so the block becomes the nested provider's — once, here, and
    the account is checked against the provider that really synthesizes.
    A DragonTTS voice without an account is left alone: the proxy path."""
    if _word(voice.provider) != TTSProvider.DRAGONTTS.value or not voice.credential_id:
        return voice
    nested, sep, model = (voice.model or "").partition(":")
    if not sep or not nested or not model:
        raise AccountRefused(
            "dragontts with a credential_id requires model '<provider>:<model>' "
            f"on the block, got {voice.model!r}"
        )
    try:
        provider = TTSProvider(nested)
    except ValueError as e:
        raise AccountRefused(f"dragontts nests an unknown provider {nested!r}") from e
    return voice.model_copy(update={"provider": provider, "model": model})


def account_blocks(configurations: Any) -> Iterator[Tuple[str, Any]]:
    """Every block of a template that may name an account, by name — the
    one walk the save-time check and the observers share. An observer's
    block with no provider of its own takes the template's."""
    if configurations is None:
        return
    llm = getattr(configurations, "llm_configurations", None)
    if llm is not None:
        yield "llm_configurations", llm
        realtime = getattr(llm, "realtime", None)
        if realtime is not None:
            yield "llm_configurations.realtime", realtime
    stt = getattr(configurations, "stt_configuration", None)
    if stt is not None:
        yield "stt_configuration", stt
    tts = getattr(configurations, "tts_configuration", None)
    if tts is not None:
        yield "tts_configuration", tts
    for key, override in (
        getattr(configurations, "tts_configuration_overrides", None) or {}
    ).items():
        yield f"tts_configuration_overrides.{key}", override
    base_provider = getattr(llm, "provider", None) if llm is not None else None
    for i, observer in enumerate(getattr(configurations, "observers", None) or []):
        obs_llm = getattr(observer, "llm", None)
        if obs_llm is None:
            continue
        if obs_llm.provider is None and base_provider is not None:
            obs_llm = obs_llm.model_copy(update={"provider": base_provider})
        yield f"observers[{i}].llm", obs_llm
