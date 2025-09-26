#!/usr/bin/env python3
"""
Speaker Verification Processor for Pipecat Voice Pipeline

SYSTEM FLOW & FUNCTION CALLS:
=============================

1. INITIALIZATION:
   Container Start → SpeakerVerificationService.__init__() → initialize() →
   _load_reference_embeddings() → np.load(embeddings/*.npy) → Service Ready (1-2s)

2. ENROLLMENT PHASE (First N Queries):
   process_frame(AudioRawFrame) → _stop_collecting_and_verify() →
   _handle_enrollment_query() → enroll_speaker() →
   _extract_speech_segments() → augment_input_audio_for_inference() →
   extract_augmented_speaker_embedding() → np.save(embeddings/speaker.npy)
   Optional: _save_debug_audio_enrollment() (if SPEAKER_VERIFICATION_DEBUG=true)

3. VERIFICATION PHASE (After Enrollment):
   process_frame(AudioRawFrame) → _stop_collecting_and_verify() →
   _handle_verification() → verify_speaker() →
   ensemble_speaker_verification() OR extract_augmented_speaker_embedding() →
   cosine_similarity(input_embedding, reference_embedding) →
   _forward_filtered_frames() OR _send_silenced_audio()

4. AUDIO AUGMENTATION PIPELINE:
   augment_input_audio_for_inference() → [speed_perturbation, volume_scaling,
   noise_addition, pitch_shifting, wiener_filter(), gaussian_filter1d()] →
   extract_embedding_standard() → weighted_average → final_embedding

5. STORAGE:
   Production: embeddings/speaker.npy (768B) - Fast startup
   Debug: + debug_audio/*.wav (if debug enabled) - For analysis

FEATURES:
- Real-time speaker verification using SpeechBrain ECAPA-TDNN models
- Test-Time Augmentation (17+ variants per audio for robustness)
- Ensemble verification with multiple strategies
- Embeddings-only storage for 30x faster startup
- Optional debug audio saving controlled by environment flag
- Audio filtering to block unauthorized speakers
"""

import asyncio
import os
import glob
import time
import uuid
import numpy as np
import torch
import librosa
import logging
from typing import List, Dict, Any, Optional
from collections import defaultdict

try:
    from speechbrain.inference.speaker import SpeakerRecognition
    from speechbrain.pretrained import EncoderClassifier

    SPEECHBRAIN_AVAILABLE = True
except ImportError:
    try:
        from speechbrain.pretrained import EncoderClassifier as SpeakerRecognition
        from speechbrain.pretrained import EncoderClassifier

        SPEECHBRAIN_AVAILABLE = True
    except ImportError:
        SPEECHBRAIN_AVAILABLE = False
        SpeakerRecognition = None
        EncoderClassifier = None

# Try to import pyannote.audio for diarization (much better than SpeechBrain)
try:
    from pyannote.audio import Pipeline as PyannoteePipeline

    PYANNOTE_AVAILABLE = True
except ImportError:
    PyannoteePipeline = None
    PYANNOTE_AVAILABLE = False

# Legacy SpeechBrain diarization (fallback)
try:
    from speechbrain.inference.diarization import SpeakerDiarization as Diarization

    SPEECHBRAIN_DIARIZATION_AVAILABLE = True
except ImportError:
    Diarization = None
    SPEECHBRAIN_DIARIZATION_AVAILABLE = False

DIARIZATION_AVAILABLE = PYANNOTE_AVAILABLE or SPEECHBRAIN_DIARIZATION_AVAILABLE

from sklearn.metrics.pairwise import cosine_similarity
from scipy.ndimage import gaussian_filter1d

# Removed find_peaks import - not used

# Try to import torchaudio for advanced augmentation
try:
    import torchaudio
    import torchaudio.transforms as T

    TORCHAUDIO_AVAILABLE = True
except ImportError:
    TORCHAUDIO_AVAILABLE = False
    logger.warning(
        "[AUGMENTATION] torchaudio not available, using basic augmentation only"
    )

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    StartFrame,
    StartInterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.agents.voice.automatic.utils.audio_processing import (
    wiener_filter,
    preprocess_audio,
)

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
N_FFT = 512
HOP_LENGTH = 256
MIN_DB = -80
MAX_DB = -20

EMBEDDING_SIMILARITY_THRESHOLD = 0.60
MODEL_CONFIDENCE_THRESHOLD = 0.60
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

ENABLE_TEST_TIME_AUGMENTATION = True
ENABLE_ENSEMBLE_VERIFICATION = True
USE_PREPROCESSING_VARIANTS = True

AUGMENTATION_SPEED_FACTORS = [0.9, 1.0, 1.1]
AUGMENTATION_VOLUME_FACTORS = [0.8, 1.0, 1.2]
AUGMENTATION_NOISE_LEVELS = [0.001, 0.003, 0.005]
AUGMENTATION_WEIGHTS = [0.4, 1.0, 0.4]


def augment_input_audio_for_inference(waveform, sample_rate=SAMPLE_RATE):
    """
    Generate multiple augmented versions of input audio for test-time augmentation.
    Returns a list of (augmented_waveform, weight) tuples.
    """
    if not ENABLE_TEST_TIME_AUGMENTATION:
        return [(waveform, 1.0)]

    augmented_samples = []

    try:
        augmented_samples.append((waveform.copy(), 1.0))

        for i, speed_factor in enumerate(AUGMENTATION_SPEED_FACTORS):
            if speed_factor != 1.0:
                try:
                    if TORCHAUDIO_AVAILABLE:
                        waveform_tensor = torch.tensor(waveform, dtype=torch.float32)
                        speed_perturb = T.SpeedPerturbation(sample_rate, [speed_factor])
                        augmented_tensor, _ = speed_perturb(
                            waveform_tensor.unsqueeze(0)
                        )
                        augmented_waveform = augmented_tensor.squeeze(0).numpy()
                    else:
                        target_length = int(len(waveform) / speed_factor)
                        augmented_waveform = np.interp(
                            np.linspace(0, len(waveform), target_length),
                            np.arange(len(waveform)),
                            waveform,
                        )

                    weight = (
                        AUGMENTATION_WEIGHTS[i]
                        if i < len(AUGMENTATION_WEIGHTS)
                        else 0.3
                    )
                    augmented_samples.append((augmented_waveform, weight))
                except Exception as e:
                    logger.warning(
                        f"[AUGMENTATION] Speed perturbation {speed_factor} failed: {e}"
                    )

        for volume_factor in AUGMENTATION_VOLUME_FACTORS:
            if volume_factor != 1.0:
                try:
                    augmented_waveform = waveform * volume_factor
                    augmented_waveform = np.clip(augmented_waveform, -1.0, 1.0)
                    augmented_samples.append((augmented_waveform, 0.4))
                except Exception as e:
                    logger.warning(
                        f"[AUGMENTATION] Volume perturbation {volume_factor} failed: {e}"
                    )

        for noise_level in AUGMENTATION_NOISE_LEVELS:
            try:
                noise = np.random.normal(0, noise_level, size=waveform.shape)
                augmented_waveform = waveform + noise
                augmented_waveform = np.clip(augmented_waveform, -1.0, 1.0)
                augmented_samples.append((augmented_waveform, 0.3))
            except Exception as e:
                logger.warning(
                    f"[AUGMENTATION] Noise addition {noise_level} failed: {e}"
                )

        if TORCHAUDIO_AVAILABLE:
            try:
                waveform_tensor = torch.tensor(waveform, dtype=torch.float32).unsqueeze(
                    0
                )
                for pitch_shift in [-2, 2]:
                    try:
                        pitch_shifter = T.PitchShift(sample_rate, n_steps=pitch_shift)
                        augmented_tensor = pitch_shifter(waveform_tensor)
                        augmented_waveform = augmented_tensor.squeeze(0).numpy()
                        augmented_samples.append((augmented_waveform, 0.3))
                    except Exception as e:
                        logger.warning(
                            f"[AUGMENTATION] Pitch shift {pitch_shift} failed: {e}"
                        )
            except Exception as e:
                logger.warning(
                    f"[AUGMENTATION] Advanced torchaudio augmentations failed: {e}"
                )

        if USE_PREPROCESSING_VARIANTS:
            try:
                for noise_factor in [3.0, 5.0, 10.0]:
                    try:
                        augmented_waveform = wiener_filter(
                            waveform, noise_factor=noise_factor
                        )
                        augmented_samples.append((augmented_waveform, 0.3))
                    except Exception as e:
                        logger.warning(
                            f"[AUGMENTATION] Wiener filtering {noise_factor} failed: {e}"
                        )

                for sigma in [0.5, 1.0, 2.0]:
                    try:
                        augmented_waveform = gaussian_filter1d(waveform, sigma=sigma)
                        augmented_samples.append((augmented_waveform, 0.2))
                    except Exception as e:
                        logger.warning(
                            f"[AUGMENTATION] Gaussian smoothing {sigma} failed: {e}"
                        )

                for target_rms in [0.1, 0.2, 0.3]:
                    try:
                        current_rms = np.sqrt(np.mean(waveform**2))
                        if current_rms > 0:
                            scale_factor = target_rms / current_rms
                            augmented_waveform = waveform * scale_factor
                            augmented_waveform = np.clip(augmented_waveform, -1.0, 1.0)
                            augmented_samples.append((augmented_waveform, 0.2))
                    except Exception as e:
                        logger.warning(
                            f"[AUGMENTATION] RMS normalization {target_rms} failed: {e}"
                        )
            except Exception as e:
                logger.warning(f"[AUGMENTATION] Preprocessing variants failed: {e}")

        logger.debug(
            f"[AUGMENTATION] Generated {len(augmented_samples)} augmented samples"
        )
        return augmented_samples

    except Exception as e:
        logger.error(f"[AUGMENTATION] Augmentation failed, returning original: {e}")
        return [(waveform, 1.0)]


