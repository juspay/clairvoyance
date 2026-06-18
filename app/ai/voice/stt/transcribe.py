"""One-shot (push-to-talk) speech-to-text.

The builders elsewhere in this package create *streaming* Pipecat STT
services (websocket / pipeline bound). This module is the opposite: a
single, direct "audio bytes -> text" call for short clips — e.g. the
chat widget's push-to-talk button. No pipeline, no batching/polling.

Provider is chosen by the caller from the template's
``stt_configuration.provider``. OpenAI, Deepgram, and Sarvam each expose
a simple token-auth REST endpoint that returns the transcript in one
response, so we hit those directly with ``httpx``. Soniox — the platform's
primary provider — has no synchronous endpoint, so its one-shot path uses
the async file API (upload → create job → poll → fetch transcript → cleanup).
Google (OAuth-only) still has no direct one-shot path, so a template
configured for it transcribes the clip with OpenAI Whisper instead. Any
provider's transient/format failure also falls back to Whisper so
push-to-talk "just works".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.core.config.dynamic import SONIOX_ASYNC_MODEL
from app.core.config.static import (
    DEEPGRAM_API_KEY,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

__all__ = ["Transcription", "TranscriptionError", "transcribe_audio"]

_HTTP_TIMEOUT_SECONDS = 30.0

_OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_SARVAM_URL = "https://api.sarvam.ai/speech-to-text"

# Soniox has no synchronous endpoint; one-shot transcription goes through its
# async file API. Short push-to-talk clips finish in 1-3s, so a brief poll loop
# (capped at ~28s) is enough before falling back to Whisper.
_SONIOX_BASE_URL = "https://api.soniox.com"
_SONIOX_POLL_INTERVAL_SECONDS = 0.7
_SONIOX_MAX_POLLS = 40

LanguageHint = Optional[Union[str, List[str]]]


@dataclass
class Transcription:
    """Result of a one-shot transcription."""

    text: str
    provider: str


class TranscriptionError(Exception):
    """Raised when transcription fails and no fallback could recover it."""


def _short_lang(language: LanguageHint) -> Optional[str]:
    """ISO-639-1 hint for OpenAI / Deepgram (``"en-IN"`` -> ``"en"``)."""
    if isinstance(language, list):
        language = language[0] if language else None
    if not language:
        return None
    return str(language).split("-")[0].lower() or None


def _sarvam_lang(language: LanguageHint) -> str:
    """Sarvam wants a BCP-47 region code (``"hi-IN"``) or ``"unknown"`` to
    auto-detect — a bare ``"hi"`` is not accepted, so fall back to auto."""
    if isinstance(language, list):
        language = language[0] if language else None
    if not language:
        return "unknown"
    s = str(language)
    return s if "-" in s else "unknown"


async def _openai(
    audio: bytes, content_type: Optional[str], filename: str, lang: Optional[str]
) -> Transcription:
    headers = {"Authorization": f"Bearer {OPENAI_STT_API_KEY}"}
    files = {"file": (filename, audio, content_type or "audio/webm")}
    data = {"model": OPENAI_STT_MODEL or "whisper-1"}
    if lang:
        data["language"] = lang
    async with create_http_client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(_OPENAI_URL, headers=headers, files=files, data=data)
    resp.raise_for_status()
    text = (resp.json().get("text") or "").strip()
    return Transcription(text=text, provider="openai")


async def _deepgram(
    audio: bytes, content_type: Optional[str], lang: Optional[str]
) -> Transcription:
    params = {"model": "nova-3", "smart_format": "true", "language": lang or "multi"}
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": content_type or "audio/webm",
    }
    async with create_http_client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            _DEEPGRAM_URL, params=params, headers=headers, content=audio
        )
    resp.raise_for_status()
    alts = resp.json()["results"]["channels"][0]["alternatives"]
    text = (alts[0]["transcript"] if alts else "").strip()
    return Transcription(text=text, provider="deepgram")


async def _sarvam(
    audio: bytes, content_type: Optional[str], filename: str, lang_code: str
) -> Transcription:
    headers = {"api-subscription-key": SARVAM_API_KEY or ""}
    files = {"file": (filename, audio, content_type or "audio/wav")}
    data = {"model": "saarika:v2", "language_code": lang_code}
    async with create_http_client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(_SARVAM_URL, headers=headers, files=files, data=data)
    resp.raise_for_status()
    return Transcription(
        text=(resp.json().get("transcript") or "").strip(), provider="sarvam"
    )


def _soniox_lang_hints(language: LanguageHint) -> Optional[List[str]]:
    """Soniox wants an array of short language codes (``["en", "hi"]``); omit
    entirely to auto-detect. ``"en-IN"`` -> ``"en"``."""
    if isinstance(language, list):
        langs: List[str] = [str(item) for item in language]
    elif language:
        langs = [str(language)]
    else:
        return None
    hints = [item.split("-")[0].lower() for item in langs if item]
    return hints or None


async def _soniox(
    audio: bytes,
    content_type: Optional[str],
    filename: str,
    language: LanguageHint,
) -> Transcription:
    """One-shot transcription via Soniox's async file API.

    Soniox has no synchronous endpoint, so this uploads the clip, creates an
    async job, polls until it completes, fetches the transcript, then best-effort
    deletes both server-side records. Raises a plain ``Exception`` (never
    :class:`TranscriptionError`) on any failure so ``transcribe_audio`` recovers
    via Whisper.
    """
    file_id: Optional[str] = None
    transcription_id: Optional[str] = None
    async with create_http_client(
        timeout=_HTTP_TIMEOUT_SECONDS,
        base_url=_SONIOX_BASE_URL,
        headers={"Authorization": f"Bearer {SONIOX_API_KEY}"},
    ) as client:
        try:
            # 1. Upload bytes — httpx sets the multipart boundary; never hand-set
            #    Content-Type (the container/codec is auto-detected anyway).
            up = await client.post(
                "/v1/files",
                files={"file": (filename, audio, content_type or "audio/webm")},
            )
            up.raise_for_status()
            file_id = up.json()["id"]

            # 2. Create the async transcription job. The model resolves
            #    Redis → env → default, so it can be bumped without a deploy.
            body: Dict[str, Any] = {
                "model": await SONIOX_ASYNC_MODEL(),
                "file_id": file_id,
            }
            hints = _soniox_lang_hints(language)
            if hints:
                body["language_hints"] = hints
            cr = await client.post("/v1/transcriptions", json=body)
            cr.raise_for_status()
            transcription_id = cr.json()["id"]

            # 3. Poll until terminal (short clips finish in a few seconds).
            status = ""
            for _ in range(_SONIOX_MAX_POLLS):
                st = await client.get(f"/v1/transcriptions/{transcription_id}")
                st.raise_for_status()
                doc = st.json()
                status = doc.get("status", "")
                if status == "error":
                    raise RuntimeError(
                        doc.get("error_message") or "soniox transcription error"
                    )
                if status == "completed":
                    break
                await asyncio.sleep(_SONIOX_POLL_INTERVAL_SECONDS)
            if status != "completed":
                raise RuntimeError("soniox transcription timed out")

            # 4. Fetch transcript — prefer the top-level text, else join tokens
            #    (token.text already carries leading spaces, so empty-string join).
            tr = await client.get(f"/v1/transcriptions/{transcription_id}/transcript")
            tr.raise_for_status()
            data = tr.json()
            text = (data.get("text") or "").strip()
            if not text:
                text = "".join(
                    t.get("text", "") for t in (data.get("tokens") or [])
                ).strip()
            if not text:
                raise RuntimeError("soniox returned empty transcript")
            return Transcription(text=text, provider="soniox")
        finally:
            # 5. Best-effort cleanup — never break the request path.
            cleanup_paths: List[str] = []
            if transcription_id:
                cleanup_paths.append(f"/v1/transcriptions/{transcription_id}")
            if file_id:
                cleanup_paths.append(f"/v1/files/{file_id}")
            for path in cleanup_paths:
                try:
                    await client.delete(path)
                except Exception:
                    pass


async def transcribe_audio(
    audio: bytes,
    content_type: Optional[str],
    *,
    provider: Optional[str],
    language: LanguageHint = None,
    filename: str = "audio.webm",
) -> Transcription:
    """Transcribe ``audio`` to text using the template-selected ``provider``.

    ``provider`` is an ``STTProvider`` value (``"soniox"`` | ``"deepgram"`` |
    ``"sarvam"`` | ``"openai"`` | ``"google"``). Soniox uses its async file API;
    Google (no direct one-shot path) and any provider whose key is unset
    transcribe via OpenAI Whisper. Raises :class:`TranscriptionError` only when
    no provider can run.
    """
    if not audio:
        raise TranscriptionError("empty audio")

    p = (provider or "").lower()
    lang = _short_lang(language)

    try:
        if p == "soniox" and SONIOX_API_KEY:
            # Pass the raw language (str | list) — Soniox takes a hints array.
            return await _soniox(audio, content_type, filename, language)
        if p == "deepgram" and DEEPGRAM_API_KEY:
            return await _deepgram(audio, content_type, lang)
        if p == "sarvam" and SARVAM_API_KEY:
            return await _sarvam(audio, content_type, filename, _sarvam_lang(language))
        if not OPENAI_STT_API_KEY:
            raise TranscriptionError(
                f"no usable STT provider for '{provider}' (OPENAI_STT_API_KEY unset)"
            )
        if p not in ("openai", "deepgram", "sarvam", "soniox"):
            logger.info(
                "transcribe: provider '{}' has no direct one-shot path; "
                "using OpenAI Whisper",
                provider,
            )
        return await _openai(audio, content_type, filename, lang)
    except TranscriptionError:
        raise
    except Exception as e:
        # Recover with Whisper so the user still gets text (e.g. Sarvam rejecting
        # webm, or a Soniox job error/timeout).
        if p in ("soniox", "deepgram", "sarvam") and OPENAI_STT_API_KEY:
            logger.warning(
                "transcribe: provider '{}' failed ({}); falling back to OpenAI Whisper",
                provider,
                e,
            )
            try:
                return await _openai(audio, content_type, filename, lang)
            except Exception as e2:
                raise TranscriptionError(f"transcription failed: {e2}") from e2
        raise TranscriptionError(f"transcription failed: {e}") from e
