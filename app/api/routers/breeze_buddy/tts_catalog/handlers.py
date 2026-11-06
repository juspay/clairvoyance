"""Handlers for the TTS voice catalog endpoints:
- GET  /tts/voices           — filtered, ETag-cached voice catalog listing.
- POST /tts/voices/reconcile — admin-only preview generation reconciliation.

Voices come from the static catalog (app/ai/voice/tts/catalog.json); which
previews exist for them comes from the preview-storage manifest. There is
no database involved.
"""

import asyncio
import hashlib
import json
from typing import Optional

from app.ai.voice.tts.catalog import get_enabled_voices
from app.ai.voice.tts.preview import (
    content_key,
    generate_preview,
    resolve_preview_config,
)
from app.core.config.dynamic import BB_VOICE_PROVIDER_DEFAULTS
from app.core.logger import logger
from app.schemas.breeze_buddy.tts_catalog import (
    CATALOG_PROVIDERS,
    CatalogVoice,
    VoicePreview,
    VoicesResponse,
)
from app.services.preview_storage import (
    load_manifest,
    load_manifest_fresh,
    manifest_key,
    store_preview,
    update_previews,
)

from .matching import language_matches

GEMINI_FALLBACK_DEFAULT = "Kore"  # gemini has no BB_SPEECH_PROVIDER_DEFAULTS entry

# No rate-limit infra exists in the TTS layer; pace generation calls gently.
RECONCILE_DELAY_SECS = 1.0

# Cap on provider error text echoed back to the admin caller in the reconcile
# report. The full exception is always logged server-side.
ERROR_DETAIL_LIMIT = 300


def _public_error(exc: Exception) -> str:
    """Exception summary safe to persist in the manifest.

    The manifest lives in the public preview location, so anything written
    into it is world-readable. A raw provider error body is not: it can echo
    back request fragments and, for a misbehaving provider, header or
    credential material. Only the exception's class name goes to storage; the
    message survives in the admin-only report and the server log.
    """
    return type(exc).__name__


async def _resolve_defaults() -> dict[str, Optional[str]]:
    """Per-provider default voice_id, fetched concurrently (each lookup hits
    Redis)."""
    configs = await asyncio.gather(
        *(BB_VOICE_PROVIDER_DEFAULTS(p) for p in CATALOG_PROVIDERS)
    )
    defaults: dict[str, Optional[str]] = {
        p: (cfg or {}).get("voice_id") for p, cfg in zip(CATALOG_PROVIDERS, configs)
    }
    defaults["gemini"] = defaults.get("gemini") or GEMINI_FALLBACK_DEFAULT
    return defaults


async def list_voices_handler(
    provider: Optional[str], language: Optional[str]
) -> tuple[VoicesResponse, str]:
    """Build the filtered voice listing plus its ETag."""
    manifest, defaults = await asyncio.gather(load_manifest(), _resolve_defaults())

    filtered = [
        v
        for v in get_enabled_voices()
        if (not provider or v.provider == provider)
        and (not language or language_matches(language, v.languages))
    ]

    voices = []
    for v in filtered:
        raw_previews = manifest.get(manifest_key(v.provider, v.voice_id), [])
        previews = [
            VoicePreview(language=p["language"], url=p["url"])
            for p in raw_previews
            if p.get("url") and not p.get("error")
        ]
        voices.append(
            CatalogVoice(
                provider=v.provider,
                voice_id=v.voice_id,
                display_name=v.display_name,
                models=v.models,
                languages=v.languages,
                tags=v.tags,
                is_default=(defaults.get(v.provider) == v.voice_id),
                previews=previews,
            )
        )

    # ETag must identify the representation actually returned (RFC 7232):
    # hash the filter params plus the full response content — which already
    # folds in the resolved defaults (is_default) and manifest state
    # (previews) — so any change to catalog, defaults, or previews yields a
    # distinct ETag for the affected filtered views.
    stamp = json.dumps(
        [provider, language, [v.model_dump() for v in voices]], sort_keys=True
    )
    etag = '"' + hashlib.sha256(stamp.encode()).hexdigest()[:16] + '"'
    return VoicesResponse(voices=voices), etag


async def reconcile_previews_handler() -> dict:
    """Regenerate stale/missing previews for every enabled voice.

    For each voice, walks its languages (or a single "en" pass for
    language-agnostic voices), skipping any language whose manifest entry
    already matches the current content_key and has a url. Any failure —
    generation or storage — is recorded against that language rather than
    aborting the run, so one broken voice doesn't block the rest. A failed
    regeneration never evicts a previously working preview: the last-known-good
    entry stays in the manifest (its stale content_key makes the next
    reconcile retry) so GET /tts/voices keeps serving the old audio meanwhile.

    The manifest is read fresh (not via the GET path's fail-soft TTL cache):
    a backend read failure aborts here — before any paid provider call — and a
    corrupt manifest file loads as empty (logged), so this run regenerates and
    the successful write repairs it.
    """
    manifest = await load_manifest_fresh()
    report = []
    for v in get_enabled_voices():
        langs = v.languages or ["en"]  # language-agnostic voices get one 'en' preview
        model = v.models[0] if v.models else None
        previews = list(manifest.get(manifest_key(v.provider, v.voice_id), []))
        changed = False
        for lang in langs:
            existing = next((p for p in previews if p.get("language") == lang), None)
            key = None
            entry = None
            try:
                config = await resolve_preview_config(
                    v.provider, v.voice_id, model, lang, v.style_params
                )
                key = content_key(config)
                if (
                    existing
                    and existing.get("content_key") == key
                    and existing.get("url")
                ):
                    report.append(
                        {
                            "provider": v.provider,
                            "voice_id": v.voice_id,
                            "language": lang,
                            "action": "skipped",
                        }
                    )
                    continue
                wav, key = await generate_preview(config)
                url = await store_preview(v.provider, v.voice_id, lang, key, wav)
                entry = {
                    "language": lang,
                    "url": url,
                    "content_key": key,
                    "format": "wav",
                }
                report.append(
                    {
                        "provider": v.provider,
                        "voice_id": v.voice_id,
                        "language": lang,
                        "action": "generated",
                    }
                )
            except Exception as e:
                logger.exception(
                    f"tts previews: generation failed for "
                    f"{v.provider}/{v.voice_id} [{lang}]"
                )
                report.append(
                    {
                        "provider": v.provider,
                        "voice_id": v.voice_id,
                        "language": lang,
                        "action": "failed",
                        "error": str(e)[:ERROR_DETAIL_LIMIT],
                    }
                )
                if not (existing and existing.get("url")):
                    entry = {
                        "language": lang,
                        "error": _public_error(e),
                        "content_key": key,
                    }
            if entry is not None:
                previews = [p for p in previews if p.get("language") != lang] + [entry]
                changed = True
            await asyncio.sleep(RECONCILE_DELAY_SECS)
        if changed:
            try:
                await update_previews(v.provider, v.voice_id, previews)
            except Exception as e:
                logger.exception(
                    f"tts previews: manifest write failed for "
                    f"{v.provider}/{v.voice_id}"
                )
                report.append(
                    {
                        "provider": v.provider,
                        "voice_id": v.voice_id,
                        "language": "*",
                        "action": "manifest_write_failed",
                        "error": str(e)[:ERROR_DETAIL_LIMIT],
                    }
                )
    return {"report": report}