def extract_augmented_speaker_embedding(waveform, model, sample_rate=SAMPLE_RATE):
    """
    Extract speaker embedding using test-time augmentation.
    Returns weighted average of embeddings from multiple augmented versions.
    """
    if not ENABLE_TEST_TIME_AUGMENTATION:
        # Fallback to standard extraction
        return extract_embedding_standard(waveform, model)

    try:
        # Generate augmented samples
        augmented_samples = augment_input_audio_for_inference(waveform, sample_rate)

        embeddings = []
        weights = []

        for aug_waveform, weight in augmented_samples:
            try:
                # Extract embedding from augmented sample
                embedding = extract_embedding_standard(aug_waveform, model)
                if embedding is not None:
                    embeddings.append(embedding)
                    weights.append(weight)
            except Exception as e:
                logger.warning(
                    f"[AUGMENTATION] Failed to extract embedding from augmented sample: {e}"
                )

        if not embeddings:
            logger.warning(
                "[AUGMENTATION] No embeddings extracted, falling back to standard method"
            )
            return extract_embedding_standard(waveform, model)

        # Convert to numpy arrays
        embeddings = np.array(embeddings)
        weights = np.array(weights)

        # Normalize weights
        weights = weights / np.sum(weights)

        # Compute weighted average
        weighted_embedding = np.average(embeddings, axis=0, weights=weights)

        # L2 normalize the final embedding
        norm = np.linalg.norm(weighted_embedding)
        if norm > 0:
            weighted_embedding = weighted_embedding / norm

        logger.debug(f"[AUGMENTATION] Combined {len(embeddings)} embeddings with TTA")
        return weighted_embedding

    except Exception as e:
        logger.error(f"[AUGMENTATION] Augmented embedding extraction failed: {e}")
        # Fallback to standard extraction
        return extract_embedding_standard(waveform, model)


def extract_embedding_standard(waveform, model):
    """Standard embedding extraction without augmentation."""
    try:
        min_samples = int(1.0 * SAMPLE_RATE)
        if len(waveform) < min_samples:
            waveform = np.pad(
                waveform, (0, min_samples - len(waveform)), mode="constant"
            )

        waveform_tensor = (
            torch.tensor(waveform, dtype=torch.float32).unsqueeze(0).to(DEVICE)
        )

        with torch.no_grad():
            embedding = model.encode_batch(waveform_tensor).squeeze().cpu().numpy()

        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding
    except Exception as e:
        logger.error(f"[EMBEDDING] Standard extraction failed: {e}")
        return None


def ensemble_speaker_verification(
    waveform, reference_embeddings, model, target_speaker_id=None
):
    """
    Advanced verification using multiple prediction strategies and ensemble methods.
    """
    if not ENABLE_ENSEMBLE_VERIFICATION:
        # Fallback to standard verification
        embedding = extract_embedding_standard(waveform, model)
        if embedding is None:
            return {"verified": False, "confidence": 0.0, "method": "standard_failed"}

        # Compare against references
        if target_speaker_id and target_speaker_id in reference_embeddings:
            similarity = cosine_similarity(
                [embedding], [reference_embeddings[target_speaker_id]]
            )[0][0]
            return {
                "verified": similarity >= EMBEDDING_SIMILARITY_THRESHOLD,
                "confidence": similarity,
                "method": "standard",
            }

        return {"verified": False, "confidence": 0.0, "method": "standard_no_reference"}

    try:
        verification_results = []

        # Method 1: Standard embedding extraction
        try:
            embedding_standard = extract_embedding_standard(waveform, model)
            if (
                embedding_standard is not None
                and target_speaker_id in reference_embeddings
            ):
                similarity = cosine_similarity(
                    [embedding_standard], [reference_embeddings[target_speaker_id]]
                )[0][0]
                verification_results.append(
                    {
                        "method": "standard",
                        "verified": similarity >= EMBEDDING_SIMILARITY_THRESHOLD,
                        "confidence": similarity,
                        "weight": 0.3,
                    }
                )
        except Exception as e:
            logger.warning(f"[ENSEMBLE] Standard method failed: {e}")

        # Method 2: Augmented embedding extraction (higher weight)
        try:
            embedding_augmented = extract_augmented_speaker_embedding(waveform, model)
            if (
                embedding_augmented is not None
                and target_speaker_id in reference_embeddings
            ):
                similarity = cosine_similarity(
                    [embedding_augmented], [reference_embeddings[target_speaker_id]]
                )[0][0]
                verification_results.append(
                    {
                        "method": "augmented",
                        "verified": similarity >= EMBEDDING_SIMILARITY_THRESHOLD,
                        "confidence": similarity,
                        "weight": 0.5,  # Higher weight for augmented method
                    }
                )
        except Exception as e:
            logger.warning(f"[ENSEMBLE] Augmented method failed: {e}")

        # Method 3: Preprocessing variants
        if USE_PREPROCESSING_VARIANTS:
            preprocessing_methods = [
                ("wiener", lambda w: wiener_filter(w, noise_factor=5.0)),
                ("gaussian", lambda w: gaussian_filter1d(w, sigma=1.0)),
                (
                    "volume_norm",
                    lambda w: w * (0.2 / max(np.sqrt(np.mean(w**2)), 1e-8)),
                ),
            ]

            for method_name, preprocessor in preprocessing_methods:
                try:
                    preprocessed_waveform = preprocessor(waveform.copy())
                    embedding_preprocessed = extract_embedding_standard(
                        preprocessed_waveform, model
                    )
                    if (
                        embedding_preprocessed is not None
                        and target_speaker_id in reference_embeddings
                    ):
                        similarity = cosine_similarity(
                            [embedding_preprocessed],
                            [reference_embeddings[target_speaker_id]],
                        )[0][0]
                        verification_results.append(
                            {
                                "method": f"preprocessed_{method_name}",
                                "verified": similarity
                                >= EMBEDDING_SIMILARITY_THRESHOLD,
                                "confidence": similarity,
                                "weight": 0.2,
                            }
                        )
                except Exception as e:
                    logger.warning(
                        f"[ENSEMBLE] Preprocessing method {method_name} failed: {e}"
                    )

        if not verification_results:
            logger.warning("[ENSEMBLE] No verification methods succeeded")
            return {"verified": False, "confidence": 0.0, "method": "ensemble_failed"}

        # Weighted voting and confidence aggregation
        total_weight = sum(result["weight"] for result in verification_results)
        verified_weight = sum(
            result["weight"] for result in verification_results if result["verified"]
        )
        weighted_confidence = (
            sum(
                result["confidence"] * result["weight"]
                for result in verification_results
            )
            / total_weight
        )

        verification_ratio = verified_weight / total_weight
        final_verified = verification_ratio >= 0.5  # Majority vote

        logger.info(
            f"[ENSEMBLE] Used {len(verification_results)} methods, ratio: {verification_ratio:.3f}, confidence: {weighted_confidence:.3f}"
        )

        return {
            "verified": final_verified,
            "confidence": weighted_confidence,
            "method": "ensemble",
            "details": {
                "methods_used": len(verification_results),
                "verification_ratio": verification_ratio,
                "individual_results": verification_results,
            },
        }

    except Exception as e:
        logger.error(f"[ENSEMBLE] Ensemble verification failed: {e}")
        # Final fallback to standard method
        try:
            embedding = extract_embedding_standard(waveform, model)
            if embedding is not None and target_speaker_id in reference_embeddings:
                similarity = cosine_similarity(
                    [embedding], [reference_embeddings[target_speaker_id]]
                )[0][0]
                return {
                    "verified": similarity >= EMBEDDING_SIMILARITY_THRESHOLD,
                    "confidence": similarity,
                    "method": "fallback_standard",
                }
        except:
            pass

        return {
            "verified": False,
            "confidence": 0.0,
            "method": "ensemble_total_failure",
        }


