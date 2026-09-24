"""The STT side: which vendor an STT block names, the environment's account
for it, and the host rule a row must obey on this service."""

from __future__ import annotations

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.accounts.types import (
    Account,
    AccountRefused,
    GcpAccount,
    KeyAccount,
    KeyOnlyAccount,
)
from app.core.config import static

# STT provider words already ARE the vendor names — except the Google
# service, which runs on the one service account.
STT_VENDOR: Dict[str, str] = {
    "deepgram": "deepgram",
    "soniox": "soniox",
    "sarvam": "sarvam",
    "assemblyai": "assemblyai",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
    "openai": "openai",
    "google": "google",
}


def elevenlabs_host() -> str:
    """ElevenLabs Scribe runs on the deployment's ``ELEVENLABS_STT_URL`` (a
    bare host; release cbcec339) — carried as a ``wss://`` URL; the STT
    builder hands pipecat the bare host it expects."""
    return f"wss://{static.ELEVENLABS_STT_URL}"


async def env_account(vendor: str, block: Any) -> Account:
    """The environment's STT account for a vendor. Raises AccountRefused,
    and only AccountRefused, when the environment has none."""
    if vendor == "openai":
        if not static.OPENAI_STT_API_KEY:
            raise AccountRefused("OPENAI_STT_API_KEY is required for openai STT")
        return KeyAccount(api_key=str(static.OPENAI_STT_API_KEY))
    if vendor == "elevenlabs":
        if not static.ELEVENLABS_STT_API_KEY:
            raise AccountRefused(
                "ELEVENLABS_STT_API_KEY is required for elevenlabs STT"
            )
        return KeyAccount(
            api_key=str(static.ELEVENLABS_STT_API_KEY), endpoint=elevenlabs_host()
        )
    if vendor == "google":
        if not static.GOOGLE_CREDENTIALS_JSON:
            raise AccountRefused("GOOGLE_CREDENTIALS_JSON is required for stt")
        return GcpAccount(credentials_json=str(static.GOOGLE_CREDENTIALS_JSON))
    env_keys = {
        "deepgram": static.DEEPGRAM_API_KEY,
        "soniox": static.SONIOX_API_KEY,
        "sarvam": static.SARVAM_API_KEY,
        "assemblyai": static.ASSEMBLYAI_API_KEY,
        "cartesia": static.CARTESIA_API_KEY,
    }
    if vendor in env_keys:
        if not env_keys[vendor]:
            raise AccountRefused(
                f"{vendor.upper()}_API_KEY is required for {vendor} STT"
            )
        return KeyAccount(api_key=str(env_keys[vendor]))
    raise AccountRefused(f"no provider account exists for {vendor.split(':', 1)[-1]}")


def row_account(vendor: str, account: Account, credential_id: str) -> Account:
    """The host a row can actually use on STT: a service with one fixed host
    cannot take an account that lives elsewhere. ElevenLabs: the deployment's
    STT host decides, the row is the key. OpenAI STT has no gateway."""
    if vendor == "elevenlabs":
        assert isinstance(account, KeyOnlyAccount)
        return KeyAccount(api_key=account.api_key, endpoint=elevenlabs_host())
    if vendor == "openai" and getattr(account, "endpoint", None):
        raise AccountRefused(
            f"credential {credential_id} names a gateway endpoint, and "
            "OpenAI STT has no gateway — it can only use the public host"
        )
    return account
