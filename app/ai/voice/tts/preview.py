"""Browser-playable preview synthesis for the TTS voice catalog.

Deliberately separate from the runtime telephony path (generate_audio ->
convert_to_mulaw, 8 kHz mulaw): previews are WAV 16-bit mono at the
provider's native/requested PCM rate. Never touches runtime dispatch.

Flow: ``resolve_preview_config`` first resolves one immutable preview
specification (model defaults, provider knobs, dynamic toggles), then that
same specification drives BOTH ``content_key`` hashing and
``generate_preview`` synthesis — so a configuration change always changes
the key, and a stored preview can never look current after the config that
produced it has moved on.

Exception to that resolution: when a catalog voice omits ``models``, the
elevenlabs/cartesia/sarvam/soniox model fallback reads the static
``BB_SPEECH_PROVIDER_DEFAULTS`` dict (or ``ELEVENLABS_MODEL_ID``) rather
than the Redis-overridable ``BB_VOICE_PROVIDER_DEFAULTS`` tier —
deliberate, since a preview pins one catalog voice/model rather than
tracking the live runtime default; see the per-branch comments in
``resolve_preview_config``.

Each ``_synthesize_<provider>_pcm`` branch mirrors the request shape of the
corresponding ``_generate_<provider>_audio`` helper in this package (same
auth, URL, payload, and residency handling) but asks the provider for
browser-friendly PCM instead of the telephony formats those helpers use.
"""

import asyncio
import base64
import hashlib
import io
import json
import wave
from typing import Optional

import httpx
from pipecat.services.soniox.tts import language_to_soniox_tts_language
from pipecat.transcriptions.language import Language
from websockets.asyncio.client import connect as websocket_connect

