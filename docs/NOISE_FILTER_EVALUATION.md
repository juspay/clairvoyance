# Pipecat Noise Filter Evaluation for Breeze Buddy (8kHz Telephony)

## Context

Breeze Buddy is a telephony voice agent operating at **8kHz sample rate** over Twilio, Exotel, Plivo, and Telnyx transports. Audio is 16-bit PCM mono, often encoded as G.711 mulaw. Any noise filter must:

1. Work at or support **8kHz input** natively (or resample without breaking)
2. Add **< 50ms of latency** (algorithmic + processing combined)
3. Be compatible with pipecat's `audio_in_filter` on `FastAPIWebsocketParams`

---

## All Noise Filters in Latest Pipecat

| # | Filter | Class | Status | Package |
|---|--------|-------|--------|---------|
| 1 | **Krisp VIVA** | `KrispVivaFilter` | Active | `krisp_audio` (proprietary SDK) |
| 2 | **RNNoise** | `RNNoiseFilter` | Active | `pyrnnoise~=0.4.1` |
| 3 | **Koala** | `KoalaFilter` | Active | `pvkoala~=2.0.3` |
| 4 | **AIC (ai-coustics)** | `AICFilter` | Active | `aic-sdk~=2.0.1` |
| 5 | **Noisereduce** | `NoisereduceFilter` | **Deprecated** (v0.0.85) | `noisereduce~=3.0.3` |
| 6 | **Krisp Legacy** | `KrispFilter` | **Deprecated** (v0.0.94) | `pipecat-ai-krisp~=0.4.0` |

---

## Detailed Evaluation

### 1. KrispVivaFilter (Krisp VIVA SDK)

**Source:** `pipecat/audio/filters/krisp_viva_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| 8kHz native support | **Yes** — `Sr8000Hz` explicitly in `KRISP_SAMPLE_RATES` | PASS |
| Algorithmic latency | **10ms** (default frame duration) | PASS |
| Frame durations | 10ms, 15ms, 20ms, 30ms, 32ms | Flexible |
| Resampling needed | **No** — works natively at 8kHz | PASS |
| Processing overhead | Proprietary DNN, highly optimized C SDK | Minimal |
| Total expected latency | **~10-15ms** | PASS |
| License | Proprietary (requires `.kef` model file) | Commercial |
| Already in project | Yes — custom `NoiseFilterFromKrisp` in Automatic agent; model at `/app/models/voice/krisp/krisp-viva-tel-v2.kef` | Ready |

**Assessment:** **Best fit for Breeze Buddy.** Native 8kHz support with the telephony-optimized model (`krisp-viva-tel-v2.kef` — the "tel" suffix indicates telephony optimization). 10ms frame duration at 8kHz = 80 samples per frame. No resampling overhead. Already available in the project infrastructure.

---

### 2. RNNoiseFilter (RNNoise)

**Source:** `pipecat/audio/filters/rnnoise_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| 8kHz native support | **No** — hardcoded to 48kHz internally | FAIL (needs resampling) |
| Algorithmic latency | ~10ms at native 48kHz | OK in isolation |
| Resampling needed | **Yes** — 8kHz → 48kHz → process → 48kHz → 8kHz | Adds latency |
| Resampling method | `SOXRStreamAudioResampler` (VHQ quality by default) | Quality OK |
| Total expected latency | ~10ms (algo) + ~5-10ms (resample up) + ~5-10ms (resample down) = **~20-30ms** | PASS (marginal) |
| License | BSD (fully open source) | Free |
| Quality at 8kHz | Degraded — RNNoise was trained on 48kHz speech data. Upsampling 8kHz telephony audio to 48kHz doesn't recover lost high-frequency information; the model may not perform optimally on narrowband speech | Concern |