class SpeakerVerificationService:
    """Speaker verification service with embeddings-only storage and optional debug audio."""

    def __init__(
        self, embeddings_dir: str = "embeddings", enable_augmentation: bool = True
    ):
        self.embeddings_dir = embeddings_dir
        self.model = None
        self.diarization_model = None
        self.diarization_type = None  # "pyannote" or "speechbrain"
        self.reference_embeddings = {}
        self.initialized = False
        self.diarization_enabled = False
        self.enable_augmentation = enable_augmentation

        self.debug_enabled = (
            os.environ.get("SPEAKER_VERIFICATION_DEBUG", "false").lower() == "true"
        )
        self.debug_audio_dir = "debug_audio" if self.debug_enabled else None

        if not enable_augmentation:
            global ENABLE_TEST_TIME_AUGMENTATION, ENABLE_ENSEMBLE_VERIFICATION, USE_PREPROCESSING_VARIANTS
            ENABLE_TEST_TIME_AUGMENTATION = False
            ENABLE_ENSEMBLE_VERIFICATION = False
            USE_PREPROCESSING_VARIANTS = False
            logger.debug("[SPEAKER_VERIFICATION] Augmentation disabled")
        else:
            logger.debug("[SPEAKER_VERIFICATION] Augmentation enabled")

        os.makedirs(self.embeddings_dir, exist_ok=True)
        if self.debug_enabled:
            os.makedirs(self.debug_audio_dir, exist_ok=True)
            logger.debug(
                f"[SPEAKER_VERIFICATION] Debug mode enabled: {os.path.abspath(self.debug_audio_dir)}"
            )

        logger.debug(
            f"[SPEAKER_VERIFICATION] Embeddings: {os.path.abspath(self.embeddings_dir)}"
        )
        logger.debug(
            f"[SPEAKER_VERIFICATION] Storage: {'Embeddings + Debug Audio' if self.debug_enabled else 'Embeddings Only'}"
        )

    async def initialize(self) -> bool:
        """Initialize the SpeechBrain models and load reference embeddings."""
        if not SPEECHBRAIN_AVAILABLE:
            logger.error(
                "[SPEAKER_VERIFICATION] ❌ SpeechBrain not available. Install with: pip install speechbrain"
            )
            return False

        try:
            logger.info(
                "[SPEAKER_VERIFICATION] Loading SpeechBrain ECAPA-TDNN model..."
            )
            # Use EncoderClassifier which is proven to work reliably
            self.model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir="pretrained_models/spkrec-ecapa-voxceleb",
            )
            self.model.to(DEVICE)

            # Try to load diarization model - prefer pyannote.audio
            if PYANNOTE_AVAILABLE:
                try:
                    logger.info(
                        "[SPEAKER_VERIFICATION] Loading pyannote.audio diarization pipeline..."
                    )
                    # Note: This requires HuggingFace authentication for the pretrained model
                    # For now, we'll initialize it lazily when first needed to avoid auth issues
                    self.diarization_model = None  # Will be loaded lazily
                    self.diarization_type = "pyannote"
                    self.diarization_enabled = True
                    logger.info(
                        "[SPEAKER_VERIFICATION] ✅ Pyannote.audio diarization ready (will load on first use)"
                    )
                except Exception as e:
                    logger.warning(
                        f"[SPEAKER_VERIFICATION] ⚠️ Pyannote.audio setup failed: {e}"
                    )
                    self.diarization_enabled = False
            elif SPEECHBRAIN_DIARIZATION_AVAILABLE and Diarization:
                try:
                    logger.info(
                        "[SPEAKER_VERIFICATION] Loading SpeechBrain diarization model..."
                    )
                    self.diarization_model = Diarization.from_hparams(
                        source="speechbrain/speaker-diarization",
                        savedir="pretrained_models/speaker-diarization",
                    )
                    self.diarization_type = "speechbrain"
                    self.diarization_enabled = True
                    logger.info(
                        "[SPEAKER_VERIFICATION] ✅ SpeechBrain diarization model loaded successfully"
                    )
                except Exception as e:
                    logger.warning(
                        f"[SPEAKER_VERIFICATION] ⚠️ SpeechBrain diarization model failed to load: {e}"
                    )
                    logger.info(
                        "[SPEAKER_VERIFICATION] Continuing without diarization..."
                    )
                    self.diarization_enabled = False
            else:
                logger.info(
                    "[SPEAKER_VERIFICATION] No diarization available, using full audio segments"
                )
                self.diarization_enabled = False

            # Load existing enrollments
            await self._load_reference_embeddings()

            self.initialized = True
            diar_info = (
                f"{self.diarization_type}" if self.diarization_enabled else "disabled"
            )
            logger.info(
                f"[SPEAKER_VERIFICATION] ✅ Service initialized successfully (diarization: {diar_info})"
            )
            return True

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] ❌ Initialization failed: {e}")
            return False

    async def _load_reference_embeddings(self):
        """Load reference embeddings from embeddings directory (fast startup)."""
        self.reference_embeddings = {}

        embedding_files = glob.glob(f"{self.embeddings_dir}/*.npy")
        if not embedding_files:
            logger.info("[SPEAKER_VERIFICATION] No embedding files found")
            return

        for embedding_file in embedding_files:
            try:
                speaker_name = os.path.splitext(os.path.basename(embedding_file))[0]

                # Load pre-computed embedding directly
                embedding = np.load(embedding_file)

                # Validate embedding
                if embedding is not None and len(embedding) > 0:
                    self.reference_embeddings[speaker_name] = embedding
                    logger.info(
                        f"[SPEAKER_VERIFICATION] ✅ Loaded embedding for '{speaker_name}' (shape: {embedding.shape})"
                    )
                else:
                    logger.warning(
                        f"[SPEAKER_VERIFICATION] ❌ Invalid embedding in: {embedding_file}"
                    )

            except Exception as e:
                logger.error(
                    f"[SPEAKER_VERIFICATION] Failed to load {embedding_file}: {e}"
                )

        logger.info(
            f"[SPEAKER_VERIFICATION] ⚡ Fast startup: Loaded {len(self.reference_embeddings)} speaker embeddings"
        )

    async def enroll_speaker(
        self, waveform: np.ndarray, speaker_id: str, step: int = 1
    ) -> bool:
        """
        Enroll a new speaker by saving embeddings (and optionally debug audio).
        Uses embeddings-only storage for fast startup with optional debug audio saving.

        Args:
            waveform: Audio waveform for enrollment
            speaker_id: Speaker identifier
            step: Enrollment step number

        Returns:
            Success status
        """
        try:
            import datetime

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # Ensure minimum duration for enrollment
            duration = len(waveform) / SAMPLE_RATE
            if duration < 1.0:  # At least 1 second of speech for enrollment
                logger.error(
                    f"[SPEAKER_VERIFICATION] Enrollment audio too short: {duration:.2f}s"
                )
                return False

            # Extract speech segments using diarization for clean enrollment
            speech_segments = await self._extract_speech_segments(waveform)

            if speech_segments:
                # Use the longest speech segment for enrollment (should be the primary speaker)
                longest_segment = max(speech_segments, key=len)
                logger.info(
                    f"[SPEAKER_VERIFICATION] Using longest speech segment for enrollment: {len(longest_segment)} samples from {len(speech_segments)} segments"
                )
                enrollment_audio = longest_segment
            else:
                logger.warning(
                    f"[SPEAKER_VERIFICATION] No speech segments found in enrollment step {step}, using full audio"
                )
                enrollment_audio = waveform

            # Extract embedding with augmentation
            if ENABLE_TEST_TIME_AUGMENTATION:
                logger.info(
                    f"[SPEAKER_VERIFICATION] 🔧 Applying comprehensive augmentation for enrollment step {step}"
                )

                # Generate multiple augmented versions for more robust reference features
                augmented_samples = augment_input_audio_for_inference(
                    enrollment_audio, SAMPLE_RATE
                )

                # Extract embeddings from all augmented samples
                augmented_embeddings = []
                for aug_waveform, weight in augmented_samples:
                    try:
                        # Apply additional preprocessing to each augmented sample
                        proc = wiener_filter(aug_waveform)
                        proc = gaussian_filter1d(proc, sigma=1.0)
                        # Add controlled noise for robustness
                        noise = np.random.normal(0, 0.001, size=proc.shape)
                        proc = proc + noise
                        # Extract embedding
                        embedding = extract_embedding_standard(proc, self.model)
                        if embedding is not None:
                            augmented_embeddings.append((embedding, weight))
                    except Exception as e:
                        logger.warning(
                            f"[SPEAKER_VERIFICATION] Failed to extract embedding from augmented sample: {e}"
                        )

                if augmented_embeddings:
                    # Compute weighted average of embeddings from all augmented samples
                    embeddings_arrays = [e[0] for e in augmented_embeddings]
                    weights = [e[1] for e in augmented_embeddings]

                    # Normalize weights
                    weights = np.array(weights)
                    weights = weights / np.sum(weights)

                    # Compute weighted average
                    final_embedding = np.average(
                        embeddings_arrays, axis=0, weights=weights
                    )

                    logger.info(
                        f"[SPEAKER_VERIFICATION] ✅ Generated augmented embedding: {len(augmented_embeddings)} variants combined"
                    )
                else:
                    # Fallback to basic embedding if augmentation fails
                    final_embedding = await self._extract_embedding(
                        enrollment_audio, use_augmentation=False
                    )
                    logger.info(
                        f"[SPEAKER_VERIFICATION] ⚠️ Augmentation failed, using standard embedding"
                    )
            else:
                # Standard embedding when TTA is disabled
                final_embedding = await self._extract_embedding(
                    enrollment_audio, use_augmentation=False
                )
                logger.info(
                    f"[SPEAKER_VERIFICATION] Using standard embedding extraction"
                )

            if final_embedding is None:
                logger.error(
                    f"[SPEAKER_VERIFICATION] Failed to extract embedding for enrollment step {step}"
                )
                return False

            # === EMBEDDINGS-ONLY STORAGE ===
            # Save embedding to .npy file
            embedding_filename = f"{speaker_id}.npy"
            embedding_path = os.path.join(self.embeddings_dir, embedding_filename)

            # If multiple enrollment steps, accumulate embeddings
            if os.path.exists(embedding_path):
                try:
                    existing_embedding = np.load(embedding_path)
                    # Average with existing embedding
                    combined_embedding = (existing_embedding + final_embedding) / 2.0
                    np.save(embedding_path, combined_embedding)
                    logger.info(
                        f"[SPEAKER_VERIFICATION] 🔄 Updated embedding by averaging with existing"
                    )
                except Exception as e:
                    logger.warning(
                        f"[SPEAKER_VERIFICATION] Failed to load existing embedding: {e}, using new one"
                    )
                    np.save(embedding_path, final_embedding)
            else:
                np.save(embedding_path, final_embedding)

            # === DEBUG AUDIO STORAGE (Optional) ===
            if self.debug_enabled:
                self._save_debug_audio_enrollment(
                    enrollment_audio, speaker_id, step, timestamp, duration
                )

            logger.info(f"[SPEAKER_VERIFICATION] 💾 ENROLLMENT STEP {step} COMPLETED:")
            logger.info(f"[SPEAKER_VERIFICATION]   Embedding saved: {embedding_path}")
            logger.info(
                f"[SPEAKER_VERIFICATION]   Embedding shape: {final_embedding.shape}"
            )
            logger.info(f"[SPEAKER_VERIFICATION]   Original duration: {duration:.2f}s")
            logger.info(
                f"[SPEAKER_VERIFICATION]   Final enrollment duration: {len(enrollment_audio)/SAMPLE_RATE:.2f}s"
            )
            logger.info(
                f"[SPEAKER_VERIFICATION]   Speech segments found: {len(speech_segments) if speech_segments else 0}"
            )
            logger.info(
                f"[SPEAKER_VERIFICATION]   Storage: {'Embeddings + Debug Audio' if self.debug_enabled else 'Embeddings Only'}"
            )

            # Re-load embeddings to include new enrollment
            await self._load_reference_embeddings()

            return True

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Enrollment failed: {e}")
            return False

    def _save_debug_audio_enrollment(
        self,
        waveform: np.ndarray,
        speaker_id: str,
        step: int,
        timestamp: str,
        duration: float,
    ):
        """Save debug audio for enrollment (only when debug mode enabled)."""
        if not self.debug_enabled:
            return

        try:
            import soundfile as sf

            enrollment_filename = f"{speaker_id}_enrollment_step{step}_{timestamp}.wav"
            enrollment_path = os.path.join(self.debug_audio_dir, enrollment_filename)

            # Convert float32 waveform to int16 for saving
            waveform_int16 = (waveform * 32767).astype(np.int16)
            sf.write(enrollment_path, waveform_int16, SAMPLE_RATE)

            logger.info(
                f"[SPEAKER_VERIFICATION] 🐛 DEBUG: Saved enrollment audio to {enrollment_path}"
            )

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Failed to save debug audio: {e}")

    async def _extract_speech_segments(self, waveform: np.ndarray) -> List[np.ndarray]:
        """Extract speech segments from waveform using diarization."""
        if not self.diarization_enabled:
            # Fallback to full waveform if no diarization
            return [waveform]

        try:
            if self.diarization_type == "pyannote":
                return await self._extract_segments_pyannote(waveform)
            elif self.diarization_type == "speechbrain":
                return await self._extract_segments_speechbrain(waveform)
            else:
                logger.warning(
                    "[SPEAKER_VERIFICATION] Unknown diarization type, using full audio"
                )
                return [waveform]

        except Exception as e:
            logger.warning(
                f"[SPEAKER_VERIFICATION] Diarization failed: {e}, using full audio"
            )
            return [waveform]

    async def _filter_audio_by_speaker_verification(
        self, waveform: np.ndarray, target_speaker_id: str, threshold: float = None
    ) -> tuple[np.ndarray, dict]:
        """
        Filter audio to silence unauthorized speakers while keeping enrolled speaker.

        Args:
            waveform: Input audio waveform
            target_speaker_id: Expected speaker identifier
            threshold: Custom similarity threshold

        Returns:
            Tuple of (filtered_waveform, verification_results)
        """
        verification_threshold = threshold or self.config.get(
            "SPEAKER_VERIFICATION_SIMILARITY_THRESHOLD", 0.6
        )

        # Extract speech segments with speaker info
        speech_segments = await self._extract_speech_segments(waveform)
        if not speech_segments or not hasattr(self, "last_speaker_segments"):
            # No speaker segmentation available, fall back to normal verification
            verification_result = await self.verify_speaker(
                waveform, target_speaker_id, threshold
            )
            if verification_result["is_verified"]:
                return waveform, verification_result
            else:
                # Silence entire audio if verification fails
                return np.zeros_like(waveform), verification_result

        # Check if target speaker exists
        if target_speaker_id not in self.reference_embeddings:
            return np.zeros_like(waveform), {
                "is_verified": False,
                "confidence": 0.0,
                "error": f"Speaker {target_speaker_id} not enrolled",
            }

        ref_embedding = self.reference_embeddings[target_speaker_id]
        filtered_waveform = np.copy(waveform)

        verified_segments = 0
        total_segments = len(speech_segments)
        max_similarity = 0.0

        logger.info(
            f"[SPEAKER_VERIFICATION] Processing {total_segments} speaker segments for filtering"
        )

        # Process each speaker segment individually
        for i, segment in enumerate(speech_segments):
            if i >= len(self.last_speaker_segments):
                break

            segment_info = self.last_speaker_segments[i]
            speaker_id = segment_info["speaker_id"]
            start_sample = segment_info["start_sample"]
            end_sample = segment_info["end_sample"]

            # Extract embedding for this specific segment
            try:
                segment_embedding = self.model.encode_batch(
                    torch.tensor(segment).unsqueeze(0)
                )
                segment_embedding = segment_embedding.squeeze().cpu().numpy()

                # Compute similarity with enrolled speaker
                similarity = cosine_similarity(
                    segment_embedding.reshape(1, -1), ref_embedding.reshape(1, -1)
                )[0][0]

                max_similarity = max(max_similarity, similarity)

                if similarity >= verification_threshold:
                    # Keep this speaker's audio (authorized)
                    verified_segments += 1
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] ✅ Segment {i+1} ({speaker_id}): similarity {similarity:.3f} >= {verification_threshold:.3f} - KEEPING"
                    )
                else:
                    # Silence this speaker's audio (unauthorized)
                    filtered_waveform[start_sample:end_sample] = 0.0
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] ❌ Segment {i+1} ({speaker_id}): similarity {similarity:.3f} < {verification_threshold:.3f} - SILENCING"
                    )

            except Exception as e:
                # If embedding extraction fails, silence the segment for safety
                filtered_waveform[start_sample:end_sample] = 0.0
                logger.warning(
                    f"[SPEAKER_VERIFICATION] Failed to process segment {i+1}, silencing for safety: {e}"
                )

        # Overall verification result
        is_verified = verified_segments > 0
        confidence = max_similarity

        verification_result = {
            "is_verified": is_verified,
            "confidence": confidence,
            "verified_segments": verified_segments,
            "total_segments": total_segments,
            "filtered_segments": total_segments - verified_segments,
        }

        logger.info(f"[SPEAKER_VERIFICATION] 🎯 SPEAKER FILTERING RESULT:")
        logger.info(f"[SPEAKER_VERIFICATION]   Target: {target_speaker_id}")
        logger.info(f"[SPEAKER_VERIFICATION]   Total segments: {total_segments}")
        logger.info(f"[SPEAKER_VERIFICATION]   Verified segments: {verified_segments}")
        logger.info(
            f"[SPEAKER_VERIFICATION]   Silenced segments: {total_segments - verified_segments}"
        )
        logger.info(f"[SPEAKER_VERIFICATION]   Max similarity: {confidence:.3f}")
        logger.info(f"[SPEAKER_VERIFICATION]   Threshold: {verification_threshold:.3f}")
        logger.info(
            f"[SPEAKER_VERIFICATION]   ✅ RESULT: {'PASSED' if is_verified else 'REJECTED'}"
        )

        return filtered_waveform, verification_result

    async def _extract_segments_pyannote(
        self, waveform: np.ndarray
    ) -> List[np.ndarray]:
        """Extract speech segments using pyannote.audio."""
        try:
            # Lazy load pyannote pipeline
            if self.diarization_model is None:
                logger.info("[SPEAKER_VERIFICATION] Loading pyannote.audio pipeline...")

                # Try to get HF_TOKEN from environment
                hf_token = os.environ.get("HF_TOKEN")
                if hf_token:
                    logger.info(
                        f"[SPEAKER_VERIFICATION] Found HF_TOKEN in environment (length: {len(hf_token)})"
                    )
                    # Set the token explicitly for huggingface_hub (multiple ways)
                    os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_token
                    os.environ["HF_HOME"] = os.path.expanduser("~/.cache/huggingface")

                    # Try to login programmatically
                    try:
                        from huggingface_hub import login

                        login(token=hf_token, add_to_git_credential=False)
                        logger.info(
                            "[SPEAKER_VERIFICATION] Successfully logged in to HuggingFace Hub"
                        )
                    except Exception as login_error:
                        logger.warning(
                            f"[SPEAKER_VERIFICATION] HF login failed: {login_error}"
                        )
                else:
                    logger.warning(
                        "[SPEAKER_VERIFICATION] No HF_TOKEN found in environment"
                    )

                try:
                    if hf_token:
                        # Pre-download necessary files to ensure they're cached
                        logger.info(
                            "[SPEAKER_VERIFICATION] Pre-downloading model files..."
                        )
                        from huggingface_hub import hf_hub_download

                        try:
                            # Download key model files
                            config_path = hf_hub_download(
                                "pyannote/speaker-diarization-3.1",
                                "config.yaml",
                                token=hf_token,
                            )
                            logger.info(
                                f"[SPEAKER_VERIFICATION] Downloaded config: {config_path}"
                            )
                        except Exception as download_error:
                            logger.warning(
                                f"[SPEAKER_VERIFICATION] Pre-download failed: {download_error}"
                            )

                        # Try with explicit token parameter from environment (older API)
                        logger.info(
                            "[SPEAKER_VERIFICATION] Attempting to load with explicit token..."
                        )
                        self.diarization_model = PyannoteePipeline.from_pretrained(
                            "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
                        )
                    else:
                        # Try with default auth
                        logger.info(
                            "[SPEAKER_VERIFICATION] Attempting to load with default auth..."
                        )
                        self.diarization_model = PyannoteePipeline.from_pretrained(
                            "pyannote/speaker-diarization-3.1", use_auth_token=True
                        )

                    if torch.cuda.is_available():
                        self.diarization_model.to(torch.device("cuda"))
                    logger.info(
                        "[SPEAKER_VERIFICATION] ✅ Pyannote.audio pipeline loaded successfully"
                    )

                except Exception as e:
                    logger.error(
                        f"[SPEAKER_VERIFICATION] Failed to load pyannote with auth: {e}"
                    )
                    logger.info("[SPEAKER_VERIFICATION] This might be due to:")
                    logger.info("[SPEAKER_VERIFICATION] 1. Invalid or expired HF_TOKEN")
                    logger.info(
                        "[SPEAKER_VERIFICATION] 2. Need to accept user conditions on HuggingFace Hub"
                    )
                    logger.info("[SPEAKER_VERIFICATION] 3. Network connectivity issues")
                    logger.info(
                        "[SPEAKER_VERIFICATION] Falling back to energy-based segmentation..."
                    )
                    raise e

            # Save waveform to temporary file for pyannote (it expects file input)
            import tempfile
            import soundfile as sf

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                # Convert float32 to int16 for saving
                waveform_int16 = (waveform * 32767).astype(np.int16)
                sf.write(tmp_file.name, waveform_int16, SAMPLE_RATE)

                # Run diarization
                logger.debug(
                    "[SPEAKER_VERIFICATION] Running pyannote.audio diarization..."
                )
                diarization = self.diarization_model(tmp_file.name)

                # Clean up temp file
                os.unlink(tmp_file.name)

            # Extract speech segments with speaker information
            speech_segments = []
            speaker_info = (
                []
            )  # Track (start_time, end_time, speaker_id) for each segment

            for turn, _, speaker in diarization.itertracks(yield_label=True):
                start_sample = int(turn.start * SAMPLE_RATE)
                end_sample = int(turn.end * SAMPLE_RATE)

                # Extract segment and validate length
                segment = waveform[start_sample:end_sample]
                if len(segment) > int(0.5 * SAMPLE_RATE):  # At least 0.5 seconds
                    speech_segments.append(segment)
                    speaker_info.append(
                        {
                            "start_time": turn.start,
                            "end_time": turn.end,
                            "start_sample": start_sample,
                            "end_sample": end_sample,
                            "speaker_id": speaker,
                        }
                    )
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] Found speech segment: {turn.start:.2f}s-{turn.end:.2f}s speaker_{speaker}"
                    )

            # Store speaker information for filtering
            self.last_speaker_segments = speaker_info

            if speech_segments:
                logger.info(
                    f"[SPEAKER_VERIFICATION] Pyannote extracted {len(speech_segments)} speech segments"
                )
                return speech_segments
            else:
                logger.warning(
                    "[SPEAKER_VERIFICATION] Pyannote found no speech segments, using full audio"
                )
                self.last_speaker_segments = []
                return [waveform]

        except Exception as e:
            logger.warning(f"[SPEAKER_VERIFICATION] Pyannote diarization failed: {e}")
            # Fall back to simple energy-based segmentation
            return await self._extract_segments_simple_vad(waveform)

    async def _extract_segments_speechbrain(
        self, waveform: np.ndarray
    ) -> List[np.ndarray]:
        """Extract speech segments using SpeechBrain diarization."""
        try:
            # Convert to tensor for diarization
            waveform_tensor = torch.tensor(waveform, dtype=torch.float32).unsqueeze(0)

            # Run diarization
            logger.debug("[SPEAKER_VERIFICATION] Running SpeechBrain diarization...")
            diarization_result = self.diarization_model.diarize_from_waveform(
                waveform_tensor, SAMPLE_RATE
            )

            speech_segments = []
            for start_time, end_time, speaker_id in diarization_result:
                start_sample = int(start_time * SAMPLE_RATE)
                end_sample = int(end_time * SAMPLE_RATE)

                # Extract segment and validate length
                segment = waveform[start_sample:end_sample]
                if len(segment) > int(0.5 * SAMPLE_RATE):  # At least 0.5 seconds
                    speech_segments.append(segment)
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] Found speech segment: {start_time:.2f}s-{end_time:.2f}s ({len(segment)} samples)"
                    )

            if speech_segments:
                logger.info(
                    f"[SPEAKER_VERIFICATION] SpeechBrain extracted {len(speech_segments)} speech segments"
                )
                return speech_segments
            else:
                logger.warning(
                    "[SPEAKER_VERIFICATION] SpeechBrain found no speech segments, using full audio"
                )
                return [waveform]

        except Exception as e:
            logger.warning(
                f"[SPEAKER_VERIFICATION] SpeechBrain diarization failed: {e}"
            )
            return await self._extract_segments_simple_vad(waveform)

    async def _extract_segments_simple_vad(
        self, waveform: np.ndarray
    ) -> List[np.ndarray]:
        """Extract speech segments using simple energy-based VAD (fallback method)."""
        try:
            logger.info(
                "[SPEAKER_VERIFICATION] Using simple energy-based segmentation as fallback"
            )

            # Frame parameters
            frame_length = int(0.025 * SAMPLE_RATE)  # 25ms frames
            hop_length = int(0.010 * SAMPLE_RATE)  # 10ms hop

            if len(waveform) < frame_length:
                return [waveform]

            # Compute energy for each frame
            energies = []
            for i in range(0, len(waveform) - frame_length + 1, hop_length):
                frame = waveform[i : i + frame_length]
                energy = np.mean(frame**2)
                energies.append(energy)

            energies = np.array(energies)

            # Adaptive threshold (75th percentile of non-zero energies)
            non_zero_energies = energies[energies > 0]
            if len(non_zero_energies) > 0:
                energy_threshold = np.percentile(non_zero_energies, 75) * 0.3
            else:
                return [waveform]

            # Find voice segments
            voice_frames = energies > energy_threshold

            # Find speech segments with minimum duration
            min_segment_duration = 0.5  # seconds
            min_segment_frames = int(min_segment_duration * SAMPLE_RATE / hop_length)

            segments = []
            start_frame = None

            for i, is_voice in enumerate(voice_frames):
                if is_voice and start_frame is None:
                    start_frame = i
                elif not is_voice and start_frame is not None:
                    # End of segment
                    segment_duration_frames = i - start_frame
                    if segment_duration_frames >= min_segment_frames:
                        start_sample = start_frame * hop_length
                        end_sample = min(i * hop_length + frame_length, len(waveform))
                        segment = waveform[start_sample:end_sample]
                        segments.append(segment)
                        logger.debug(
                            f"[SPEAKER_VERIFICATION] Found energy-based segment: {start_sample/SAMPLE_RATE:.2f}s-{end_sample/SAMPLE_RATE:.2f}s"
                        )
                    start_frame = None

            # Handle segment that continues to end
            if start_frame is not None:
                segment_duration_frames = len(voice_frames) - start_frame
                if segment_duration_frames >= min_segment_frames:
                    start_sample = start_frame * hop_length
                    segment = waveform[start_sample:]
                    segments.append(segment)
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] Found energy-based segment: {start_sample/SAMPLE_RATE:.2f}s-end"
                    )

            if segments:
                logger.info(
                    f"[SPEAKER_VERIFICATION] Energy-based VAD extracted {len(segments)} speech segments"
                )
                return segments
            else:
                logger.warning(
                    "[SPEAKER_VERIFICATION] No energy-based segments found, using full audio"
                )
                return [waveform]

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Simple VAD segmentation failed: {e}")
            return [waveform]

    async def _extract_embedding(
        self, waveform: np.ndarray, use_augmentation: bool = True
    ) -> Optional[np.ndarray]:
        """Extract speaker embedding from audio waveform using encode_batch with optional augmentation."""
        try:
            # Use augmented embedding extraction if enabled, otherwise standard
            if use_augmentation and ENABLE_TEST_TIME_AUGMENTATION:
                logger.info(
                    "[SPEAKER_VERIFICATION] 🔧 Using augmented embedding extraction"
                )
                embedding = extract_augmented_speaker_embedding(
                    waveform, self.model, SAMPLE_RATE
                )
            else:
                logger.info(
                    "[SPEAKER_VERIFICATION] ⚡ Using standard embedding extraction"
                )
                embedding = extract_embedding_standard(waveform, self.model)

            return embedding

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Embedding extraction failed: {e}")
            return None

    async def _extract_embedding_from_segments(
        self, speech_segments: List[np.ndarray]
    ) -> Optional[np.ndarray]:
        """Extract speaker embedding from multiple speech segments and average them."""
        try:
            embeddings = []

            for i, segment in enumerate(speech_segments):
                embedding = await self._extract_embedding(segment)
                if embedding is not None:
                    embeddings.append(embedding)
                    logger.debug(
                        f"[SPEAKER_VERIFICATION] Extracted embedding from segment {i+1}/{len(speech_segments)}"
                    )

            if not embeddings:
                logger.error(
                    "[SPEAKER_VERIFICATION] No valid embeddings extracted from any segment"
                )
                return None

            if len(embeddings) == 1:
                return embeddings[0]
            else:
                # Average multiple embeddings for more robust representation
                averaged_embedding = np.mean(embeddings, axis=0)
                logger.info(
                    f"[SPEAKER_VERIFICATION] Averaged {len(embeddings)} embeddings from speech segments"
                )
                return averaged_embedding

        except Exception as e:
            logger.error(
                f"[SPEAKER_VERIFICATION] Multi-segment embedding extraction failed: {e}"
            )
            return None

    async def verify_speaker(
        self, waveform: np.ndarray, target_speaker_id: str, threshold: float = None
    ) -> Dict[str, Any]:
        """
        Verify if the input waveform matches the target speaker using ensemble methods.

        Args:
            waveform: Input audio waveform
            target_speaker_id: Expected speaker identifier
            threshold: Custom similarity threshold (optional)

        Returns:
            Dict containing verification results
        """
        if not self.initialized:
            return {
                "is_verified": False,
                "confidence": 0.0,
                "error": "Service not initialized",
            }

        # Use provided threshold, or processor's configured threshold, or hardcoded default
        verification_threshold = (
            threshold if threshold is not None else EMBEDDING_SIMILARITY_THRESHOLD
        )

        try:
            # Use waveform directly since DailyTransport VAD already provides clean speech boundaries
            logger.debug(
                f"[SPEAKER_VERIFICATION] Processing audio duration: {len(waveform)/SAMPLE_RATE:.2f}s"
            )

            # Ensure minimum duration for verification
            duration = len(waveform) / SAMPLE_RATE
            if duration < 0.5:  # At least 0.5 seconds of speech
                return {
                    "is_verified": False,
                    "confidence": 0.0,
                    "error": f"Audio too short: {duration:.2f}s < 0.5s required",
                }

            # Check if target speaker exists
            if target_speaker_id not in self.reference_embeddings:
                return {
                    "is_verified": False,
                    "confidence": 0.0,
                    "error": f"Speaker {target_speaker_id} not enrolled",
                }

            # Log augmentation status
            logger.info(
                f"[SPEAKER_VERIFICATION] Augmentation status - TTA: {ENABLE_TEST_TIME_AUGMENTATION}, Ensemble: {ENABLE_ENSEMBLE_VERIFICATION}, Preprocessing: {USE_PREPROCESSING_VARIANTS}"
            )

            # Use ensemble verification if enabled, otherwise standard method
            if ENABLE_ENSEMBLE_VERIFICATION:
                logger.info(
                    f"[SPEAKER_VERIFICATION] Using ensemble verification for speaker '{target_speaker_id}'"
                )
                ensemble_result = ensemble_speaker_verification(
                    waveform, self.reference_embeddings, self.model, target_speaker_id
                )

                # Convert ensemble result to expected format
                is_verified = ensemble_result.get("verified", False)
                confidence = ensemble_result.get("confidence", 0.0)
                method = ensemble_result.get("method", "ensemble")

                logger.info(f"[SPEAKER_VERIFICATION] 🔍 ENSEMBLE VERIFICATION RESULT:")
                logger.info(f"[SPEAKER_VERIFICATION]   Speaker: {target_speaker_id}")
                logger.info(f"[SPEAKER_VERIFICATION]   Audio duration: {duration:.2f}s")
                logger.info(f"[SPEAKER_VERIFICATION]   Method: {method}")
                logger.info(f"[SPEAKER_VERIFICATION]   Confidence: {confidence:.3f}")
                logger.info(
                    f"[SPEAKER_VERIFICATION]   Threshold: {verification_threshold:.3f}"
                )
                logger.info(
                    f"[SPEAKER_VERIFICATION]   ✅ VERIFIED: {is_verified}"
                    if is_verified
                    else f"[SPEAKER_VERIFICATION]   ❌ REJECTED: {is_verified}"
                )

                return {
                    "is_verified": is_verified,
                    "confidence": float(confidence),
                    "identified_speaker": target_speaker_id if is_verified else None,
                    "threshold": verification_threshold,
                    "method": method,
                    "ensemble_details": ensemble_result.get("details", {}),
                }
            else:
                # Standard verification with augmented embedding extraction
                logger.info(
                    f"[SPEAKER_VERIFICATION] Using standard verification with augmentation for speaker '{target_speaker_id}'"
                )

                # Extract speech segments using diarization (for multi-speaker separation)
                speech_segments = await self._extract_speech_segments(waveform)
                if not speech_segments:
                    return {
                        "is_verified": False,
                        "confidence": 0.0,
                        "error": "No speech segments found in audio",
                    }

                # Extract embedding from speech segments with augmentation
                if ENABLE_TEST_TIME_AUGMENTATION:
                    logger.info(
                        "[SPEAKER_VERIFICATION] ✅ Test-time augmentation ENABLED"
                    )
                    # Use the longest segment for augmented extraction
                    longest_segment = max(speech_segments, key=len)
                    input_embedding = extract_augmented_speaker_embedding(
                        longest_segment, self.model, SAMPLE_RATE
                    )
                else:
                    logger.info(
                        "[SPEAKER_VERIFICATION] ⚠️ Test-time augmentation DISABLED"
                    )
                    input_embedding = await self._extract_embedding_from_segments(
                        speech_segments
                    )

                if input_embedding is None:
                    return {
                        "is_verified": False,
                        "confidence": 0.0,
                        "error": "Failed to extract input embedding from speech segments",
                    }

                # Compute similarity
                ref_embedding = self.reference_embeddings[target_speaker_id]
                similarity = cosine_similarity(
                    input_embedding.reshape(1, -1), ref_embedding.reshape(1, -1)
                )[0][0]

                is_verified = similarity >= verification_threshold
                method = "augmented" if ENABLE_TEST_TIME_AUGMENTATION else "standard"

                logger.info(f"[SPEAKER_VERIFICATION] 🔍 VERIFICATION RESULT:")
                logger.info(f"[SPEAKER_VERIFICATION]   Speaker: {target_speaker_id}")
                logger.info(f"[SPEAKER_VERIFICATION]   Audio duration: {duration:.2f}s")
                logger.info(
                    f"[SPEAKER_VERIFICATION]   Speech segments: {len(speech_segments)}"
                )
                logger.info(f"[SPEAKER_VERIFICATION]   Method: {method}")
                logger.info(f"[SPEAKER_VERIFICATION]   Similarity: {similarity:.3f}")
                logger.info(
                    f"[SPEAKER_VERIFICATION]   Threshold: {verification_threshold:.3f}"
                )
                logger.info(
                    f"[SPEAKER_VERIFICATION]   ✅ VERIFIED: {is_verified}"
                    if is_verified
                    else f"[SPEAKER_VERIFICATION]   ❌ REJECTED: {is_verified}"
                )

                return {
                    "is_verified": is_verified,
                    "confidence": float(similarity),
                    "identified_speaker": target_speaker_id,
                    "threshold": verification_threshold,
                    "speech_segments_count": len(speech_segments),
                    "method": method,
                }

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Verification failed: {e}")
            return {"is_verified": False, "confidence": 0.0, "error": str(e)}

    async def enroll_speaker(
        self, waveform: np.ndarray, speaker_id: str, step: int = 1
    ) -> bool:
        """
        Enroll a new speaker or add additional samples.
        Uses speech segment extraction to save only speech portions.

        Args:
            waveform: Audio waveform for enrollment
            speaker_id: Speaker identifier
            step: Enrollment step number

        Returns:
            Success status
        """
        try:
            import datetime

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # Use waveform directly since DailyTransport VAD already provides clean speech boundaries
            logger.debug(
                f"[SPEAKER_VERIFICATION] Enrollment audio duration: {len(waveform)/SAMPLE_RATE:.2f}s"
            )

            # Ensure minimum duration for enrollment
            duration = len(waveform) / SAMPLE_RATE
            if duration < 1.0:  # At least 1 second of speech for enrollment
                logger.error(
                    f"[SPEAKER_VERIFICATION] Enrollment audio too short: {duration:.2f}s"
                )
                return False

            # Extract speech segments using diarization for clean enrollment
            speech_segments = await self._extract_speech_segments(waveform)

            if speech_segments:
                # Use the longest speech segment for enrollment (should be the primary speaker)
                longest_segment = max(speech_segments, key=len)
                logger.info(
                    f"[SPEAKER_VERIFICATION] Using longest speech segment for enrollment: {len(longest_segment)} samples from {len(speech_segments)} segments"
                )
                enrollment_audio = longest_segment
            else:
                logger.warning(
                    f"[SPEAKER_VERIFICATION] No speech segments found in enrollment step {step}, using full audio"
                )
                enrollment_audio = waveform

            # Save enrollment audio
            enrollment_filename = f"{speaker_id}_enrollment_step{step}_{timestamp}.wav"
            enrollment_path = os.path.join(self.enrollment_dir, enrollment_filename)

            # Convert float32 waveform to int16 for saving
            waveform_int16 = (enrollment_audio * 32767).astype(np.int16)
            import soundfile as sf

            sf.write(enrollment_path, waveform_int16, SAMPLE_RATE)

            logger.info(f"[SPEAKER_VERIFICATION] 📁 ENROLLMENT STEP {step} SAVED:")
            logger.info(f"[SPEAKER_VERIFICATION]   File: {enrollment_path}")
            logger.info(f"[SPEAKER_VERIFICATION]   Original duration: {duration:.2f}s")
            logger.info(
                f"[SPEAKER_VERIFICATION]   Final enrollment duration: {len(enrollment_audio)/SAMPLE_RATE:.2f}s"
            )
            logger.info(
                f"[SPEAKER_VERIFICATION]   Speech segments found: {len(speech_segments) if speech_segments else 0}"
            )

            # Show segment selection efficiency
            if speech_segments and len(enrollment_audio) != len(waveform):
                segment_efficiency = (len(enrollment_audio) / len(waveform)) * 100
                logger.info(
                    f"[SPEAKER_VERIFICATION]   🎯 Using {segment_efficiency:.1f}% of audio (longest speech segment)"
                )
            else:
                logger.info(
                    f"[SPEAKER_VERIFICATION]   📢 Using full audio (no diarization applied)"
                )

            # Re-load embeddings to include new enrollment
            await self._load_reference_embeddings()

            return True

        except Exception as e:
            logger.error(f"[SPEAKER_VERIFICATION] Enrollment failed: {e}")
            return False

    def get_enrolled_speakers(self) -> List[str]:
        """Get list of enrolled speaker IDs."""
        return list(self.reference_embeddings.keys())