from app.ai.voice.tts.soniox import SONIOX_TTS_SYNTH_TIMEOUT_SECS, SONIOX_TTS_WS_URL
from app.core.config.dynamic import (
    BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY,
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_SPEECH_PROVIDER_DEFAULTS,
    GEMINI_TTS_MODEL,
)
from app.core.config.static import (
    CARTESIA_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_INDIAN_RESIDENCY_API_KEY,
    ELEVENLABS_MODEL_ID,
    GOOGLE_CREDENTIALS_JSON,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger

__all__ = [
    "PreviewGenerationError",
    "CANONICAL_SENTENCES",
    "sentence_for",
    "resolve_preview_config",
    "content_key",
    "pcm_to_wav",
    "wav_to_pcm",
    "generate_preview",
]

# Gemini/Google Chirp TTS native streaming sample rate — fixed by the API.
_GEMINI_SAMPLE_RATE = 24_000
_GOOGLE_SAMPLE_RATE = 24_000

CANONICAL_SENTENCES: dict[str, str] = {
    "en": "Hi, this is a quick preview of my voice. I can help confirm your order and answer questions.",
    "hi": "नमस्ते, यह मेरी आवाज़ की एक छोटी झलक है। मैं आपका ऑर्डर कन्फर्म करने में मदद कर सकती हूँ।",
    "ta": "வணக்கம், இது என் குரலின் ஒரு சிறிய முன்னோட்டம். உங்கள் ஆர்டரை உறுதிப்படுத்த உதவுகிறேன்.",
    "te": "నమస్తే, ఇది నా గొంతు యొక్క చిన్న ప్రివ్యూ. మీ ఆర్డర్‌ను నిర్ధారించడంలో సహాయం చేస్తాను.",
    "kn": "ನಮಸ್ಕಾರ, ಇದು ನನ್ನ ಧ್ವನಿಯ ಸಣ್ಣ ಮುನ್ನೋಟ. ನಿಮ್ಮ ಆರ್ಡರ್ ದೃಢೀಕರಿಸಲು ಸಹಾಯ ಮಾಡುತ್ತೇನೆ.",
}


def sentence_for(language: str) -> str:
    """Canonical preview sentence for a language, falling back bare-code ->
    English (e.g. "en-IN" -> "en", unknown -> "en")."""
    lang = (language or "en").lower()
    return CANONICAL_SENTENCES.get(lang) or CANONICAL_SENTENCES.get(
        lang.split("-")[0], CANONICAL_SENTENCES["en"]
    )


async def resolve_preview_config(
    provider: str,
    voice_id: str,
    model: Optional[str],
    language: str,
    style_params: Optional[dict],
) -> dict:
    """Resolve the effective synthesis configuration for one preview.

    Fills in every input that determines the audio output: catalog model or
    the provider's default, dynamic toggles (ElevenLabs residency, Sarvam
    preprocessing), Sarvam payload knobs, fixed sample rates. The returned
    dict is the single spec that both ``content_key`` and
    ``generate_preview`` consume, so hashing and synthesis can never see
    different configurations.
    """
    style = style_params or {}
    text = sentence_for(language)
    cfg: dict = {
        "provider": provider,
        "voice_id": voice_id,
        "language": language,
        "text": text,
        "style_params": style,
        "format": "wav",
    }
    if provider == "elevenlabs":
        cfg["model"] = model or ELEVENLABS_MODEL_ID
        cfg["indian_residency"] = await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY()
        cfg["voice_settings"] = {"stability": 0.5, "similarity_boost": 0.75}
    elif provider == "cartesia":
        # Static-dict fallback only — deliberately skips BB_VOICE_PROVIDER_DEFAULTS'
        # Redis override tier since preview always targets one specific catalog
        # voice/model, not "the currently configured runtime default".
        cfg["model"] = model or BB_SPEECH_PROVIDER_DEFAULTS.get("cartesia", {}).get(
            "model"
        )
        cfg["sample_rate"] = 16000
    elif provider == "sarvam":
        # sarvam_defaults is the static-dict tier only — same rationale as
        # the cartesia branch above.
        cfg["payload"] = _sarvam_payload(
            voice_id=voice_id,
            model=model,
            language_code=language or "en-IN",
            text=text,
            sarvam_defaults=BB_SPEECH_PROVIDER_DEFAULTS.get("sarvam", {}),
            enable_preprocessing=await BB_SARVAM_TTS_ENABLE_PREPROCESSING(),
        )
        cfg["model"] = cfg["payload"]["model"]
    elif provider == "gemini":
        cfg["model"] = model or await GEMINI_TTS_MODEL()
        cfg["sample_rate"] = _GEMINI_SAMPLE_RATE
    elif provider == "google":
        # Chirp3-HD voice names encode model + locale; no separate model.
        cfg["model"] = None
        cfg["sample_rate"] = _GOOGLE_SAMPLE_RATE
    elif provider == "soniox":
        # Static-dict fallback only — same rationale as cartesia above.
        cfg["model"] = model or BB_SPEECH_PROVIDER_DEFAULTS.get("soniox", {}).get(
            "model"
        )
        cfg["sample_rate"] = 16000
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return cfg


def content_key(config: dict) -> str:
    """Deterministic 16-hex-char key over a resolved preview configuration.

    Takes the output of ``resolve_preview_config`` — never raw catalog
    inputs — so the key changes whenever anything that affects the audio
    does (resolved model, dynamic toggles, payload knobs, sentence text).
    """
    payload = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container at the given rate."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    """Extract (frames, sample_rate) from WAV bytes; raw PCM passes through.

    Providers that return a full WAV container (Sarvam's REST API does by
    default) must be unwrapped before ``pcm_to_wav`` re-wraps, or the inner
    header ends up as a click of garbage PCM at the start of the preview.
    """
    if not data.startswith(b"RIFF"):
        return data, 16000
    with wave.open(io.BytesIO(data)) as w:
        return w.readframes(w.getnframes()), w.getframerate()


class PreviewGenerationError(Exception):
    """Raised when a provider fails to synthesize a preview for a voice."""

    def __init__(self, provider: str, voice_id: str, cause: str):
        self.provider = provider
        self.voice_id = voice_id
        self.cause = cause
        super().__init__(
            f"Preview generation failed for provider={provider} voice_id={voice_id}: {cause}"
        )


async def generate_preview(config: dict) -> tuple[bytes, str]:
    """Synthesize one preview WAV from a resolved configuration.

    Returns (wav_bytes, content_key) where the key is derived from the same
    ``config`` that drove synthesis (see ``resolve_preview_config``).
    """
    key = content_key(config)
    pcm, rate = await _synthesize_pcm(config)
    return pcm_to_wav(pcm, rate), key


async def _synthesize_pcm(config: dict) -> tuple[bytes, int]:
    provider = config["provider"]
    voice_id = config["voice_id"]
    try:
        if provider == "cartesia":
            return await _synthesize_cartesia_pcm(config)
        if provider == "elevenlabs":
            return await _synthesize_elevenlabs_pcm(config)
        if provider == "sarvam":
            return await _synthesize_sarvam_pcm(config)
        if provider == "gemini":
            return await _synthesize_gemini_pcm(config)
        if provider == "google":
            return await _synthesize_google_pcm(config)
        if provider == "soniox":
            return await _synthesize_soniox_pcm(config)
        raise ValueError(f"Unsupported provider: {provider}")
    except Exception as exc:
        raise PreviewGenerationError(provider, voice_id, str(exc)) from exc


async def _synthesize_cartesia_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_cartesia_audio`'s request shape (URL, headers,
    payload) — already requests pcm_s16le @ 16000. Model/voice/language arrive
    pre-resolved from `resolve_preview_config`, which deliberately skips that
    helper's BB_VOICE_PROVIDER_DEFAULTS Redis tier."""
    if not CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY is required for Cartesia preview synthesis")

    url = "https://api.cartesia.ai/tts/bytes"
    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
        "Content-Type": "application/json",
    }
    payload = {
        "model_id": config["model"],
        "transcript": config["text"],
        "voice": {"mode": "id", "id": config["voice_id"]},
        "language": config["language"],
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": config["sample_rate"],
        },
    }

    logger.info(
        f"Synthesizing preview with Cartesia "
        f"[voice_id={config['voice_id']}, model={config['model']}]"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content, config["sample_rate"]


async def _synthesize_elevenlabs_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_elevenlabs_audio`'s auth/residency split, requesting
    pcm_16000 instead of ulaw_8000. Residency comes pre-resolved in the
    config so synthesis matches what was hashed."""
    if config["indian_residency"]:
        api_key = ELEVENLABS_INDIAN_RESIDENCY_API_KEY
        base_url = "https://api.in.residency.elevenlabs.io"
        if not api_key:
            raise ValueError(
                "ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required when use_indian_residency is True"
            )
    else:
        api_key = ELEVENLABS_API_KEY
        base_url = "https://api.elevenlabs.io"
        if not api_key:
            raise ValueError(
                "ELEVENLABS_API_KEY is required for ElevenLabs preview synthesis"
            )

    url = f"{base_url}/v1/text-to-speech/{config['voice_id']}?output_format=pcm_16000"
    # Intentional divergence from the mirrored helper: it sets
    # "Accept: audio/basic" (the u-law MIME type) to match its ulaw_8000
    # request. That header is stale here — output_format=pcm_16000 in the
    # URL governs the response format, so the header is dropped rather than
    # sent with a mismatched MIME type.
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": config["text"],
        "model_id": config["model"],
        "voice_settings": config["voice_settings"],
    }

    logger.info(
        f"Synthesizing preview with ElevenLabs (pcm_16000) "
        f"[voice_id={config['voice_id']}, model_id={config['model']}, "
        f"indian_residency={config['indian_residency']}]"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content, 16000


def _sarvam_payload(
    *,
    voice_id: str,
    model: Optional[str],
    language_code: str,
    text: str,
    sarvam_defaults: dict,
    enable_preprocessing: bool,
) -> dict:
    """Pure payload builder for `POST /text-to-speech`, kept separate from the
    network call so the v3 field-omission rule and language qualification can
    be unit tested directly.

    Bulbul V3 rejects pitch/loudness outright (confirmed via the API's own
    error): "Pitch and loudness parameters are currently not supported for
    the Bulbul V3 model. Please do not pass these values." Earlier models
    (e.g. bulbul:v2) accept both, so only omit them for v3.

    target_language_code also only accepts region-qualified codes (the API's
    validation error enumerates an all-"xx-IN" set: as-IN, bn-IN, ..., hi-IN,
    ..., ur-IN) but the catalog stores bare codes (e.g. "hi", "en"). Sarvam is
    India-region-only, so qualifying any bare code with "-IN" is safe here.
    """
    final_model = model or sarvam_defaults.get("model")
    qualified_language_code = (
        language_code if "-" in language_code else f"{language_code}-IN"
    )
    payload = {
        "inputs": [text],
        "target_language_code": qualified_language_code,
        "speaker": voice_id,
        "pace": sarvam_defaults.get("speed", 0.9),
        "speech_sample_rate": 16000,
        "enable_preprocessing": enable_preprocessing,
        "model": final_model,
    }
    if not (final_model or "").startswith("bulbul:v3"):
        payload["pitch"] = sarvam_defaults.get("pitch", 0.0)
        payload["loudness"] = 1.5
    return payload


async def _synthesize_sarvam_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_sarvam_audio`, posting the exact payload that was
    hashed into the content key. Sarvam's REST API returns base64 WAV (its
    default codec), so the container is stripped here — `generate_preview`
    adds exactly one WAV header."""
    if not SARVAM_API_KEY:
        raise ValueError("SARVAM_API_KEY is required for Sarvam preview synthesis")

    url = "https://api.sarvam.ai/text-to-speech"
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    payload = config["payload"]

    logger.info(
        f"Synthesizing preview with Sarvam "
        f"[voice_id={config['voice_id']}, language={config['language']}]"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        result = response.json()

        audio_base64 = (result.get("audios") or [None])[0]
        if not audio_base64:
            raise Exception("No audio returned from Sarvam API")

        return wav_to_pcm(base64.b64decode(audio_base64))


async def _synthesize_gemini_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_gemini_audio`'s streaming_synthesize + credential
    handling, but returns native 24 kHz PCM (no downsample — pcm_to_wav
    takes the rate) and honours an optional style prompt."""
    from google.cloud import texttospeech_v1
    from google.oauth2 import service_account

    credentials_json = GOOGLE_CREDENTIALS_JSON
    if not credentials_json:
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON is required for Gemini TTS pre-synthesis"
        )

    final_voice = config["voice_id"] or "Kore"
    style_prompt = config["style_params"].get("style_prompt")

    logger.info(
        f"Synthesizing preview with Gemini "
        f"[voice={final_voice}, model={config['model']}, lang={config['language'] or 'en-IN'}]"
    )

    json_account_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(json_account_info)
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)

    try:
        voice_params = texttospeech_v1.VoiceSelectionParams(
            language_code=config["language"] or "en-IN",
            name=final_voice,
            model_name=config["model"],
        )

        streaming_config = texttospeech_v1.StreamingSynthesizeConfig(
            voice=voice_params,
            streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.PCM,
                sample_rate_hertz=_GEMINI_SAMPLE_RATE,
            ),
        )

        async def _request_generator():
            yield texttospeech_v1.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )
            synthesis_params: dict = {"text": config["text"]}
            if style_prompt:
                synthesis_params["prompt"] = style_prompt
            yield texttospeech_v1.StreamingSynthesizeRequest(
                input=texttospeech_v1.StreamingSynthesisInput(**synthesis_params)
            )

        chunks: list[bytes] = []
        streaming_responses = await client.streaming_synthesize(_request_generator())
        async for response in streaming_responses:
            if response.audio_content:
                chunks.append(response.audio_content)

        pcm_24k = b"".join(chunks)

        if not pcm_24k:
            raise RuntimeError(
                "Gemini TTS returned empty audio — check credentials and voice settings"
            )

        return pcm_24k, _GEMINI_SAMPLE_RATE
    finally:
        await client.transport.grpc_channel.close()  # type: ignore[union-attr]


