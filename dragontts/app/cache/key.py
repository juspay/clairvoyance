"""Cache-key derivation — normalization + canonical params + hashing.

The key fully determines the cached bytes. ``model_id`` carries routing as
``<provider>:<model>`` and is split on the FIRST colon only, so a model that
itself contains a colon (e.g. sarvam ``bulbul:v3``) parses correctly:
``"sarvam:bulbul:v3"`` -> provider ``sarvam``, model ``bulbul:v3``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from app.core.config import PROVIDER_DEFAULTS

# Unit separator between key parts — prevents field-merge ambiguity.
_SEP = "\x1f"

# Invisible / zero-width characters to strip. LLM tokenization emits these as
# artifacts inside transliterated words (e.g. Telugu "వెబ్‌సైట్" carries a ZWJ
# ‌ between వెబ్ and సైట్). They have no TTS value, can make a provider
# mis-synthesize the word (white noise), and would otherwise split one spoken
# phrase into two cache keys (with-ZWJ vs without). Stripped before key + synth.
_ZW = "​‌‍‎‏﻿­"
_ZW_TABLE = str.maketrans("", "", _ZW)


def normalize_text(text: str) -> str:
    """Strip zero-width chars, NFC-normalize, then collapse internal whitespace."""
    text = text.translate(_ZW_TABLE)
    return " ".join(unicodedata.normalize("NFC", text).split())


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Split ``<provider>:<model>`` on the first colon. Provider names contain
    no colon; the model half may (e.g. ``bulbul:v3``)."""
    if ":" not in model_id:
        raise ValueError(f"model_id must be '<provider>:<model>', got {model_id!r}")
    provider, model = model_id.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise ValueError(f"model_id missing provider or model: {model_id!r}")
    return provider, model


def canonical_params(provider: str, params: dict | None) -> str:
    """Canonical JSON of tuning params, dropping values equal to provider
    defaults. Returns "" when all params are default/absent, so identical-
    default requests collapse to one entry while differing params get distinct
    entries."""
    params = params or {}
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    kept = {
        k: params[k]
        for k in sorted(params)
        if params[k] is not None and not (k in defaults and params[k] == defaults[k])
    }
    if not kept:
        return ""
    return json.dumps(kept, sort_keys=True, separators=(",", ":"))


def hash_key(
    *,
    text: str,
    provider: str,
    voice_id: str,
    model: str,
    language: str,
    params_canonical: str,
) -> str:
    """Deterministic SHA-256 over every input that affects the audio.

    The requested output_format is intentionally NOT part of the key: the cache
    stores audio in the provider's native format and converts to the requested
    format on serve, so one entry serves every format (one-shot μ-law and
    streaming PCM share it).
    """
    parts = [
        normalize_text(text),
        provider,
        voice_id,
        model,
        language or "",
        params_canonical,
    ]
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