**Assessment:** **Usable but suboptimal for Breeze Buddy.** The double resampling (8k→48k→8k) fits within the 50ms budget but wastes cycles. More importantly, RNNoise was trained on wideband/fullband audio — narrowband 8kHz telephony audio upsampled to 48kHz will have zero energy above 4kHz, which may confuse the model or provide marginal denoising benefit. The filter defaults to `"QQ"` (quick quality) resampling to minimize latency.

---

### 3. KoalaFilter (PicoVoice Koala)

**Source:** `pipecat/audio/filters/koala_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| 8kHz native support | **No** — Koala requires **16kHz** (`pvkoala.create()` returns `sample_rate=16000`) | FAIL |
| Resampling support | **None built-in** — if transport rate != koala rate, filter marks itself `_koala_ready = False` and passes audio through unprocessed | FAIL |
| Algorithmic latency | ~20ms at 16kHz (256 samples/frame) | N/A |
| License | Proprietary (requires PicoVoice access key) | Commercial |

**Assessment:** **Not usable for Breeze Buddy.** Koala only works at 16kHz. When the transport provides 8kHz audio, the filter logs a warning and **disables itself entirely** (returns audio unprocessed). There is no built-in resampling fallback. Would need a wrapper that resamples 8k→16k→process→16k→8k, which pipecat doesn't provide for this filter.

---

### 4. AICFilter (ai-coustics)

**Source:** `pipecat/audio/filters/aic_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| 8kHz native support | **Yes** — ai-coustics SDK supports 8kHz, 16kHz, 48kHz streaming | PASS |
| Algorithmic latency | Model-dependent; SDK reports via `processor_ctx.get_output_delay()` | Needs testing |
| Expected latency | ai-coustics claims ~20ms for streaming applications | PASS (likely) |
| Resampling needed | **No** — SDK handles sample rate internally via `ProcessorConfig.optimal(model, sample_rate)` | PASS |
| Processing overhead | Rust-based AirTen inference engine, ~1-2% CPU on Intel Xeon | Minimal |
| License | Proprietary (requires license key + model download) | Commercial |
| Extra features | Built-in VAD, noise gate, voice gain, enhancement levels | Valuable |
| Already in project | Partially — used in Automatic agent with older API (`enhancement_level`, `voice_gain` params) | Needs adaptation |

**Assessment:** **Strong candidate for Breeze Buddy.** Native 8kHz support, low CPU footprint, and the SDK was designed with telephony use cases in mind (explicitly mentions g711/mulaw codec handling). The integrated VAD could potentially replace or complement Silero VAD. However, requires a license key and model download, and the pipecat `AICFilter` API has changed (now uses `model_id`/`model_path` instead of the older `enhancement_level`/`voice_gain` params the Automatic agent uses).

---

### 5. NoisereduceFilter (Spectral Gating) — DEPRECATED

