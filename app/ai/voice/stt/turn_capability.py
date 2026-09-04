"""Whether an STT service detects user turn boundaries itself.

Since pipecat 1.5 a server-side turn-detecting STT (Soniox with
``vad_force_turn_endpoint=False``, Deepgram Flux, AssemblyAI, …) broadcasts
``ProposedUserStoppedSpeakingFrame`` when it decides the user finished, and
advertises ``ExternalUserTurnStrategies`` on its metadata frame. A finalized
``TranscriptionFrame`` no longer means "turn over" — it means "these words are
confirmed" and arrives per segment while the user is still speaking.

Callers use this to pick the stop strategy: listen for the service's proposal
(``ExternalUserTurnStopStrategy``) instead of firing on transcript arrival,
which would end the turn mid-sentence.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logger import logger


def stt_proposes_turn_boundaries(stt: Optional[Any]) -> bool:
    """Return True when ``stt`` decides turn ends itself and proposes them.

    Probes the service's own metadata frame — the same one the aggregator reads
    at start — so the answer tracks the service's runtime configuration rather
    than a provider name we would have to keep in sync.

    Fails CLOSED: an absent service, an older service without the metadata hook,
    or any probe error returns False, which keeps the locally pinned stop
    strategy (previous behavior) rather than waiting for proposals that may
    never come.
    """
    if stt is None:
        return False

    try:
        frame = stt.service_metadata_frame()
    except Exception as exc:  # noqa: BLE001 - probe must never break call setup
        logger.warning(
            f"Could not read turn-detection capability from "
            f"{type(stt).__name__}: {exc}. Assuming it proposes no turns."
        )
        return False

    return getattr(frame, "user_turn_strategies", None) is not None
