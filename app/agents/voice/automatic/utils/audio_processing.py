#!/usr/bin/env python3
"""
Audio Processing Utilities for Speaker Verification

This module provides audio processing utilities specifically designed for
speaker verification tasks, including filtering, feature extraction, and
waveform preprocessing.
"""

import numpy as np
import logging
from typing import Tuple, Optional
from scipy.ndimage import uniform_filter, gaussian_filter1d

logger = logging.getLogger(__name__)

# Audio processing constants
SAMPLE_RATE = 16000
N_FFT = 512
HOP_LENGTH = 256
MIN_DB = -80
MAX_DB = -20


def wiener_filter(
    signal: np.ndarray, noise_factor: float = 5.0, filter_size: int = 3
) -> np.ndarray:
    """
    Apply Wiener filtering for noise reduction.

    Args:
        signal: Input audio signal
        noise_factor: Factor for noise variance estimation
        filter_size: Size of the uniform filter for smoothing

    Returns:
        Filtered audio signal
    """
    if len(signal) == 0:
        return signal

    try:
        mean_val = np.mean(signal)
        variance = np.var(signal)

        # Avoid division by zero
        if variance == 0:
            return signal - mean_val

        # Estimate noise variance
        noise_variance = variance / noise_factor

        # Wiener filter calculation
        wiener_gain = variance / (variance + noise_variance)

        # Apply uniform filter for smoothing
        filtered = uniform_filter(signal, size=filter_size, mode="constant")

        # Apply Wiener gain
        return (signal - mean_val) * wiener_gain + mean_val
    except Exception as e:
        logger.warning(f"Wiener filter failed: {e}, returning original signal")
        return signal


def preprocess_audio(
    waveform: np.ndarray,
    apply_wiener: bool = True,
    apply_gaussian: bool = True,
    sigma: float = 1.0,
) -> np.ndarray:
    """
    Preprocess audio waveform for speaker verification.

    Args:
        waveform: Input audio waveform
        apply_wiener: Whether to apply Wiener filtering
        apply_gaussian: Whether to apply Gaussian filtering
        sigma: Standard deviation for Gaussian filter

    Returns:
        Preprocessed audio waveform
    """
    processed = waveform.copy()

    if apply_wiener:
        processed = wiener_filter(processed)

    if apply_gaussian:
        processed = gaussian_filter1d(processed, sigma=sigma)

    return processed


def calculate_audio_db(waveform: np.ndarray) -> float:
    """
    Calculate dB level of audio waveform.

    Args:
        waveform: Input audio waveform

    Returns:
        dB level of the audio
    """
    rms = np.sqrt(np.mean(waveform**2))
    if rms > 0:
        db_level = 20 * np.log10(rms)
    else:
        db_level = MIN_DB

    return max(db_level, MIN_DB)


def normalize_audio(waveform: np.ndarray, target_db: float = -20.0) -> np.ndarray:
    """
    Normalize audio to target dB level.

    Args:
        waveform: Input audio waveform
        target_db: Target dB level

    Returns:
        Normalized audio waveform
    """
    current_db = calculate_audio_db(waveform)
    if current_db == MIN_DB:
        return waveform

    # Calculate scaling factor
    db_diff = target_db - current_db
    scale_factor = 10 ** (db_diff / 20)

    # Apply scaling and clip to prevent overflow
    normalized = waveform * scale_factor
    return np.clip(normalized, -1.0, 1.0)


def extract_mfcc_features(
    waveform: np.ndarray, sr: int = SAMPLE_RATE, n_mfcc: int = 40
) -> Optional[np.ndarray]:
    """
    Extract MFCC features from audio waveform.

    Args:
        waveform: Input audio waveform
        sr: Sample rate
        n_mfcc: Number of MFCC coefficients

    Returns:
        MFCC features or None if extraction fails
    """
    try:
        import librosa

        if len(waveform) == 0:
            return np.zeros(n_mfcc)

        # Extract MFCC features
        mfcc = librosa.feature.mfcc(
            y=waveform, sr=sr, n_mfcc=n_mfcc, n_fft=N_FFT, hop_length=HOP_LENGTH
        )

        # Return mean across time axis
        return np.mean(mfcc, axis=1)

    except Exception as e:
        logger.error(f"MFCC extraction failed: {e}")
        return None


def validate_audio_quality(
    waveform: np.ndarray,
    min_duration: float = 0.5,
    min_db: float = -60.0,
    max_db: float = 0.0,
) -> Tuple[bool, str]:
    """
    Validate audio quality for speaker verification.

    Args:
        waveform: Input audio waveform
        min_duration: Minimum duration in seconds
        min_db: Minimum acceptable dB level
        max_db: Maximum acceptable dB level

    Returns:
        Tuple of (is_valid, reason)
    """
    # Check duration
    duration = len(waveform) / SAMPLE_RATE
    if duration < min_duration:
        return False, f"Audio too short: {duration:.2f}s < {min_duration}s"

    # Check audio level
    db_level = calculate_audio_db(waveform)
    if db_level < min_db:
        return False, f"Audio too quiet: {db_level:.1f}dB < {min_db}dB"

    if db_level > max_db:
        return False, f"Audio too loud: {db_level:.1f}dB > {max_db}dB"

    # Check for silence (all zeros)
    if np.all(waveform == 0):
        return False, "Audio is silent"

    # Check for clipping
    clipping_ratio = np.sum(np.abs(waveform) > 0.95) / len(waveform)
    if clipping_ratio > 0.01:  # More than 1% clipped
        return False, f"Audio clipped: {clipping_ratio*100:.1f}% samples"

    return True, "Audio quality acceptable"