class SpeakerVerificationProcessor(FrameProcessor):
    """
    Real-time speaker verification processor that integrates with Pipecat pipeline.

    Features:
    - Auto-enrollment after 2 user queries
    - Real-time speaker verification
    - Audio filtering for unverified speakers
    - RTVI events for frontend notifications
    """

    def __init__(
        self,
        target_speaker_id: str,
        embeddings_dir: str = "embeddings",
        enable_enrollment: bool = True,
        enrollment_queries: int = 2,
        similarity_threshold: float = 0.60,
    ):
        super().__init__()

        self.target_speaker_id = target_speaker_id
        self.enable_enrollment = enable_enrollment
        self.enrollment_queries = (
            enrollment_queries  # Number of queries for auto-enrollment
        )
        self.query_count = 0  # Track number of user queries
        self.user_locked = False  # Whether user is locked after enrollment
        self.similarity_threshold = similarity_threshold  # Configurable threshold

        # Initialize verification service with embeddings directory
        self.verification_service = SpeakerVerificationService(embeddings_dir)
        self.service_ready = False

        # Audio collection state
        self.is_collecting = False
        self.current_audio_frames: List[AudioRawFrame] = []

        # RTVI event callback for sending events to frontend
        self.rtvi_callback = None

        # Verification tracking
        self._consecutive_rejections = 0
        self._max_consecutive_rejections = 10
        self._rejection_cooldown_until = 0
        self._rejection_cooldown_seconds = 2.0

        # Statistics
        self.stats = {
            "total_segments": 0,
            "verified_segments": 0,
            "rejected_segments": 0,
            "enrollment_queries_completed": 0,
            "queries_processed": 0,
        }

        logger.info(
            f"[SPEAKER_VERIFICATION_PROCESSOR] Initialized for speaker: {target_speaker_id}"
        )
        if enable_enrollment:
            logger.info(
                f"[SPEAKER_VERIFICATION_PROCESSOR] 🎤 Auto-enrollment: Lock after {enrollment_queries} queries"
            )

    async def initialize(self) -> bool:
        """Initialize the verification service."""
        self.service_ready = await self.verification_service.initialize()
        return self.service_ready

    def set_rtvi_callback(self, rtvi_callback):
        """Set callback function for sending RTVI events to frontend."""
        self.rtvi_callback = rtvi_callback

    async def _send_rtvi_event(self, event_type: str, data: dict):
        """Send RTVI event to frontend."""
        if self.rtvi_callback:
            try:
                await self.rtvi_callback({"type": event_type, "data": data})
                logger.info(f"[SPEAKER_VERIFICATION] Sent RTVI event: {event_type}")
            except Exception as e:
                logger.error(f"[SPEAKER_VERIFICATION] Failed to send RTVI event: {e}")
        else:
            logger.debug(
                f"[SPEAKER_VERIFICATION] No RTVI callback set for event: {event_type}"
            )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames and handle speaker verification."""
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            # Handle audio frames for verification
            if self.is_collecting:
                self.current_audio_frames.append(frame)

        elif isinstance(frame, UserStartedSpeakingFrame):
            # VAD detected speech start
            if not self.is_collecting:
                await self._start_collecting()
            await self.push_frame(frame, direction)

        elif isinstance(frame, UserStoppedSpeakingFrame):
            # VAD detected speech end - trigger verification
            if self.is_collecting:
                await self._stop_collecting_and_verify()
            await self.push_frame(frame, direction)

        else:
            # Pass through other frame types
            await self.push_frame(frame, direction)

    async def _start_collecting(self):
        """Start collecting audio frames."""
        if not self.is_collecting:
            self.is_collecting = True
            self.current_audio_frames = []
            logger.debug("[SPEAKER_VERIFICATION_PROCESSOR] 🎙️ Started collecting audio")

    async def _stop_collecting_and_verify(self):
        """Stop collecting and process the audio for verification."""
        if not self.is_collecting:
            return

        self.is_collecting = False
        frames_to_process = self.current_audio_frames
        self.current_audio_frames = []

        if not frames_to_process:
            return

        logger.info(
            f"[SPEAKER_VERIFICATION_PROCESSOR] 🔍 Processing {len(frames_to_process)} audio frames"
        )

        # Convert frames to waveform
        waveforms = []
        for frame in frames_to_process:
            waveform_np = (
                np.frombuffer(frame.audio, dtype=np.int16).astype(np.float32) / 32768.0
            )
            waveforms.append(waveform_np)

        if not waveforms:
            return

        full_waveform = np.concatenate(waveforms)
        duration = len(full_waveform) / SAMPLE_RATE

        # Save audio for debugging (centralized debug system)
        if self.verification_service.debug_enabled:
            await self._save_debug_audio(full_waveform, duration)

        # Skip very short segments
        if duration < 0.5:
            logger.debug(
                f"[SPEAKER_VERIFICATION_PROCESSOR] ⏭️ Skipping short segment: {duration:.2f}s"
            )
            await self._forward_frames(frames_to_process)
            return

        self.stats["total_segments"] += 1
        self.query_count += 1
        self.stats["queries_processed"] = self.query_count

        # Handle enrollment phase (first N queries)
        if self.enable_enrollment and self.query_count <= self.enrollment_queries:
            await self._handle_enrollment_query(
                full_waveform, duration, self.query_count
            )
            await self._forward_frames(frames_to_process)  # Forward during enrollment

            # Check if enrollment is complete
            if self.query_count == self.enrollment_queries:
                await self._complete_enrollment()
            return

        # Handle verification phase (after enrollment)
        if self.user_locked:
            await self._handle_verification(full_waveform, frames_to_process)
        else:
            # If not locked and past enrollment, just forward (shouldn't happen)
            await self._forward_frames(frames_to_process)

    async def _handle_enrollment_query(
        self, waveform: np.ndarray, duration: float, query_num: int
    ):
        """Handle enrollment query."""
        logger.info(
            f"[SPEAKER_VERIFICATION_PROCESSOR] 📝 ENROLLMENT QUERY {query_num}/{self.enrollment_queries}"
        )

        if not self.service_ready:
            logger.warning(
                "[SPEAKER_VERIFICATION_PROCESSOR] ⚠️ Service not ready for enrollment"
            )
            return

        # Send RTVI event for enrollment progress
        await self._send_rtvi_event(
            "speaker-enrollment-progress",
            {
                "query_number": query_num,
                "total_queries": self.enrollment_queries,
                "speaker_id": self.target_speaker_id,
                "duration": duration,
            },
        )

        success = await self.verification_service.enroll_speaker(
            waveform, self.target_speaker_id, query_num
        )

        if success:
            self.stats["enrollment_queries_completed"] += 1
            remaining = self.enrollment_queries - query_num
            if remaining > 0:
                logger.info(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] ✅ Query {query_num} enrolled. {remaining} more needed."
                )
            else:
                logger.info(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] 🎉 All enrollment queries complete!"
                )
        else:
            logger.error(
                f"[SPEAKER_VERIFICATION_PROCESSOR] ❌ Enrollment query {query_num} failed"
            )

    async def _complete_enrollment(self):
        """Complete the enrollment process and lock the user."""
        self.user_locked = True

        logger.info(
            f"[SPEAKER_VERIFICATION_PROCESSOR] 🔒 USER LOCKED - Speaker verification now active for: {self.target_speaker_id}"
        )

        # Send RTVI event to notify frontend that user is locked
        await self._send_rtvi_event(
            "speaker-verification-locked",
            {
                "speaker_id": self.target_speaker_id,
                "enrollment_queries_completed": self.stats[
                    "enrollment_queries_completed"
                ],
                "locked": True,
                "message": f"Speaker verification is now active. Only {self.target_speaker_id} can continue the conversation.",
            },
        )

    async def _handle_verification(
        self, waveform: np.ndarray, frames: List[AudioRawFrame]
    ):
        """Handle speaker verification."""
        # Check cooldown
        if time.time() < self._rejection_cooldown_until:
            logger.debug(
                "[SPEAKER_VERIFICATION_PROCESSOR] 🔇 In rejection cooldown - blocking audio"
            )
            await self._send_silenced_audio(frames)
            return

        if not self.service_ready:
            logger.warning(
                "[SPEAKER_VERIFICATION_PROCESSOR] ⚠️ Service not ready - blocking audio"
            )
            await self._send_silenced_audio(frames)
            return

        # Filter audio using speaker-specific verification
        filtered_waveform, result = (
            await self.verification_service._filter_audio_by_speaker_verification(
                waveform, self.target_speaker_id, self.similarity_threshold
            )
        )

        if result.get("is_verified", False):
            # At least some authorized speech found
            self.stats["verified_segments"] += 1
            self._consecutive_rejections = 0

            # Log detailed filtering results
            verified_segments = result.get("verified_segments", 0)
            total_segments = result.get("total_segments", 1)
            filtered_segments = result.get("filtered_segments", 0)

            logger.info(
                f"[SPEAKER_VERIFICATION_PROCESSOR] ✅ VERIFIED - Confidence: {result.get('confidence', 0):.3f}"
            )
            if filtered_segments > 0:
                logger.info(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] 🔇 FILTERED: {filtered_segments} unauthorized segments silenced, {verified_segments} segments kept"
                )

            # Send success RTVI event with filtering details
            await self._send_rtvi_event(
                "speaker-verification-success",
                {
                    "speaker_id": self.target_speaker_id,
                    "confidence": result.get("confidence", 0),
                    "verified": True,
                    "verified_segments": verified_segments,
                    "total_segments": total_segments,
                    "filtered_segments": filtered_segments,
                },
            )

            # Convert filtered waveform back to frames and forward
            await self._forward_filtered_frames(frames, filtered_waveform, waveform)
        else:
            # No authorized speech found - block entire audio
            self.stats["rejected_segments"] += 1
            self._consecutive_rejections += 1

            # Send failure RTVI event
            await self._send_rtvi_event(
                "speaker-verification-failed",
                {
                    "speaker_id": self.target_speaker_id,
                    "confidence": result.get("confidence", 0),
                    "error": result.get("error", "Unknown error"),
                    "consecutive_rejections": self._consecutive_rejections,
                    "verified": False,
                },
            )

            # Activate cooldown if too many rejections
            if self._consecutive_rejections >= self._max_consecutive_rejections:
                self._rejection_cooldown_until = (
                    time.time() + self._rejection_cooldown_seconds
                )
                logger.warning(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] 🚫 Cooldown activated: {self._rejection_cooldown_seconds}s"
                )

                # Send cooldown RTVI event
                await self._send_rtvi_event(
                    "speaker-verification-cooldown",
                    {
                        "speaker_id": self.target_speaker_id,
                        "cooldown_seconds": self._rejection_cooldown_seconds,
                        "message": "Too many verification failures. Audio will be blocked temporarily.",
                    },
                )

            logger.info(
                f"[SPEAKER_VERIFICATION_PROCESSOR] ❌ REJECTED - {result.get('error', 'Unknown error')}"
            )
            await self._send_silenced_audio(frames)
            await self._send_interruption()

    async def _forward_filtered_frames(
        self,
        original_frames: List[AudioRawFrame],
        filtered_waveform: np.ndarray,
        original_waveform: np.ndarray,
    ):
        """Forward audio frames with speaker filtering applied."""
        if len(original_frames) == 0:
            return

        # Convert filtered waveform back to int16
        filtered_int16 = (filtered_waveform * 32767).astype(np.int16)

        # Reconstruct frames using exact original frame boundaries
        start_sample = 0
        for i, original_frame in enumerate(original_frames):
            # Calculate exact frame size from original frame
            original_frame_size = (
                len(original_frame.audio) // 2
            )  # Convert bytes to samples
            end_sample = start_sample + original_frame_size

            if end_sample <= len(filtered_int16):
                # Extract the corresponding filtered audio chunk (exact size)
                filtered_chunk = filtered_int16[start_sample:end_sample]
                filtered_bytes = filtered_chunk.tobytes()
            else:
                # If we run out of filtered audio, use silence but preserve original frame size
                logger.warning(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] Frame {i+1} extends beyond filtered audio, using original frame audio"
                )
                filtered_bytes = original_frame.audio  # Keep original as fallback

            # Verify the frame size matches
            if len(filtered_bytes) != len(original_frame.audio):
                logger.error(
                    f"[SPEAKER_VERIFICATION_PROCESSOR] Frame size mismatch: filtered={len(filtered_bytes)}, original={len(original_frame.audio)}"
                )
                # Use original frame as fallback to prevent corruption
                filtered_bytes = original_frame.audio

            # Modify the original frame in place instead of creating new frame
            # This preserves all internal state and frame pipeline compatibility
            original_frame.audio = filtered_bytes

            await self.push_frame(original_frame)
            start_sample = end_sample

        logger.debug(
            f"[SPEAKER_VERIFICATION_PROCESSOR] ➡️ Forwarded {len(original_frames)} filtered frames to STT"
        )

    async def _forward_frames(self, frames: List[AudioRawFrame]):
        """Forward original frames to downstream processors."""
        logger.debug(
            f"[SPEAKER_VERIFICATION_PROCESSOR] ➡️ Forwarding {len(frames)} frames to STT"
        )
        for frame in frames:
            await self.push_frame(frame)

    async def _send_silenced_audio(self, frames: List[AudioRawFrame]):
        """Send silenced audio frames."""
        logger.debug(
            f"[SPEAKER_VERIFICATION_PROCESSOR] 🔇 Silencing {len(frames)} frames"
        )
        # Don't forward frames - this blocks audio from reaching STT

    async def _send_interruption(self):
        """Send interruption frame."""
        try:
            await self.push_frame(StartInterruptionFrame())
            logger.debug("[SPEAKER_VERIFICATION_PROCESSOR] 🛑 Sent interruption frame")
        except Exception as e:
            logger.error(
                f"[SPEAKER_VERIFICATION_PROCESSOR] Failed to send interruption: {e}"
            )

    async def _save_debug_audio(self, waveform: np.ndarray, duration: float):
        """Save audio segment for debugging purposes (centralized debug system)."""
        if not self.verification_service.debug_enabled:
            return

        try:
            import datetime
            import soundfile as sf

            # Use centralized debug directory
            debug_dir = self.verification_service.debug_audio_dir

            # Generate filename with timestamp and phase info
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[
                :-3
            ]  # Include milliseconds
            if self.user_locked:
                phase = "verification"
                status = "locked"
            else:
                phase = "enrollment"
                status = f"step{self.query_count}"

            filename = f"{self.target_speaker_id}_{phase}_{status}_{timestamp}_{duration:.2f}s.wav"
            filepath = os.path.join(debug_dir, filename)

            # Convert float32 waveform to int16 for saving
            waveform_int16 = (waveform * 32767).astype(np.int16)
            sf.write(filepath, waveform_int16, SAMPLE_RATE)

            logger.info(
                f"[SPEAKER_VERIFICATION_PROCESSOR] 🐛 DEBUG: Saved audio to {filepath}"
            )

        except Exception as e:
            logger.error(
                f"[SPEAKER_VERIFICATION_PROCESSOR] Failed to save debug audio: {e}"
            )

    def get_status(self) -> Dict[str, Any]:
        """Get current processor status."""
        if self.query_count == 0:
            enrollment_status = "not_started"
        elif self.query_count < self.enrollment_queries:
            enrollment_status = "in_progress"
        else:
            enrollment_status = "completed"

        return {
            "target_speaker": self.target_speaker_id,
            "service_ready": self.service_ready,
            "user_locked": self.user_locked,
            "enrollment": {
                "enabled": self.enable_enrollment,
                "current_query": self.query_count,
                "total_queries": self.enrollment_queries,
                "status": enrollment_status,
                "remaining_queries": max(0, self.enrollment_queries - self.query_count),
            },
            "verification": {
                "active": self.user_locked,
                "consecutive_rejections": self._consecutive_rejections,
                "in_cooldown": time.time() < self._rejection_cooldown_until,
                "enrolled_speakers": self.verification_service.get_enrolled_speakers(),
            },
            "stats": self.stats.copy(),
        }
