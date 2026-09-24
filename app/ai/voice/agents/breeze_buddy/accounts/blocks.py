"""The template side of the vocabulary: which service a block configures,
which vendor its provider word names (each service owns its words: llm.py,
stt.py, tts.py), and every block of a template that may name an account."""

from __future__ import annotations

from typing import Any, Iterator, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.accounts.llm import LLM_VENDOR, REALTIME_VENDOR
from app.ai.voice.agents.breeze_buddy.accounts.stt import STT_VENDOR
from app.ai.voice.agents.breeze_buddy.accounts.tts import TTS_VENDOR
from app.ai.voice.agents.breeze_buddy.template.types import STTConfiguration, TTSConfig
from app.ai.voice.llm.types import LLMConfiguration, RealtimeConfig


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
    if kind == "stt":
        return STT_VENDOR.get(str(word)) or f"stt:{word}"
    return TTS_VENDOR.get(str(word)) or f"tts:{word}"


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
