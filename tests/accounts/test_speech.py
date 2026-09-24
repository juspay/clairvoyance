"""Phase 4 of provider accounts (docs/PROVIDER_CREDENTIALS.md): the STT and
TTS factories build on the account; a DragonTTS voice with an account is
synthesized by its provider directly; the ElevenLabs host is the
deployment's per-service URL and the row only brings the key."""

import asyncio
from typing import Any, Dict

import pytest

from app.ai.voice.agents.breeze_buddy.accounts import AccountRefused, Accounts
from app.ai.voice.agents.breeze_buddy.template.types import (
    STTConfiguration,
    TTSConfig,
)
from tests.accounts.conftest import ROW, ROW2, Store, cred


def test_the_stt_factory_builds_on_the_account(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.stt as stt_factory

    seen: Dict[str, Any] = {}

    def fake_build(config: Any) -> str:
        seen["api_key"] = config.api_key
        return "stt-svc"

    monkeypatch.setattr(stt_factory, "build_deepgram_stt", fake_build)
    store.rows[ROW2] = cred(id=ROW2, provider="deepgram", value={"api_key": "dg-acct"})
    config = STTConfiguration(provider="deepgram", credential_id=ROW2)
    assert (
        asyncio.run(
            stt_factory.create_stt_from_config(config, accounts=Accounts("r-1", "m-1"))
        )
        == "stt-svc"
    )
    assert seen == {"api_key": "dg-acct"}


def test_the_tts_factory_bypasses_dragontts_for_a_voice_with_an_account(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.tts as tts_factory

    seen: Dict[str, Any] = {}

    def fake_cartesia(config: Any) -> str:
        seen["api_key"], seen["model"] = config.api_key, config.model
        return "cartesia-svc"

    def fake_dragon(config: Any) -> str:
        raise AssertionError("DragonTTS must not be used for a voice with an account")

    async def off(*a: Any, **k: Any) -> Any:
        return False

    monkeypatch.setattr(tts_factory, "build_cartesia_tts", fake_cartesia)
    monkeypatch.setattr(tts_factory, "build_dragontts_tts", fake_dragon)
    monkeypatch.setattr(tts_factory, "BB_STRIP_EMOJIS_FROM_TTS", off)
    monkeypatch.setattr(tts_factory, "BB_AGGREGATE_SENTENCES", off)
    store.rows[ROW2] = cred(id=ROW2, provider="cartesia", value={"api_key": "ca-acct"})
    voice = TTSConfig(
        provider="dragontts", model="cartesia:sonic-3", voice_id="v", credential_id=ROW2
    )
    svc = asyncio.run(
        tts_factory.get_tts_service(voice, accounts=Accounts("r-1", "m-1"))
    )
    assert svc == "cartesia-svc" and seen == {"api_key": "ca-acct", "model": "sonic-3"}
    # the same voice naming an ElevenLabs row is refused: the row must be the
    # provider that really synthesizes
    wrong = TTSConfig(
        provider="dragontts", model="cartesia:sonic-3", voice_id="v", credential_id=ROW
    )
    with pytest.raises(AccountRefused, match="this block needs cartesia"):
        asyncio.run(tts_factory.get_tts_service(wrong, accounts=Accounts("r-1", "m-1")))


def test_resolve_voice_config_unwraps_a_dragontts_voice_with_an_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.ai.voice.agents.breeze_buddy.tts as tts_factory

    async def defaults(*a: Any, **k: Any) -> Dict[str, Any]:
        return {}

    monkeypatch.setattr(tts_factory, "BB_VOICE_PROVIDER_DEFAULTS", defaults)
    voice = TTSConfig(
        provider="dragontts", model="elevenlabs:eleven_flash_v2_5", credential_id=ROW
    )
    resolved = asyncio.run(tts_factory.resolve_voice_config(voice))
    assert (resolved.provider.value, resolved.model, resolved.credential_id) == (
        "elevenlabs",
        "eleven_flash_v2_5",
        ROW,
    )


def test_the_elevenlabs_host_is_the_deployments_url_and_the_row_only_brings_the_key(
    store: Store, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ai.voice.agents.breeze_buddy.stt as stt_factory
    import app.ai.voice.agents.breeze_buddy.tts as tts_factory

    seen: Dict[str, Any] = {}

    def fake_elevenlabs(config: Any) -> str:
        seen["api_key"], seen["url"] = config.api_key, config.url
        return "elevenlabs-svc"

    def fake_scribe(config: Any) -> str:
        seen["stt_key"], seen["base_url"] = config.api_key, config.base_url
        return "scribe-svc"

    async def off(*a: Any, **k: Any) -> Any:
        return False

    monkeypatch.setattr(tts_factory, "build_elevenlabs_tts", fake_elevenlabs)
    monkeypatch.setattr(tts_factory, "BB_STRIP_EMOJIS_FROM_TTS", off)
    monkeypatch.setattr(tts_factory, "BB_AGGREGATE_SENTENCES", off)
    monkeypatch.setattr(stt_factory, "build_elevenlabs_stt", fake_scribe)
    voice = TTSConfig(provider="elevenlabs", voice_id="v", credential_id=ROW)
    asyncio.run(tts_factory.get_tts_service(voice, accounts=Accounts("r-1", "m-1")))
    assert (seen["api_key"], seen["url"]) == ("xi-secret", "wss://tts.india.test")
    scribe = STTConfiguration(provider="elevenlabs", credential_id=ROW)
    asyncio.run(
        stt_factory.create_stt_from_config(scribe, accounts=Accounts("r-1", "m-1"))
    )
    # pipecat builds wss://{base_url}/v1 itself: the bare host, the row's key
    assert (seen["stt_key"], seen["base_url"]) == ("xi-secret", "stt.india.test")


def test_the_ivr_walker_checks_the_account_up_front_only_for_a_voice_that_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy DragonTTS voice (no credential_id) must not be refused at IVR
    start — the resolver has no account for the proxy, and never needs one."""
    with pytest.raises(
        AccountRefused, match="no provider account exists for dragontts"
    ):
        asyncio.run(
            Accounts("r-1", "m-1").get(
                TTSConfig(provider="dragontts", model="elevenlabs:eleven_flash_v2_5")
            )
        )


def test_the_ivr_walker_ends_the_call_as_an_ivr_error_when_the_voice_check_raises_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential read that fails with a DB error (not a ValueError), or a
    voice that will not resolve, must still leave the call with an outcome
    and a closed socket — agent.run wraps the walker in a bare try/finally.
    And the walker uses the agent's resolver: one per call."""
    from types import SimpleNamespace

    from app.ai.voice.agents.breeze_buddy.ivr import walker

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("credentials read: connection reset")

    closed: Dict[str, Any] = {}

    async def finalize(self: Any, call_ended_by: str) -> None:
        closed["by"] = call_ended_by

    monkeypatch.setattr(walker, "resolve_voice_config", boom)
    monkeypatch.setattr(walker.IvrWalker, "_finalize_and_close", finalize)
    agent = SimpleNamespace(
        ws=object(),
        stream_sid="stream-1",
        provider="plivo",
        lead=SimpleNamespace(outcome=None, reseller_id="r-1", merchant_id="m-1"),
        errors=[],
        template=object(),
        configurations=None,
        accounts=None,
        greeting_text=None,
        conversation_ended=False,
    )
    w = walker.IvrWalker(agent)  # type: ignore[arg-type]
    asyncio.run(w.run())
    assert (agent.lead.outcome, closed["by"]) == (walker.IVR_ERROR_OUTCOME, "system")
    assert w.accounts is agent.accounts and isinstance(agent.accounts, Accounts)
