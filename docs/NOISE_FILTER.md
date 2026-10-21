# Noizzen noise filter

Real-time noise suppression for the caller's audio, applied before VAD and STT.
Code: `app/ai/voice/noise_filter/noizzen/` (the Noizzen SDK and engine); wiring:
`get_transport_params` in `app/ai/voice/agents/breeze_buddy/agent/transport.py`.

## Configuration

```json
"noise_filter": { "enable": true, "provider": "noizzen", "model": "noise_cancellation" }
```

- `provider` and `model` must be given together. With `noizzen`, all three `model`
  values (`noise_cancellation`, `noise_cancellation_s`, `voice_focus`) run the
  same model; there is no primary-speaker model.
- `enhancement_level` (optional, 0.0–1.0): share of the denoised audio kept, the
  rest being the original. Default 0.5 (see "Choice of model and setting").
- Telephony (8 kHz) uses `dpdfnet2_8k`; Daily web calls (16 kHz) use
  `dpdfnet_base_16k`.

The template alone decides which filter a call gets:

| Template `noise_filter` | Filter |
| --- | --- |
| `provider: "noizzen"` | Noizzen |
| `provider: "aic"`, or the legacy `type` | ai-coustics; when it cannot run (no license key, missing model, a refused key at start or mid-call), that call falls back to Noizzen |

Rollout: a deploy changes nothing for existing templates. Move a template to Noizzen
by setting its `provider` to `"noizzen"`, and back by setting it to `"aic"`; the
change applies from the template's next call.

## How it works

`NoizzenFilter` is a pipecat `BaseAudioFilter`, set as the transport's
`audio_in_filter`. For each 10 ms hop of caller audio: window → FFT → one engine
step with the call's recurrent state → blend with the noisy frame from 4 hops back
(the model looks 40 ms ahead) → inverse FFT → overlap-add.

- Silence-skipping: a caller's line is near-silent for most of a call (networks
  mute it between words). A hop whose newest 10 ms is below −55 dBFS skips the
  engine and takes its output as zero, so the blend outputs half of the delayed
  original, which is what the model returns on silence anyway. The engine keeps
  running for 200 ms after the last louder hop, and the check reads audio 40 ms
  ahead of the output, so the engine is already running when speech is output.
  The engine's state is kept while skipped; framing, blend and delay don't change.
- The model is loaded once per process and shared by all calls; each call keeps
  its own state. Inference runs off the event loop on a small thread pool (one
  thread busy per call; prod runs one call per voice pod).
