import io
import wave

from app.ai.voice.tts.preview import (
    _sarvam_payload,
    content_key,
    pcm_to_wav,
    sentence_for,
    wav_to_pcm,
)


def _cfg(**overrides) -> dict:
    cfg = {
        "provider": "cartesia",
        "voice_id": "v1",
        "model": "sonic-3.5",
        "language": "en",
        "style_params": {},
        "format": "wav",
    }
    cfg.update(overrides)
    return cfg


def test_content_key_stable_and_sensitive():
    a = content_key(_cfg())
    assert a == content_key(_cfg())
    assert len(a) == 16
    assert a != content_key(_cfg(language="hi"))
    assert a != content_key(_cfg(style_params={"emotion": "happy"}))


def test_content_key_sensitive_to_resolved_model():
    # The key hashes the RESOLVED configuration — a provider-default model
    # change must invalidate previews generated under the old default.
    assert content_key(_cfg(model="sonic-3.5")) != content_key(_cfg(model="sonic-4"))


def test_pcm_to_wav_header():
    wav = pcm_to_wav(b"\x00\x01" * 1600, 16000)
    with wave.open(io.BytesIO(wav)) as w:
        assert (
            w.getnchannels() == 1
            and w.getframerate() == 16000
            and w.getsampwidth() == 2
        )


def test_wav_to_pcm_round_trip_strips_container():
    # Providers that return a full WAV (Sarvam) must be unwrapped before
    # generate_preview re-wraps — exactly one header in the output.
    pcm = b"\x00\x01" * 1600
    frames, rate = wav_to_pcm(pcm_to_wav(pcm, 22050))
    assert frames == pcm
    assert rate == 22050
    assert not frames.startswith(b"RIFF")


def test_wav_to_pcm_passes_raw_pcm_through():
    raw = b"\x00\x01" * 100
    frames, rate = wav_to_pcm(raw)
    assert frames == raw
    assert rate == 16000


def test_sentence_localized_with_fallback():
    assert sentence_for("hi") != sentence_for("en")
    assert sentence_for("en-IN") == sentence_for("en")  # bare-code fallback
    assert sentence_for("xx") == sentence_for("en")  # unknown -> English


def test_sarvam_payload_omits_pitch_loudness_for_bulbul_v3():
    # Bulbul V3 rejects these fields outright (confirmed against the live API):
    # "Pitch and loudness parameters are currently not supported for the
    # Bulbul V3 model. Please do not pass these values."
    payload = _sarvam_payload(
        voice_id="shreya",
        model="bulbul:v3",
        language_code="en-IN",
        text="hi",
        sarvam_defaults={"model": "bulbul:v3", "speed": 0.9, "pitch": 0.0},
        enable_preprocessing=True,
    )
    assert "pitch" not in payload
    assert "loudness" not in payload
    assert payload["pace"] == 0.9
    assert payload["model"] == "bulbul:v3"


def test_sarvam_payload_keeps_pitch_loudness_for_non_v3():
    payload = _sarvam_payload(
        voice_id="shreya",
        model="bulbul:v2",
        language_code="en-IN",
        text="hi",
        sarvam_defaults={"model": "bulbul:v2", "speed": 0.9, "pitch": 0.0},
        enable_preprocessing=True,
    )
    assert payload["pitch"] == 0.0
    assert payload["loudness"] == 1.5


def test_sarvam_payload_region_qualifies_bare_language_code():
    # Sarvam's target_language_code only accepts region-qualified codes (its
    # validation error enumerates an all-"xx-IN" set: as-IN, bn-IN, ..., hi-IN,
    # ..., ur-IN), but the catalog stores bare codes like "hi".
    payload = _sarvam_payload(
        voice_id="amit",
        model="bulbul:v3",
        language_code="hi",
        text="hi",
        sarvam_defaults={"model": "bulbul:v3", "speed": 0.9, "pitch": 0.0},
        enable_preprocessing=True,
    )
    assert payload["target_language_code"] == "hi-IN"


def test_sarvam_payload_leaves_already_qualified_language_code_unchanged():
    payload = _sarvam_payload(
        voice_id="amit",
        model="bulbul:v3",
        language_code="hi-IN",
        text="hi",
        sarvam_defaults={"model": "bulbul:v3", "speed": 0.9, "pitch": 0.0},
        enable_preprocessing=True,
    )
    assert payload["target_language_code"] == "hi-IN"


def test_sarvam_payload_region_qualifies_bare_english_code():
    payload = _sarvam_payload(
        voice_id="amit",
        model="bulbul:v3",
        language_code="en",
        text="hi",
        sarvam_defaults={"model": "bulbul:v3", "speed": 0.9, "pitch": 0.0},
        enable_preprocessing=True,
    )
    assert payload["target_language_code"] == "en-IN"
