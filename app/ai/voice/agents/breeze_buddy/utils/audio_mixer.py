"""Audio mixer utilities for Breeze Buddy voice agents.

Provides helpers for creating SoundfileMixer instances from template
configuration. Audio files must be pre-encoded at the correct sample rate
for each transport — no runtime resampling is performed.

File naming convention (in static/audio/):
  <name>_8k.mp3/.wav  — 8000 Hz mono  (telephony: Twilio/Plivo/Exotel/Telnyx)
  <name>_24k.mp3/.wav — 24000 Hz mono (Daily WebRTC)
"""

import os
from typing import Optional

import soundfile
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

from app.ai.voice.agents.breeze_buddy.template.types import BackgroundSoundFile
from app.core.logger import logger

# Base path for bundled audio assets
_AUDIO_PATH = "app/ai/voice/agents/breeze_buddy/static/audio"

# Mapping from BackgroundSoundFile enum → (8k filename, 24k filename)
_BACKGROUND_SOUND_FILE_MAP: dict[str, tuple[str, str]] = {
    BackgroundSoundFile.OFFICE_AMBIENCE: (
        "office-ambience_8k.mp3",
        "office-ambience_24k.mp3",
    ),
}

# Mapping from FillerSoundtrack enum value (str) → (8k filename, 24k filename).
# Keyed by string value so we don't need to import FillerSoundtrack here
# (avoids a potential circular import with types.py).
_FILLER_SOUNDTRACK_FILE_MAP: dict[str, tuple[str, str]] = {
    "typing": ("typing_music_realistic_8k.mp3", "typing_music_realistic_24k.mp3"),
    "dial-tone": ("dial-tone_8k.wav", "dial-tone_24k.wav"),
}


def _audio_file_path(filename: str) -> str:
    return os.path.join(_AUDIO_PATH, filename)


def _validate_audio_file(path: str) -> bool:
    """Return True if the file exists and is mono. Logs a warning otherwise."""
    if not os.path.exists(path):
        return False
    try:
        info = soundfile.info(path)
        if info.channels != 1:
            logger.warning(
                f"Audio file is not mono (channels={info.channels}), skipping: {path}"
            )
            return False
    except Exception as e:
        logger.warning(f"Failed to read audio file info for {path}: {e}")
        return False
    return True


def create_background_sound_mixer(
    template,
    sample_rate: int = 8000,
) -> Optional[SoundfileMixer]:
    """Build a SoundfileMixer from template audio config.

    Selects the pre-encoded file variant that matches `sample_rate`:
      8000  → *_8k files  (telephony)
      24000 → *_24k files (Daily)

    Registers:
    1. Ambient background sound (enable_background_sound + background_sound_file).
    2. Background music from any global function whose
       filler_audio.background_music_config.sound_file is set.

    Returns:
        SoundfileMixer instance, or None if no sounds are needed.
    """
    if not template or not template.configurations:
        return None

    config = template.configurations
    sound_files: dict[str, str] = {}
    default_sound: Optional[str] = None
    volume = 0.4

    # Pick index 0 for 8kHz (telephony), index 1 for 24kHz (Daily)
    file_idx = 0 if sample_rate == 8000 else 1

    # --- 1. Ambient background sound ---
    if config.enable_background_sound and config.background_sound_file:
        bg_key = (
            config.background_sound_file.value
            if hasattr(config.background_sound_file, "value")
            else config.background_sound_file
        )
        file_variants = _BACKGROUND_SOUND_FILE_MAP.get(bg_key)
        if file_variants:
            bg_path = _audio_file_path(file_variants[file_idx])
            if _validate_audio_file(bg_path):
                sound_files["background"] = bg_path
                default_sound = "background"
                volume = config.background_sound_volume
            else:
                logger.warning(
                    f"Background sound file not found or invalid: {file_variants[file_idx]}"
                )
        else:
            logger.warning(f"Unknown background sound key: {bg_key!r}")

    # --- 2. Per-function filler background music ---
    global_funcs = template.flow.get("global_functions", []) if template.flow else []
    for gf in global_funcs:
        filler_audio = gf.get("filler_audio") if isinstance(gf, dict) else None
        if not filler_audio:
            continue
        music_cfg = filler_audio.get("background_music_config") or {}
        if not music_cfg:
            continue
        soundtrack = music_cfg.get("sound_file")  # FillerSoundtrack enum value or None
        if not soundtrack:
            continue
        soundtrack_key = (
            soundtrack.value if hasattr(soundtrack, "value") else soundtrack
        )
        if soundtrack_key in sound_files:
            continue
        file_variants = _FILLER_SOUNDTRACK_FILE_MAP.get(soundtrack_key)
        if not file_variants:
            logger.warning(f"Unknown filler soundtrack: {soundtrack_key!r}")
            continue
        path = _audio_file_path(file_variants[file_idx])
        if _validate_audio_file(path):
            sound_files[soundtrack_key] = path
            if default_sound is None:
                default_sound = soundtrack_key
                volume = music_cfg.get("volume", 0.4)
        else:
            logger.warning(
                f"Filler soundtrack file not found or invalid: {file_variants[file_idx]}"
            )

    if not sound_files:
        return None

    if default_sound is None:
        default_sound = next(iter(sound_files))

    assert isinstance(default_sound, str)  # always set — sound_files is non-empty here

    has_ambient = "background" in sound_files
    start_mixing = has_ambient

    try:
        mixer = SoundfileMixer(
            sound_files=sound_files,
            default_sound=default_sound,
            volume=volume,
            mixing=start_mixing,
        )
        logger.info(
            f"Audio mixer created: sounds={list(sound_files.keys())}, "
            f"default={default_sound}, mixing={start_mixing}"
        )
        return mixer
    except Exception as e:
        logger.warning(f"Failed to create audio mixer: {e}")
        return None