async def _synthesize_google_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_google_audio`'s streaming_synthesize + credential
    handling, but returns native 24 kHz PCM (no downsample)."""
    from google.cloud import texttospeech_v1
    from google.oauth2 import service_account

    credentials_json = GOOGLE_CREDENTIALS_JSON
    if not credentials_json:
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON is required for Google TTS pre-synthesis"
        )

    final_voice = config["voice_id"] or "en-IN-Chirp3-HD-Despina"
    final_language = config["language"] or "en-IN"

    logger.info(
        f"Synthesizing preview with Google "
        f"[voice={final_voice}, lang={final_language}]"
    )

    json_account_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(json_account_info)
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)

    try:
        voice_params = texttospeech_v1.VoiceSelectionParams(
            language_code=final_language,
            name=final_voice,
        )

        streaming_config = texttospeech_v1.StreamingSynthesizeConfig(
            voice=voice_params,
            streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.PCM,
                sample_rate_hertz=_GOOGLE_SAMPLE_RATE,
            ),
        )

        async def _request_generator():
            yield texttospeech_v1.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )
            yield texttospeech_v1.StreamingSynthesizeRequest(
                input=texttospeech_v1.StreamingSynthesisInput(text=config["text"])
            )

        chunks: list[bytes] = []
        streaming_responses = await client.streaming_synthesize(_request_generator())
        async for response in streaming_responses:
            if response.audio_content:
                chunks.append(response.audio_content)

        pcm_24k = b"".join(chunks)

        if not pcm_24k:
            raise RuntimeError(
                "Google TTS returned empty audio — check credentials and voice settings"
            )

        return pcm_24k, _GOOGLE_SAMPLE_RATE
    finally:
        await client.transport.grpc_channel.close()  # type: ignore[union-attr]


async def _synthesize_soniox_pcm(config: dict) -> tuple[bytes, int]:
    """Mirrors `_generate_soniox_audio`'s one-shot WS protocol — already
    requests pcm_s16le @ 16000."""
    if not SONIOX_API_KEY:
        raise ValueError("SONIOX_API_KEY is required for Soniox TTS")

    voice = config["voice_id"] or "Priya"
    sample_rate = config["sample_rate"]
    language = config["language"]

    if language:
        try:
            lang_enum = Language(language)
        except ValueError:
            logger.warning(
                f"Invalid Soniox language code '{language}', falling back to EN"
            )
            lang_enum = Language.EN
    else:
        lang_enum = Language.EN

    soniox_lang = language_to_soniox_tts_language(lang_enum) or "en"

    config_msg = {
        "api_key": SONIOX_API_KEY,
        "stream_id": "preview",
        "model": config["model"],
        "voice": voice,
        "language": soniox_lang,
        "audio_format": "pcm_s16le",
        "sample_rate": sample_rate,
    }
    text_msg = {"text": config["text"], "text_end": True, "stream_id": "preview"}

    logger.info(
        f"Synthesizing preview with Soniox (pcm_s16le {sample_rate}) "
        f"[voice_id={voice}, model={config['model']}, language={soniox_lang}]"
    )

    audio_chunks: list[bytes] = []
    try:
        async with asyncio.timeout(SONIOX_TTS_SYNTH_TIMEOUT_SECS):
            async with websocket_connect(SONIOX_TTS_WS_URL) as ws:
                await ws.send(json.dumps(config_msg))
                await ws.send(json.dumps(text_msg))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    error_code = msg.get("error_code")
                    if error_code is not None:
                        error_message = msg.get("error_message", "")
                        raise Exception(
                            f"Soniox TTS error {error_code}: {error_message}"
                        )

                    audio_b64 = msg.get("audio")
                    if audio_b64:
                        audio_chunks.append(base64.b64decode(audio_b64))

                    if msg.get("terminated"):
                        break
    except asyncio.TimeoutError:
        raise Exception(
            f"Soniox TTS synth timed out after {SONIOX_TTS_SYNTH_TIMEOUT_SECS}s"
        )

    if not audio_chunks:
        raise Exception("No audio returned from Soniox TTS")

    return b"".join(audio_chunks), sample_rate
