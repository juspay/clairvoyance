"""The TTS side: which vendor a voice names, the environment's account for
it, the DragonTTS unwrap, and the host rule a row must obey on this service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.accounts.types import (
    Account,
    AccountRefused,
    GcpAccount,
    KeyAccount,
    KeyOnlyAccount,
)
from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig, TTSProvider
from app.core.config import static

# TTS provider words already ARE the vendor names — except Gemini TTS and the
# Google service, which both run on the one service account. DragonTTS is a
# proxy over a nested provider and is unwrapped BEFORE a voice reaches here
# (unwrap_dragontts).
TTS_VENDOR: Dict[str, str] = {
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
    "sarvam": "sarvam",
    "soniox": "soniox",
    "gemini": "google",
    "google": "google",
}


def _word(value: Any) -> Optional[str]:
    return getattr(value, "value", value)


def elevenlabs_host() -> str:
    """An ElevenLabs voice runs on the deployment's ``ELEVENLABS_TTS_URL``
    (a bare host; release cbcec339) — carried as the ``wss://`` URL the
    stream dials; the batch path dials it as ``https://``."""
    return f"wss://{static.ELEVENLABS_TTS_URL}"


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


async def env_account(vendor: str, block: Any) -> Account:
    """The environment's TTS account for a vendor. Raises AccountRefused,
    and only AccountRefused, when the environment has none."""
    if vendor == "elevenlabs":
        if not static.ELEVENLABS_TTS_API_KEY:
            raise AccountRefused(
                "ELEVENLABS_TTS_API_KEY is required for elevenlabs TTS"
            )
        return KeyAccount(
            api_key=str(static.ELEVENLABS_TTS_API_KEY), endpoint=elevenlabs_host()
        )
    if vendor == "google":
        if not static.GOOGLE_CREDENTIALS_JSON:
            raise AccountRefused("GOOGLE_CREDENTIALS_JSON is required for tts")
        return GcpAccount(credentials_json=str(static.GOOGLE_CREDENTIALS_JSON))
    env_keys = {
        "cartesia": static.CARTESIA_API_KEY,
        "sarvam": static.SARVAM_API_KEY,
        "soniox": static.SONIOX_API_KEY,
    }
    if vendor in env_keys:
        if not env_keys[vendor]:
            raise AccountRefused(
                f"{vendor.upper()}_API_KEY is required for {vendor} TTS"
            )
        return KeyAccount(api_key=str(env_keys[vendor]))
    raise AccountRefused(f"no provider account exists for {vendor.split(':', 1)[-1]}")


def row_account(vendor: str, account: Account, credential_id: str) -> Account:
    """The host a row can actually use on TTS: ElevenLabs — the deployment's
    TTS host decides, the row is the key."""
    if vendor == "elevenlabs":
        assert isinstance(account, KeyOnlyAccount)
        return KeyAccount(api_key=account.api_key, endpoint=elevenlabs_host())
    return account
