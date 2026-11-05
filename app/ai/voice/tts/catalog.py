"""Static TTS voice catalog.

The catalog is deliberately code, not rows in a table: voices are curated,
change rarely, and belong under code review. ``catalog.json`` next to this
module is the single source of truth for which voices exist; the preview
manifest (which previews have been generated, and where) lives in preview
storage — see ``app.services.preview_storage``.

Adding/updating a voice = edit catalog.json, ship, then POST
/tts/voices/reconcile to generate its previews (docs/TTS_VOICE_CATALOG.md).

Curation notes (carried over from the original seed):
- Deliberately excludes ids that only exist as dead/example references in the
  codebase: "bQQWtYx9EodAqMdkrNAc" (unused ELEVENLABS_VOICE_ID fallback),
  "iB2rIwm9cQCRGWoKDRtX" (template-generator example id), "manisha" (stale
  sarvam fallback superseded by the "shreya" default).
- sarvam speaker names verified against the installed pipecat SDK
  (SarvamTTSSpeakerV3); gemini voice names verified against
  GeminiTTSService.AVAILABLE_VOICES. Gemini "Despina" and google
  "en-IN-Chirp3-HD-Despina" are distinct voices that share a human name.
- google Chirp3-HD voice names encode model + locale, so those entries have
  no models field; gemini's model comes from GEMINI_TTS_MODEL at synth time.
"""

import json
import os
from functools import lru_cache
from typing import List

from app.schemas.breeze_buddy.tts_catalog import CatalogVoiceEntry

__all__ = ["get_enabled_voices", "get_all_voices", "CATALOG_PATH"]

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")


@lru_cache(maxsize=1)
def _load() -> tuple:
    """Parse + validate catalog.json once per process.

    Sync file I/O is confined to this first call; app startup primes the
    cache from the lifespan (via a thread) so requests never touch the disk.
    """
    with open(CATALOG_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    entries = tuple(CatalogVoiceEntry(**v) for v in raw["voices"])
    seen: set = set()
    for e in entries:
        pair = (e.provider, e.voice_id)
        if pair in seen:
            raise ValueError(f"Duplicate catalog entry: {pair}")
        seen.add(pair)
    return entries


def get_all_voices() -> List[CatalogVoiceEntry]:
    """Every catalog entry, including disabled ones."""
    return list(_load())


def get_enabled_voices() -> List[CatalogVoiceEntry]:
    """Catalog entries with enabled=true — the set served and reconciled."""
    return [v for v in _load() if v.enabled]