- Added delay: ~50 ms (40 ms look-ahead + 10 ms framing).
- `filter()` never raises (pipecat's input task only catches timeouts): an error,
  non-finite output or falling 100 ms behind real time passes the rest of the call
  through unfiltered. A missing or refused engine means no filter, never a failed call.
- Logs: `Noizzen engine loaded: <library>` once per process (which CPU build
  runs), and at the end of every call `Noizzen summary: engine skipped on N% of
  M steps; filter on to the end` (or `switched off: <reason>`).

## Noizzen engine

The model runs on the Noizzen engine (`app/ai/voice/noise_filter/noizzen/engine/`),
built the way ai-coustics describes theirs: one fixed program per model, weights
memory-mapped instead of copied, nothing allocated while audio is processed.

- `build.py` turns the ONNX graph into straight-line C (one kernel call per node,
  every shape and offset fixed) plus `<stem>.weights` (the float weights, large
  matrices pre-transposed, and the initial state). Unsupported ops or shapes fail
  the build instead of producing a wrong engine.
- `runtime.c` memory-maps the weights read-only, shared by every call in the
  process; each call allocates its scratch (~0.4 MB) and state once. On x86,
  denormals are flushed during each frame (decaying state in near-silence would
  otherwise stall the CPU).
- Libraries are built per CPU variant (`x86-64-v3` with AVX2/FMA, `x86-64-v2`,
  `arm64`); the loader picks the best one this CPU runs.
- The weights header (model, sample rate, window, hop), size and the CRC32 the
  library was built with are checked at load: a corrupt, truncated or other
  model's file is refused, and calls run without a filter.
- Output matches ONNX Runtime (`tests/breeze_buddy/test_noizzen_engine.py`:
  every supported op on a synthetic model, and the real models when provisioned).

## Models and provisioning

Ceva DPDFNet (huggingface.co/Ceva-IP/DPDFNet), Apache-2.0 for code and weights,
used unmodified: `onnx/dpdfnet2_8khz.onnx` and `onnx/baseline.onnx` (saved as
`dpdfnet_baseline_16khz.onnx`), revision `dd6818d00f50c836fed43a6243ebe49116de5964`.
Both are mirrored to `gs://breeze-clairvoyance-models/dpdfnet/`, next to the
ai-coustics models. GCP builds download them, check their sha256 (see the
Dockerfile) and build the engine into `BREEZE_BUDDY_NOISE_MODEL_DIR`
(`/app/models/voice/noizzen`), next to the ONNX files (kept for rebuilds; nothing
reads them at runtime). Missing or mismatched models are a build warning and
calls run without a noise filter; an engine build error fails the image. Builds
without the GCP token (AWS) skip both. Nothing is downloaded at runtime.

For local runs, build the engine next to the ONNX files and point
`BREEZE_BUDDY_NOISE_MODEL_DIR` at that folder:

```bash
uv run --no-project --with onnx==1.19.1 --with numpy==2.2.6 \
    python app/ai/voice/noise_filter/noizzen/engine/build.py <model_dir>
```

## Choice of model and setting (Oct 2026)

DPDFNet-2 at strength 0.5 with silence-skipping, against ai-coustics. Every filter
ran through its production class, in 20 ms chunks.

| Measured on | | No filter | quail-S | quail-L | Noizzen |
| --- | --- | --- | --- | --- | --- |
| 12 test calls (1) | Noise removed in pauses | – | 1.9 dB | 4.4 dB | 5.6 dB |
| | Voice clarity (SI-SDR) | 6.0 dB | 7.6 dB | 9.5 dB | 10.5 dB |
| | Soniox CER | 0.7% | 0.5% | 0.5% | 0.5% |
| 32 real calls (2) | Speech level change, median / worst call | – | −0.24 / −0.98 dB | −0.34 / −1.65 dB | −0.14 / −0.69 dB |
| | Engine skipped | – | – | – | 75% of steps |
| c2d node (3) | Filter CPU per call, 1–12 calls | – | 60–82m | 194–263m | 43–64m |
| | Added delay | – | 30 ms | 30 ms | ~50 ms |

1. Hindi and English speech with real fan, traffic and chatter noise at 0–15 dB,
   phone band, G.711; Apple M4.
2. Flipkart calls that placed an order or activated a line (26 Oct), customer
   channel of the Vobiz recordings; Apple M4.
3. The production machine type (c2d-standard-4, AMD) and image, real calls played
   in real time (reviewer's run, 28 Oct); ONNX Runtime with skipping: 41–67m. Per
   model step on this CPU: 1.08 ms on ONNX Runtime, 1.30 ms on the engine (1.22 ms
   with the comparison clamp in `fexp`); with skipping the per-call cost is equal
   within noise.

- Memory added per voice pod (Linux, with a voice pod's imports, VAD off as in
  prod): ~11 MB for the engine (~1 MB private; the rest is the shared, read-only
  weights file) vs ~33 MB for an ONNX Runtime session of the same model.
- At strength 0.75 (before the engine and silence-skipping), on Hindi and English
  phone audio with real noise: Sarvam CER 13.6% vs 16.5% for quail-S and 17.0%
  with no filter; Soniox 15.7% vs 16.5% and 16.5%; Whisper WER 27.5% vs 35.5% and
  36.6%. Full strength drops parts of words (Soniox 19.4%). Speech-to-text at 0.5
  with silence-skipping on live calls is measured in the canary.
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
`_create_aic_filter` in `transport.py` and the `aic` pipecat extra, and map
`provider: "aic"` templates to Noizzen.