**Source:** `pipecat/audio/filters/noisereduce_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| 8kHz native support | **Yes** — accepts any sample rate via `sr` parameter | PASS |
| Algorithmic latency | **Variable and unpredictable** — `noisereduce.reduce_noise()` is designed for offline/batch processing, not streaming | CONCERN |
| Resampling needed | No | PASS |
| Processing approach | Spectral gating — STFT-based, processes entire input chunk at once | Not truly streaming |
| License | MIT (open source) | Free |
| Status | **Deprecated since pipecat v0.0.85** | Do not use |

**Assessment:** **Do not use for Breeze Buddy.** Deprecated by pipecat maintainers. The `noisereduce` library was designed for offline batch processing, not real-time streaming. Processing latency is chunk-size dependent and can spike unpredictably. Already enabled as default in the project (`ENABLE_NOISE_REDUCE_FILTER=true`) but should be replaced.

---

### 6. KrispFilter (Legacy) — DEPRECATED

**Source:** `pipecat/audio/filters/krisp_filter.py`

| Criteria | Value | Verdict |
|----------|-------|---------|
| Status | **Deprecated since pipecat v0.0.94**, replaced by `KrispVivaFilter` | Do not use |

**Assessment:** **Do not use.** Superseded by `KrispVivaFilter`. Will be removed in a future pipecat version.

---

## Summary Matrix

| Filter | 8kHz Native | Latency (est.) | Sub-50ms | Usable in Breeze Buddy | Recommendation |
|--------|:-----------:|:--------------:|:--------:|:----------------------:|:--------------:|
| **KrispVivaFilter** | Yes | ~10-15ms | Yes | **Yes** | **TOP PICK** |
| **AICFilter** | Yes | ~20ms | Yes | **Yes** | **STRONG PICK** |
| **RNNoiseFilter** | No (resamples) | ~20-30ms | Yes (marginal) | **Conditionally** | Fallback option |
| **KoalaFilter** | No (16kHz only) | N/A | N/A | **No** | Not compatible |
| **NoisereduceFilter** | Yes | Unpredictable | Unreliable | **No** | Deprecated |
| **KrispFilter** | N/A | N/A | N/A | **No** | Deprecated |

---

## Recommendations for Breeze Buddy

### Primary: KrispVivaFilter

- Already integrated in the project infrastructure (model file, config flags)
- Native 8kHz with telephony-optimized model (`krisp-viva-tel-v2.kef`)
- 10ms frame duration = lowest latency of all options
- To enable: set `ENABLE_KRISP_FILTER=true` and pass `audio_in_filter=KrispVivaFilter(model_path=KRISP_MODEL_PATH)` in transport params

### Secondary: AICFilter

- Native 8kHz support with telephony-aware processing
- Built-in VAD could improve speech detection quality
- Higher quality enhancement (denoising + speech enhancement)
- Requires license key and model provisioning
- To enable: set `ENABLE_AIC_FILTER=true`, provide `AICOUSTICS_LICENSE_KEY`, and configure model

### Fallback: RNNoiseFilter

- Free and open source (no license keys needed)
- Works at 8kHz via automatic resampling (8k→48k→8k)
- ~20-30ms latency still within 50ms budget
- Quality may be suboptimal on narrowband telephony audio
- Good option for development/testing without commercial dependencies

### Not Recommended

- **KoalaFilter** — Does not work at 8kHz, no resampling support, disables itself
- **NoisereduceFilter** — Deprecated, unpredictable latency, not designed for streaming
- **KrispFilter (legacy)** — Deprecated, use KrispVivaFilter instead

---

## Current State in Breeze Buddy

The Breeze Buddy telephony pipeline (`app/ai/voice/agents/breeze_buddy/agent/transport.py`) currently does **not** set any `audio_in_filter` on the `FastAPIWebsocketParams`. The noise filtering config flags (`ENABLE_KRISP_FILTER`, `ENABLE_AIC_FILTER`, `ENABLE_NOISE_REDUCE_FILTER`) are only applied in the **Automatic agent** (`app/ai/voice/agents/automatic/__init__.py`), not in Breeze Buddy.

The `FastAPIWebsocketParams` base class does support `audio_in_filter` (inherited from `TransportParams`), so adding a filter to Breeze Buddy's telephony transports is straightforward — pass the filter instance in the transport params.

---

## References

- Pipecat filters source: `pipecat-ai/pipecat/src/pipecat/audio/filters/`
- Krisp VIVA SDK sample rates: `pipecat-ai/pipecat/src/pipecat/audio/krisp_instance.py`
- Breeze Buddy transport config: `app/ai/voice/agents/breeze_buddy/agent/transport.py`
- Breeze Buddy VAD config (8kHz): `app/ai/voice/agents/breeze_buddy/agent/vad.py`
- PicoVoice Koala docs: https://picovoice.ai/docs/koala/
- ai-coustics SDK docs: https://docs.ai-coustics.com/sdk/overview
- RNNoise project: https://jmvalin.ca/demo/rnnoise/
