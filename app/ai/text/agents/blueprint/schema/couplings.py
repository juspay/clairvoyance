"""Curated field-to-field couplings.

These are the invariants the template system enforces (at runtime, via
Pydantic validators or documented provider constraints) that Blueprint
must mirror when gathering configuration from the user.

Source of truth for the invariants themselves is
``app/ai/voice/agents/breeze_buddy/template/types.py``. This module
merely encodes them in a machine-readable form the planner can query.

When ``types.py`` gains a new coupling, add it here.
"""

from app.ai.text.agents.blueprint.schema.models import (
    ConstraintKind,
    Coupling,
    CouplingEffect,
)

# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

_STT_DEEPGRAM_REQUIRES_CONFIG = Coupling(
    name="stt_deepgram_config_required",
    trigger={"configurations.stt_configuration.provider": "deepgram"},
    effects=[
        CouplingEffect(
            path="configurations.stt_configuration.deepgram",
            kind=ConstraintKind.REQUIRED,
            reason="Deepgram-specific config (model, endpointing) is required when provider=deepgram.",
        )
    ],
    reason="Deepgram provider needs its own config block.",
)

_STT_SMART_TURN_REQUIRES_DEEPGRAM = Coupling(
    name="smart_turn_requires_deepgram",
    trigger={"configurations.stt_configuration.turn_detection": "smart_turn"},
    effects=[
        CouplingEffect(
            path="configurations.stt_configuration.provider",
            kind=ConstraintKind.EQUALS,
            value="deepgram",
            reason="SmartTurn only runs atop Deepgram transcripts.",
        ),
        CouplingEffect(
            path="configurations.stt_configuration.smart_turn",
            kind=ConstraintKind.REQUIRED,
            reason="SmartTurn model config (stop_secs, max_duration) must be present.",
        ),
        CouplingEffect(
            path="configurations.stt_configuration.user_speech_timeout",
            kind=ConstraintKind.EQUALS,
            value=0.0,
            reason="SmartTurn handles endpointing; timeout must be 0.",
        ),
    ],
    reason="turn_detection=smart_turn implies Deepgram + smart_turn block + no timeout.",
)

_STT_PAYLOAD_LANG_SINGLE_SLOT = Coupling(
    name="payload_language_selection_single_slot",
    trigger={"configurations.stt_configuration.payload_based_language_selection": True},
    effects=[
        CouplingEffect(
            path="configurations.stt_configuration.language",
            kind=ConstraintKind.REQUIRED,
            reason="Payload-based selection picks from a single language slot.",
        ),
    ],
    reason="Language must be set (as a single value) when payload selection is on.",
)

# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

_TTS_SELECTION_REQUIRES_PROVIDERS = Coupling(
    name="tts_selection_requires_providers",
    trigger={"configurations.tts_selection_config.enabled": True},
    effects=[
        CouplingEffect(
            path="configurations.tts_selection_config.providers",
            kind=ConstraintKind.REQUIRED,
            reason="LLM-based TTS selection picks from this providers list.",
        ),
        CouplingEffect(
            path="configurations.tts_selection_config.prompt",
            kind=ConstraintKind.REQUIRED,
            reason="Selection prompt tells the LLM how to choose.",
        ),
    ],
    reason="TTS selection needs something to pick from and instructions to pick by.",
)

# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

_BACKGROUND_SOUND_ENABLED_REQUIRES_FILE = Coupling(
    name="background_sound_needs_file",
    trigger={"configurations.enable_background_sound": True},
    effects=[
        CouplingEffect(
            path="configurations.background_sound_file",
            kind=ConstraintKind.REQUIRED,
            reason="Something has to actually play.",
        ),
    ],
    reason="enable_background_sound=True without a file is a no-op.",
)

# ---------------------------------------------------------------------------
# Warm transfer
# ---------------------------------------------------------------------------
#
# transfer_number only matters when the flow exposes the
# ``connect_to_live_agent`` builtin function. We can't fully express this
# as a field-level coupling (the "trigger" is structural, not a value in
# the draft) — so the Validator specialist checks it separately. Leaving
# a placeholder here so the spec-check catches it.

# ---------------------------------------------------------------------------
# The exported list.
# ---------------------------------------------------------------------------

COUPLINGS: list[Coupling] = [
    _STT_DEEPGRAM_REQUIRES_CONFIG,
    _STT_SMART_TURN_REQUIRES_DEEPGRAM,
    _STT_PAYLOAD_LANG_SINGLE_SLOT,
    _TTS_SELECTION_REQUIRES_PROVIDERS,
    _BACKGROUND_SOUND_ENABLED_REQUIRES_FILE,
]


__all__ = ["COUPLINGS"]
