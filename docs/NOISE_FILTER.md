# Buddy noise filter

Real-time noise suppression for the caller's audio, applied before VAD and STT.
Code: `app/ai/voice/noise_filter/`; wiring: `get_transport_params` in
`app/ai/voice/agents/breeze_buddy/agent/transport.py`.

## Configuration

```json
"noise_filter": { "enable": true, "provider": "buddy", "model": "noise_cancellation" }
```

- `provider` and `model` must be given together. With `buddy`, all three `model`
  values (`noise_cancellation`, `noise_cancellation_s`, `voice_focus`) run the
  same model; there is no primary-speaker model.
- `enhancement_level` (optional, 0.0–1.0): share of the denoised audio kept, the
  rest being the original. Default 0.5 (see "Choice of model and setting").
- Telephony (8 kHz) uses `dpdfnet2_8k`; Daily web calls (16 kHz) use
  `dpdfnet_base_16k`.

`provider: "aic"` and the legacy `type` field select the previous ai-coustics
filter, which is used only when the dynamic flag `BREEZE_BUDDY_ENABLE_AIC_FILTER`
is true (read per call):

| `BREEZE_BUDDY_ENABLE_AIC_FILTER` | `provider: "buddy"` | `provider: "aic"` / legacy `type` |
| --- | --- | --- |
| `false` (default) | Buddy | Buddy |
| `true` | Buddy | ai-coustics; on any ai-coustics failure, that call falls back to Buddy |

## How it works

`BuddyNoiseFilter` is a pipecat `BaseAudioFilter`, set as the transport's
`audio_in_filter`. For each 20 ms of caller audio: window → FFT → one ONNX step
with the call's recurrent state → blend with the noisy frame from 4 hops back
(the model looks 40 ms ahead) → inverse FFT → overlap-add.

- The model is loaded once per process and shared by all calls; each call keeps
  its own state. Inference runs off the event loop on a small thread pool (one
  thread busy per call; prod runs one call per voice pod).
- Added delay: ~50 ms. Compute: ~1.6 ms per 20 ms of audio (p99), about 8.5% of
  a CPU core per call.
- `filter()` never raises (pipecat's input task only catches timeouts): an error,
  non-finite output or falling 100 ms behind real time passes the rest of the call
  through unfiltered. A missing model file means no filter, never a failed call.

## Models and provisioning

Ceva DPDFNet (huggingface.co/Ceva-IP/DPDFNet), Apache-2.0 for code and weights,
used unmodified: `onnx/dpdfnet2_8khz.onnx` and `onnx/baseline.onnx` (saved as
`dpdfnet_baseline_16khz.onnx`), revision `dd6818d00f50c836fed43a6243ebe49116de5964`.
Both are mirrored to `gs://breeze-clairvoyance-models/dpdfnet/`, next to the
ai-coustics models. GCP builds copy them from there into
`BREEZE_BUDDY_NOISE_MODEL_DIR` (`/app/models/voice/buddy`) and check their
sha256 (see the Dockerfile); as for the ai-coustics models, a missing or
mismatched file is a build warning, and a missing model means calls run without
a noise filter. Builds
without the GCP token (AWS) skip them, as they skip the ai-coustics models, and
run without a noise filter. Nothing is downloaded at runtime. For local runs,
point `BREEZE_BUDDY_NOISE_MODEL_DIR` at a folder holding the two files.

## Choice of model and setting (Oct 2026)

Evaluated on telephony audio (300–3400 Hz, 8 kHz, G.711) with real recorded
noise, each filter fed 20 ms chunks as in production, and the STTs configured
exactly as the highest-volume templates. Error rates: lower is better.

| | Sarvam CER (Hindi) | Soniox CER | Whisper WER | CPU per call |
| --- | --- | --- | --- | --- |
| No filter | 17.0% | 16.5% | 36.6% | – |
| ai-coustics quail-S (previous) | 16.5% | 16.5% | 35.5% | 3.7% |
| Buddy, 0.75 | 13.6% | 15.7% | 27.5% | 8.5% |
| Buddy, full strength | – | 19.4% | 30.7% | 8.5% |

- On real call recordings (caller side), Buddy lowered background noise by
  ~12.6 dB with speech level unchanged, and Soniox accuracy and turn counts stayed
  level with no filter.
- Full strength removes more noise but drops parts of words; 0.5 recognised best.
- Not adopted on telephony audio: DPDFNet-base at 8 kHz, GTCRN, UL-UNAS, RNNoise,
  FastEnhancer (worse than no filter); WebRTC NS, SpeexDSP (no better); the larger
  `dpdfnet8_8khz` (same accuracy, ~2.5× the CPU). int8 and TFLite builds of the
  chosen model were not faster.
- Not yet evaluated: the 16 kHz model on real web (Daily) audio.

## Licensing rules

- New models need permissive licences for both code and weights; record the
  source revision here, mirror the files to the bucket, and pin their sha256 in
  the Dockerfile.
- Training data: Ceva lists DNS4, Multilingual LibriSpeech, MUSAN and FSD50K for
  its 16 kHz models (FSD50K includes some non-commercial clips); the 8 kHz model's
  training data is not documented.
- Apache-2.0 requires shipping its licence text only if the models are
  redistributed (e.g. an image handed to a customer); internal use does not.
- Never train, fine-tune or distil a model on ai-coustics output (including
  recordings or transcripts made from ai-coustics-filtered audio), and never run
  `.aicmodel` files outside the ai-coustics SDK.
- Evaluation results that include ai-coustics stay internal.

## Removing the ai-coustics path

When the ai-coustics licence ends: delete its model files
(`gs://breeze-clairvoyance-models/aic`, Docker images and caches that contain
them), remove the Dockerfile download step, `_FailOpenAICFilter` and
`_create_aic_filter` in `transport.py`, the `BREEZE_BUDDY_ENABLE_AIC_FILTER` flag
and the `aic` pipecat extra, and map `provider: "aic"` templates to Buddy.