def apply_voice_activity_detection(
    waveform: np.ndarray, energy_threshold: float | None = None
) -> np.ndarray:
    """
    Energy-based VAD with adaptive threshold and segment merging.
    """
    frame_length = int(0.025 * SAMPLE_RATE)  # 25ms
    hop_length = int(0.010 * SAMPLE_RATE)  # 10ms

    if len(waveform) < frame_length:
        return waveform

    # Frame energies
    energies = []
    for i in range(0, len(waveform) - frame_length + 1, hop_length):
        frame = waveform[i : i + frame_length]
        energies.append(np.mean(frame**2))
    energies = np.array(energies)

    # Adaptive threshold if not provided
    if energy_threshold is None:
        # 30% of median energy as baseline
        median_energy = np.median(energies[energies > 0])
        energy_threshold = median_energy * 0.3

    voice_flags = energies > energy_threshold

    # Extract voice segments
    voice_segments = []
    start_idx = None
    for i, is_voice in enumerate(voice_flags):
        sample_idx = i * hop_length
        if is_voice and start_idx is None:
            start_idx = sample_idx
        elif not is_voice and start_idx is not None:
            end_idx = min(sample_idx + frame_length, len(waveform))
            voice_segments.append((start_idx, end_idx))
            start_idx = None
    if start_idx is not None:
        voice_segments.append((start_idx, len(waveform)))

    # Merge segments that are <300ms apart
    merged = []
    for seg in voice_segments:
        if not merged:
            merged.append(seg)
        else:
            prev_start, prev_end = merged[-1]
            cur_start, cur_end = seg
            if cur_start - prev_end < 0.3 * SAMPLE_RATE:
                merged[-1] = (prev_start, cur_end)
            else:
                merged.append(seg)

    if not merged:
        return waveform  # fallback

    # Concatenate segments
    return np.concatenate([waveform[s:e] for s, e in merged])


def convert_frames_to_waveform(frames: list) -> np.ndarray:
    """
    Convert list of AudioRawFrame objects to numpy waveform.

    Args:
        frames: List of AudioRawFrame objects

    Returns:
        Combined waveform as numpy array
    """
    if not frames:
        return np.array([], dtype=np.float32)

    waveforms = []
    for frame in frames:
        # Convert bytes to numpy array
        waveform_np = (
            np.frombuffer(frame.audio, dtype=np.int16).astype(np.float32) / 32768.0
        )
        waveforms.append(waveform_np)

    return np.concatenate(waveforms) if waveforms else np.array([], dtype=np.float32)


def add_noise_for_robustness(
    waveform: np.ndarray, noise_level: float = 0.001
) -> np.ndarray:
    """
    Add small amount of noise to improve robustness.

    Args:
        waveform: Input audio waveform
        noise_level: Standard deviation of noise to add

    Returns:
        Waveform with added noise
    """
    if len(waveform) == 0:
        return waveform

    noise = np.random.normal(0, noise_level, size=waveform.shape)
    return waveform + noise


def detect_speech_boundaries(
    waveform: np.ndarray,
    energy_threshold: float = 0.01,
    min_speech_duration: float = 0.1,
) -> list:
    """
    Detect speech segment boundaries in audio.

    Args:
        waveform: Input audio waveform
        energy_threshold: Energy threshold for speech detection
        min_speech_duration: Minimum duration for valid speech segment

    Returns:
        List of (start_time, end_time) tuples in seconds
    """
    frame_length = int(0.025 * SAMPLE_RATE)  # 25ms frames
    hop_length = int(0.010 * SAMPLE_RATE)  # 10ms hop

    if len(waveform) < frame_length:
        return [(0, len(waveform) / SAMPLE_RATE)]

    # Compute energy for each frame
    energies = []
    for i in range(0, len(waveform) - frame_length + 1, hop_length):
        frame = waveform[i : i + frame_length]
        energy = np.mean(frame**2)
        energies.append(energy)

    energies = np.array(energies)
    voice_frames = energies > energy_threshold

    # Find speech segments
    segments = []
    start_frame = None

    for i, is_voice in enumerate(voice_frames):
        if is_voice and start_frame is None:
            start_frame = i
        elif not is_voice and start_frame is not None:
            # Convert frame indices to time
            start_time = start_frame * hop_length / SAMPLE_RATE
            end_time = i * hop_length / SAMPLE_RATE

            # Check minimum duration
            if end_time - start_time >= min_speech_duration:
                segments.append((start_time, end_time))

            start_frame = None

    # Handle case where speech continues to end
    if start_frame is not None:
        start_time = start_frame * hop_length / SAMPLE_RATE
        end_time = len(waveform) / SAMPLE_RATE
        if end_time - start_time >= min_speech_duration:
            segments.append((start_time, end_time))

    return segments if segments else [(0, len(waveform) / SAMPLE_RATE)]
