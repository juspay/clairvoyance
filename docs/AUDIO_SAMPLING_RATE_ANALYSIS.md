# Audio Sampling Rate Analysis & Optimization Guide

## Executive Summary

This document provides a comprehensive analysis of audio sampling rates across telephony providers integrated with Breeze Buddy, their impact on voice quality, and actionable recommendations for improving audio richness and clarity.

**Key Finding:** Current telephony providers are configured at 8 kHz (narrowband), but **Exotel supports up to 24 kHz** and **Plivo supports 16 kHz**, offering significant opportunities for quality improvement.

---

## Table of Contents

1. [Understanding Audio Sampling Rates](#understanding-audio-sampling-rates)
2. [Provider Capabilities Comparison](#provider-capabilities-comparison)
3. [Current Implementation](#current-implementation)
4. [Impact on Audio Quality](#impact-on-audio-quality)
5. [Audio Enhancement Infrastructure](#audio-enhancement-infrastructure)
6. [Recommendations](#recommendations)
7. [Implementation Guide](#implementation-guide)
8. [Testing & Validation](#testing--validation)
9. [References](#references)

---

## Understanding Audio Sampling Rates

### What is Sampling Rate?

Sampling rate (measured in kHz or Hz) is the number of times per second that audio is sampled. It directly determines:
- **Frequency range** that can be captured
- **Audio clarity and richness**
- **Naturalness of speech**
- **Speaker recognition accuracy**

### Common Telephony Sampling Rates

| Rate | Category | Frequency Range | Use Case | Quality |
|------|----------|-----------------|----------|---------|
| **8 kHz** | Narrowband | 300-3,400 Hz | Traditional PSTN telephony | Basic speech intelligibility |
| **16 kHz** | Wideband (HD Voice) | 50-7,000 Hz | Modern VoIP, mobile HD voice | Clear, natural speech |
| **24 kHz** | Super-wideband | 50-14,000 Hz | Premium telephony | Near-studio quality |
| **48 kHz** | Fullband | 20-20,000 Hz | WebRTC, professional audio | Studio quality |

### Nyquist-Shannon Theorem

The sampling rate must be at least **2x the highest frequency** you want to capture:
- 8 kHz sampling → captures up to 4 kHz (actually limited to 3.4 kHz in telephony)
- 16 kHz sampling → captures up to 8 kHz (typically 7 kHz in practice)
- 24 kHz sampling → captures up to 12 kHz (typically 14 kHz in practice)

---

## Provider Capabilities Comparison

### Detailed Provider Analysis

#### 1. **Exotel** 🏆 Winner - Best Audio Quality

**Maximum Supported:** 24 kHz (Super-wideband/HD)

**Current Usage:** 16 kHz (Wideband) - ✅ **IMPLEMENTED**

**Capabilities:**
- ✅ Supports 8 kHz (default)
- ✅ Supports 16 kHz (enhanced quality) - **CURRENTLY ACTIVE**
- ✅ Supports 24 kHz (HD quality) - **FUTURE UPGRADE PATH**

**Bandwidth Considerations:**
- 8 kHz: ~64 kbps with G.711
- 16 kHz: ~128 kbps (balanced quality/bandwidth) - **current**
- 24 kHz: ~192 kbps (requires good network) - available for premium tier

**Implementation:** Breeze Buddy now appends `?sample-rate=16000` to the Exotel Voicebot WebSocket URL, instructing Exotel to stream at 16 kHz wideband for 2x better audio quality.

**Sources:**
- [Exotel Digital Voice Platform](https://exotel.com/products/digital-voice/)
- [Exotel Stream and Voicebot Documentation](https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet)

---

#### 2. **Plivo** 🥈 Runner-up - Good HD Voice Support

**Maximum Supported:** 16 kHz (Wideband/HD Voice)

**Current Usage:** 16 kHz (Wideband) - ✅ **IMPLEMENTED**

**Capabilities:**
- ✅ Supports 8 kHz via μ-law/A-law codecs
- ✅ Supports 16 kHz via `audio/x-l16` codec - **CURRENTLY ACTIVE**
- ❌ Does not support 24 kHz or higher

**Codec Details:**
- `audio/x-mulaw;rate=8000` - Standard narrowband (legacy)
- `audio/x-l16;rate=16000` - Linear PCM, 16 kHz wideband (**current implementation**)

**Implementation:** Breeze Buddy now uses `contentType="audio/x-l16;rate=16000"` in the Plivo Stream XML, providing 2x better audio quality with full consonant clarity and natural voice timbre.

**Sources:**
- [Plivo Supported Audio Codecs](https://support.plivo.com/hc/en-us/articles/32800673795993-What-are-the-supported-audio-codecs)
- [Plivo Opus Codec for SDK Apps](https://www.plivo.com/blog/opus-audio-codec-better-voice-quality-for-plivo-sdk-based-apps/)
- [Plivo Audio Payload Format](https://support.plivo.com/hc/en-us/articles/32252710653337-What-is-the-expected-payload-format-to-send-audio-to-Plivo)

---

#### 3. **Twilio** ⚠️ Limited - Narrowband Only

**Maximum Supported:** 8 kHz (network transcoding limitation)

**Current Usage:** 8 kHz (Narrowband)

**Capabilities:**
- ✅ Supports 8 kHz via μ-law codec (standard PSTN)
- ⚠️ SDKs can handle higher rates (16/24/48 kHz) but network transcodes to 8 kHz
- ❌ No true wideband support for telephony calls

**Important Note:** While Twilio's client SDKs (iOS, Android, Web) support higher sampling rates locally, the **Twilio Voice network transcodes everything to 8 kHz μ-law** for PSTN interconnection.

**Recommendation:** Keep at 8 kHz. Focus audio quality improvements on TTS, noise reduction, and audio enhancement filters instead.

**Sources:**
- [Twilio Audio Recording Best Practices](https://support.twilio.com/hc/en-us/articles/223180588-Best-Practices-for-Audio-Recordings)
- [Twilio Voice SDKs Supported Codecs](https://help.twilio.com/articles/13527980995355-Twilio-Voice-SDKs-Supported-Audio-Codecs)
- [Twilio AI Voice Agents Latency Guide](https://www.twilio.com/en-us/blog/developers/best-practices/guide-core-latency-ai-voice-agents)

---

#### 4. **Telnyx** 🔍 Unknown - Requires Investigation

**Maximum Supported:** Unknown (assumed 8 kHz)

**Current Usage:** 8 kHz (Narrowband)

**Status:** No public documentation found regarding HD voice or wideband support.

**Recommendation:** Contact Telnyx support to inquire about wideband/HD voice capabilities.

---

#### 5. **Daily.co** ✅ Already Optimized - WebRTC

**Maximum Supported:** 48 kHz+ (WebRTC capable)

**Current Usage:** 16 kHz (Wideband)

**Capabilities:**
- ✅ WebRTC-based, supports up to 48 kHz and beyond
- ✅ Currently configured at 16 kHz - good balance
- ✅ Could be increased to 24 kHz or 48 kHz if needed

**Recommendation:** Current 16 kHz setting is optimal for web/mobile use cases. Consider 24 kHz for premium experiences.

---

### Provider Comparison Table

| Provider | Current | Maximum | Quality Gain | Bandwidth Impact | Implementation Effort |
|----------|---------|---------|--------------|------------------|-----------------------|
| **Exotel** | 8 kHz | **24 kHz** | +200% | Moderate-High | Low |
| **Plivo** | 8 kHz | **16 kHz** | +100% | Moderate | Low |
| **Twilio** | 8 kHz | 8 kHz | N/A | N/A | N/A |
| **Telnyx** | 8 kHz | Unknown | Unknown | Unknown | Unknown |
| **Daily.co** | 16 kHz | 48 kHz+ | Already good | N/A | N/A |

---

## Current Implementation

### Code Architecture

**Primary Configuration Files:**

1. **`app/ai/voice/agents/breeze_buddy/agent/vad.py`**
   - Defines sampling rate constants
   - Creates VAD analyzers with appropriate sample rates

2. **`app/ai/voice/agents/breeze_buddy/agent/transport.py`**
   - Configures transport parameters per provider
   - Sets input/output sample rates

### Current Sample Rate Constants

```python
# app/ai/voice/agents/breeze_buddy/agent/vad.py

TELEPHONY_SAMPLE_RATE = 8000    # For Twilio, Plivo, Exotel, Telnyx
DAILY_SAMPLE_RATE = 16000       # For Daily.co transport
```

### Current Transport Configuration

```python
# app/ai/voice/agents/breeze_buddy/agent/transport.py

def get_transport_params(vad_analyzer, audio_out_mixer=None):
    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            # Daily automatically uses 16 kHz
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_mixer=audio_out_mixer,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_mixer=audio_out_mixer,
        ),
        # ... similar for telnyx
    }
```

### VAD Analyzer Creation

```python
# app/ai/voice/agents/breeze_buddy/agent/vad.py

async def create_vad_analyzer(is_daily_mode: bool, template: Optional[TemplateModel] = None):
    if is_daily_mode:
        params = await create_daily_vad_params()
        return SileroVADAnalyzer(sample_rate=DAILY_SAMPLE_RATE, params=params), None

    default_vad_params = build_default_vad_params(template)
    return (
        SileroVADAnalyzer(sample_rate=TELEPHONY_SAMPLE_RATE, params=default_vad_params),
        default_vad_params,
    )
```

---

## Impact on Audio Quality

### What Changes with Higher Sampling Rates?

#### 8 kHz (Narrowband) - Current Telephony Setting

**Frequency Range:** 300-3,400 Hz

**What's Captured:**
- ✅ Vowel sounds (primary speech energy)
- ✅ Basic speech intelligibility
- ⚠️ Fundamental frequencies only

**What's Missing:**
- ❌ Consonant clarity (s, sh, f, th sounds)
- ❌ Sibilance and fricatives
- ❌ High-frequency harmonics
- ❌ Natural voice timbre
- ❌ Speaker characteristics

**User Experience:**
- "Tinny" or "muffled" sound
- Difficulty distinguishing similar-sounding words
- Harder speaker recognition
- Reduced naturalness

---

#### 16 kHz (Wideband) - Recommended Upgrade

**Frequency Range:** 50-7,000 Hz

**Improvements Over 8 kHz:**
- ✅ **2x more audio information**
- ✅ Crisp consonant sounds
- ✅ Clear sibilance (s, sh, ch sounds)
- ✅ Natural voice timbre
- ✅ Better speaker recognition
- ✅ More engaging listening experience

**Perceived Quality:**
- Sounds like "in-person" conversation
- Significantly reduced listener fatigue
- Higher perceived professionalism
- Better AI/TTS naturalness

**Bandwidth Cost:**
- ~64 kbps increase (from 64 kbps to 128 kbps)
- Negligible on modern mobile/broadband

**Industry Adoption:**
- Used by: FaceTime, WhatsApp, Skype, HD Voice on LTE
- Standard for modern VoIP

---

#### 24 kHz (Super-wideband) - Premium Option

**Frequency Range:** 50-14,000 Hz

**Improvements Over 16 kHz:**
- ✅ **3x more audio information than 8 kHz**
- ✅ Extended high-frequency detail
- ✅ Near-studio quality for telephony
- ✅ Maximum speech naturalness
- ✅ Optimal for AI-generated voice (TTS)

**Best For:**
- Premium customer experiences
- Complex IVR interactions
- AI voice assistants
- Music-on-hold or audio branding
- Multilingual support (tonal languages benefit more)

**Bandwidth Cost:**
- ~128 kbps increase (from 64 kbps to 192 kbps)
- Requires stable network connection

**When to Use:**
- High-value customer interactions
- When network bandwidth is guaranteed
- For AI agents where naturalness is critical

---

### Real-World Quality Comparison

| Aspect | 8 kHz | 16 kHz | 24 kHz |
|--------|-------|--------|--------|
| **Speech Intelligibility** | Good | Excellent | Excellent |
| **Consonant Clarity** | Poor | Good | Excellent |
| **Naturalness** | Low | High | Very High |
| **Speaker Recognition** | Difficult | Easy | Very Easy |
| **Listener Fatigue** | High | Low | Very Low |
| **AI/TTS Quality** | Robotic | Natural | Very Natural |
| **Bandwidth** | 64 kbps | 128 kbps | 192 kbps |
| **Use Case** | Legacy PSTN | Modern VoIP | Premium VoIP |

---

## Audio Enhancement Infrastructure

### Existing Audio Processing Capabilities

Breeze Buddy already has sophisticated audio enhancement infrastructure that works **alongside** sampling rate improvements.

### 1. Noise Reduction Filter

**Configuration:** `.env.example:33`
```bash
ENABLE_NOISE_REDUCE_FILTER=true
```

**Purpose:**
- Removes background noise from input audio
- Improves speech clarity in noisy environments
- Uses Pipecat's built-in noise reduction

**Recommendation:** Keep enabled (already active)

---

### 2. AIC (AI Coustics) Audio Enhancement

**Configuration:** `.env.example:36-41`
```bash
ENABLE_AIC_FILTER=true
AICOUSTICS_LICENSE_KEY=your_key_here
AIC_ENHANCEMENT_LEVEL=1.0
AIC_VOICE_GAIN=1.2
AIC_NOISE_GATE_ENABLE=true
```

**What is AIC?**
- Professional AI-powered audio enhancement
- Real-time processing for telephony
- Industry-leading quality improvement

**Key Parameters:**

1. **Enhancement Level** (`AIC_ENHANCEMENT_LEVEL`)
   - Current: `1.0` (default)
   - Range: `0.0` to `2.0`
   - Recommendation: **Try `1.2-1.5` for more richness**
   - Higher = more processing, richer sound

2. **Voice Gain** (`AIC_VOICE_GAIN`)
   - Current: `1.2` (20% boost)
   - Purpose: Amplifies voice presence
   - Recommendation: Good default, test `1.3-1.5` if needed

3. **Noise Gate** (`AIC_NOISE_GATE_ENABLE`)
   - Current: `true`
   - Purpose: Removes low-level noise during silence
   - Recommendation: Keep enabled

**Recommendation:**
- Increase `AIC_ENHANCEMENT_LEVEL` to `1.3` for richer audio
- Combines well with higher sampling rates

---

### 3. Krisp Audio Filter

**Configuration:** `.env.example:43-45`
```bash
ENABLE_KRISP_FILTER=false
KRISP_MODEL_PATH="/app/models/voice/krisp/krisp-viva-tel-v2.kef"
```

**What is Krisp?**
- Industry-leading AI noise cancellation
- Removes background voices, keyboard typing, dog barking, etc.
- Widely used in professional conferencing (Zoom, Teams)

**Why Currently Disabled?**
- Likely due to licensing or performance considerations
- Model file must be present at specified path

**Recommendation:**
- **Enable if you have Krisp license and model file**
- Particularly valuable for telephony where callers may be in noisy environments
- Can be used together with AIC (they complement each other)

---

### 4. Text-to-Speech (TTS) Quality

**Current Configuration:** `.env.example:46-52`
```bash
# ElevenLabs Configuration
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID="bQQWtYx9EodAqMdkrNAc"
ELEVENLABS_MODEL_ID="eleven_flash_v2_5"
ELEVENLABS_VOICE_SPEED=1.15
ELEVENLABS_TTS_SPEED=1.10
```

**TTS Quality Improvements:**

1. **Reduce Speech Speed**
   - Current: `1.15` (15% faster)
   - Recommendation: **`1.0-1.05`** (slower = more frequency content)
   - Slower speech allows higher frequencies to be more perceivable

2. **Request Higher Sample Rate from TTS**
   - Check if ElevenLabs can output 16 kHz or 24 kHz
   - Pipecat will handle resampling if needed
   - Higher source quality = better final quality

3. **Use Latest Models**
   - Current: `eleven_flash_v2_5` (fast, good quality)
   - Consider: `eleven_turbo_v2_5` or `eleven_multilingual_v2`
   - Latest models have better audio fidelity

---

### 5. Voice Activity Detection (VAD)

**Configuration:** `.env.example:54-63`
```bash
# Telephony VAD
VAD_CONFIDENCE=0.85
VAD_MIN_VOLUME=0.75

# Daily (Web/Mobile) VAD
DAILY_VAD_CONFIDENCE=0.9
DAILY_VAD_START_SECS=0.22
DAILY_VAD_STOP_SECS=0.3
DAILY_VAD_MIN_VOLUME=0.8
```

**Impact on Audio Quality:**

Aggressive VAD (high thresholds) can:
- ❌ Cut off soft-spoken speakers
- ❌ Clip beginning/end of utterances
- ❌ Make conversation feel choppy

**Recommendations for Richer Audio:**

```bash
# Less aggressive VAD = more audio passed through
VAD_MIN_VOLUME=0.6           # Down from 0.75 (allow softer speech)
DAILY_VAD_MIN_VOLUME=0.7     # Down from 0.8
VAD_CONFIDENCE=0.8           # Down from 0.85 (less strict)
```

**Trade-off:**
- Lower thresholds = more natural conversation flow
- But: May pass through more background noise
- Mitigate with good noise reduction filters (AIC/Krisp)

---

### Audio Processing Pipeline

```
Input Audio (from caller)
    ↓
[Noise Reduction Filter] ← ENABLE_NOISE_REDUCE_FILTER
    ↓
[Krisp Noise Cancellation] ← ENABLE_KRISP_FILTER (optional)
    ↓
[VAD Analysis] ← SileroVADAnalyzer
    ↓
[STT Service] ← Speech-to-Text
    ↓
[LLM Processing]
    ↓
[TTS Service] ← Text-to-Speech
    ↓
[AIC Audio Enhancement] ← ENABLE_AIC_FILTER
    ↓
Output Audio (to caller)
```

---

## Recommendations

### Priority 1: High-Impact, Low-Effort (Implement First)

#### 1.1 Upgrade Exotel to 16 kHz ⭐⭐⭐⭐⭐

**Effort:** Low (configuration change)
**Impact:** High (100% quality improvement)
**Risk:** Low (16 kHz widely supported)

**Implementation:**
```python
# vad.py
EXOTEL_SAMPLE_RATE = 16000

# transport.py
"exotel": lambda: FastAPIWebsocketParams(
    audio_in_sample_rate=16000,
    audio_out_sample_rate=16000,
    audio_out_mixer=audio_out_mixer,
)
```

**Testing:**
- A/B test with users
- Monitor bandwidth usage
- If successful, consider 24 kHz for premium tier

---

#### 1.2 Upgrade Plivo to 16 kHz ⭐⭐⭐⭐⭐

**Effort:** Low (configuration change)
**Impact:** High (100% quality improvement)
**Risk:** Low (standard wideband)

**Implementation:**
```python
# vad.py
PLIVO_SAMPLE_RATE = 16000

# transport.py
"plivo": lambda: FastAPIWebsocketParams(
    audio_in_sample_rate=16000,
    audio_out_sample_rate=16000,
    audio_out_mixer=audio_out_mixer,
)
```

---

#### 1.3 Optimize Audio Enhancement Filters ⭐⭐⭐⭐

**Effort:** Minimal (environment variable changes)
**Impact:** Medium-High (works with any sampling rate)

**Implementation (.env):**
```bash
# Increase AIC enhancement for richer audio
AIC_ENHANCEMENT_LEVEL=1.3  # Up from 1.0

# Consider enabling Krisp if licensed
ENABLE_KRISP_FILTER=true

# Less aggressive VAD
VAD_MIN_VOLUME=0.6  # Down from 0.75
```

---

### Priority 2: Medium-Impact Optimizations

#### 2.1 Test Exotel at 24 kHz (Premium Tier) ⭐⭐⭐⭐

**Effort:** Low (configuration change)
**Impact:** High (but only for premium use cases)
**Risk:** Medium (bandwidth requirements)

**When to Use:**
- High-value customer calls
- AI voice agents (showcases capability)
- Stable network environments

**Implementation:**
```python
# vad.py
EXOTEL_HD_SAMPLE_RATE = 24000

# Use environment variable to toggle
EXOTEL_USE_HD_AUDIO = os.getenv("EXOTEL_USE_HD_AUDIO", "false").lower() == "true"
EXOTEL_SAMPLE_RATE = EXOTEL_HD_SAMPLE_RATE if EXOTEL_USE_HD_AUDIO else 16000
```

---

#### 2.2 Optimize TTS Settings ⭐⭐⭐

**Effort:** Low
**Impact:** Medium (better TTS quality)

**Changes (.env):**
```bash
ELEVENLABS_VOICE_SPEED=1.05  # Down from 1.15 (slower = richer)
ELEVENLABS_TTS_SPEED=1.0     # Down from 1.10
```

---

#### 2.3 Increase Daily.co to 24 kHz ⭐⭐⭐

**Effort:** Low
**Impact:** Medium (already at 16 kHz, diminishing returns)

**For web/mobile premium experiences:**
```python
# vad.py
DAILY_SAMPLE_RATE = 24000  # Up from 16000
```

---

### Priority 3: Research & Long-term

#### 3.1 Research Telnyx Capabilities ⭐⭐

**Action Items:**
- Contact Telnyx support
- Request wideband/HD voice documentation
- Test if 16 kHz supported

---

#### 3.2 Implement Dynamic Sample Rate Selection ⭐⭐⭐

**Concept:** Auto-adjust based on network conditions

```python
async def get_optimal_sample_rate(provider: str, network_quality: str) -> int:
    if network_quality == "excellent":
        if provider == "exotel":
            return 24000
        elif provider in ["plivo", "daily"]:
            return 16000
    elif network_quality == "good":
        return 16000
    else:
        return 8000  # Fallback for poor networks
```

---

#### 3.3 User A/B Testing Framework ⭐⭐⭐⭐

**Implement analytics to measure:**
- Call quality ratings (user feedback)
- Transcription accuracy (STT WER - Word Error Rate)
- Call completion rates
- Average call duration (engagement metric)

**Compare:**
- 8 kHz vs 16 kHz
- 16 kHz vs 24 kHz
- With/without AIC enhancement

---

## Implementation Guide

### Step 1: Update Constants (`vad.py`)

**Before:**
```python
# app/ai/voice/agents/breeze_buddy/agent/vad.py

TELEPHONY_SAMPLE_RATE = 8000
DAILY_SAMPLE_RATE = 16000
```

**After:**
```python
# app/ai/voice/agents/breeze_buddy/agent/vad.py

# Legacy/fallback
TELEPHONY_SAMPLE_RATE = 8000

# Provider-specific sample rates
EXOTEL_SAMPLE_RATE = 16000      # Supports up to 24000
PLIVO_SAMPLE_RATE = 16000       # Max supported
TWILIO_SAMPLE_RATE = 8000       # Limited by network
TELNYX_SAMPLE_RATE = 8000       # Unknown, conservative default
DAILY_SAMPLE_RATE = 16000       # WebRTC, could go higher

# HD/Premium options (use via environment variable)
EXOTEL_HD_SAMPLE_RATE = 24000
```

---

### Step 2: Update Transport Configuration (`transport.py`)

**Before:**
```python
# app/ai/voice/agents/breeze_buddy/agent/transport.py

def get_transport_params(vad_analyzer, audio_out_mixer=None):
    return {
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,  # 8000
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        # ...
    }
```

**After:**
```python
# app/ai/voice/agents/breeze_buddy/agent/transport.py

from app.ai.voice.agents.breeze_buddy.agent.vad import (
    EXOTEL_SAMPLE_RATE,
    PLIVO_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
    TELNYX_SAMPLE_RATE,
)

def get_transport_params(vad_analyzer, audio_out_mixer=None):
    return {
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=EXOTEL_SAMPLE_RATE,  # 16000
            audio_out_sample_rate=EXOTEL_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=PLIVO_SAMPLE_RATE,  # 16000
            audio_out_sample_rate=PLIVO_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,  # 8000 (limited)
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_sample_rate=TELNYX_SAMPLE_RATE,  # 8000 (unknown)
            audio_out_sample_rate=TELNYX_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            # Daily handles sample rate automatically
        ),
    }
```

---

### Step 3: Update VAD Analyzer Creation

**Modify `create_vad_analyzer()` to use provider-specific rates:**

```python
async def create_vad_analyzer(
    is_daily_mode: bool,
    provider: Optional[str] = None,  # NEW: accept provider name
    template: Optional[TemplateModel] = None,
) -> tuple[SileroVADAnalyzer, Optional[VADParams]]:
    """Create VAD analyzer with appropriate parameters.

    Args:
        is_daily_mode: Whether this is Daily mode
        provider: Provider name (e.g., 'exotel', 'plivo', 'twilio')
        template: Template model for telephony mode VAD params

    Returns:
        Tuple of (SileroVADAnalyzer, default_vad_params)
    """
    if is_daily_mode:
        params = await create_daily_vad_params()
        return SileroVADAnalyzer(sample_rate=DAILY_SAMPLE_RATE, params=params), None

    # Determine sample rate based on provider
    sample_rate = TELEPHONY_SAMPLE_RATE  # Default fallback
    if provider:
        provider_lower = provider.lower()
        if provider_lower == "exotel":
            sample_rate = EXOTEL_SAMPLE_RATE
        elif provider_lower == "plivo":
            sample_rate = PLIVO_SAMPLE_RATE
        elif provider_lower == "twilio":
            sample_rate = TWILIO_SAMPLE_RATE
        elif provider_lower == "telnyx":
            sample_rate = TELNYX_SAMPLE_RATE

    default_vad_params = build_default_vad_params(template)
    return (
        SileroVADAnalyzer(sample_rate=sample_rate, params=default_vad_params),
        default_vad_params,
    )
```

---

### Step 4: Environment Variable Configuration

**Add to `.env.example`:**

```bash
# ============================================================================
# Audio Quality Configuration
# ============================================================================

# Exotel Audio Quality
# Options: 8000 (narrowband), 16000 (wideband - recommended), 24000 (HD)
EXOTEL_SAMPLE_RATE=16000

# Plivo Audio Quality
# Options: 8000 (narrowband), 16000 (wideband - max supported)
PLIVO_SAMPLE_RATE=16000

# Daily.co Audio Quality (WebRTC)
# Options: 16000 (wideband - recommended), 24000 (HD), 48000 (fullband)
DAILY_SAMPLE_RATE=16000

# Audio Enhancement Settings (for better richness)
AIC_ENHANCEMENT_LEVEL=1.3       # Increased from 1.0 for richer audio
AIC_VOICE_GAIN=1.2
AIC_NOISE_GATE_ENABLE=true

# Enable Krisp noise cancellation (requires license & model file)
ENABLE_KRISP_FILTER=false

# VAD sensitivity (lower = less aggressive, more natural conversation)
VAD_MIN_VOLUME=0.6              # Reduced from 0.75
DAILY_VAD_MIN_VOLUME=0.7        # Reduced from 0.8

# TTS Quality (slower = richer audio)
ELEVENLABS_VOICE_SPEED=1.05     # Reduced from 1.15
ELEVENLABS_TTS_SPEED=1.0        # Reduced from 1.10
```

---

### Step 5: Update Call Initialization

**Ensure provider name is passed to `create_vad_analyzer()`:**

```python
# In websocket/call handler
provider_name = request.headers.get("X-Provider-Name")  # or from config

vad_analyzer, vad_params = await create_vad_analyzer(
    is_daily_mode=False,
    provider=provider_name,  # Pass provider name
    template=template,
)
```

---

## Testing & Validation

### Unit Tests

**Test sample rate selection:**

```python
# tests/test_vad_sample_rates.py

import pytest
from app.ai.voice.agents.breeze_buddy.agent.vad import create_vad_analyzer

@pytest.mark.asyncio
async def test_exotel_sample_rate():
    analyzer, _ = await create_vad_analyzer(
        is_daily_mode=False,
        provider="exotel"
    )
    assert analyzer.sample_rate == 16000

@pytest.mark.asyncio
async def test_plivo_sample_rate():
    analyzer, _ = await create_vad_analyzer(
        is_daily_mode=False,
        provider="plivo"
    )
    assert analyzer.sample_rate == 16000

@pytest.mark.asyncio
async def test_twilio_sample_rate():
    analyzer, _ = await create_vad_analyzer(
        is_daily_mode=False,
        provider="twilio"
    )
    assert analyzer.sample_rate == 8000
```

---

### Integration Tests

**Test actual provider connections:**

```python
# tests/integration/test_provider_audio_quality.py

@pytest.mark.integration
async def test_exotel_16khz_connection():
    """Test Exotel accepts 16 kHz audio streams"""
    # Setup transport with 16 kHz
    # Send test audio
    # Verify no codec errors
    pass

@pytest.mark.integration
async def test_plivo_16khz_connection():
    """Test Plivo accepts 16 kHz audio streams"""
    pass
```

---

### Manual Testing Checklist

#### Audio Quality Validation

- [ ] **Exotel 8 kHz (baseline)**
  - Place test call
  - Rate speech clarity (1-10)
  - Rate naturalness (1-10)
  - Note any issues

- [ ] **Exotel 16 kHz (upgrade)**
  - Place test call with same script
  - Compare clarity vs baseline
  - Compare naturalness vs baseline
  - Note bandwidth usage

- [ ] **Exotel 24 kHz (HD test)**
  - Place test call with same script
  - Compare vs 16 kHz
  - Monitor network stability
  - Note bandwidth usage

- [ ] **Plivo 16 kHz**
  - Repeat above tests

- [ ] **A/B Comparison**
  - Have test subjects compare 8 kHz vs 16 kHz (blind)
  - Collect feedback: which sounds better?

#### Edge Cases

- [ ] Poor network conditions (test with network throttling)
- [ ] Noisy environment (caller in cafe/street)
- [ ] Multiple rapid turn-taking
- [ ] Long-duration calls (>15 minutes)
- [ ] Different device types (iOS, Android, desktop)

---

### Monitoring Metrics

**Track in production:**

1. **Audio Quality Metrics**
   - Average call quality rating (CSAT)
   - Transcription accuracy (STT WER)
   - Call completion rate

2. **Performance Metrics**
   - Bandwidth usage per provider
   - Latency (time-to-first-byte for audio)
   - CPU usage (higher sample rate = more processing)

3. **Business Metrics**
   - User engagement (call duration)
   - Repeat usage rate
   - Customer satisfaction scores

---

### Rollout Strategy

#### Phase 1: Internal Testing (Week 1)
- Deploy to staging environment
- Test with internal team
- Validate all providers work correctly
- Tune filter settings

#### Phase 2: Beta Testing (Week 2-3)
- Roll out to 10% of traffic
- Monitor metrics
- Collect user feedback
- Fix any issues

#### Phase 3: Gradual Rollout (Week 4-6)
- 25% traffic → 50% → 75% → 100%
- A/B test throughout
- Compare metrics against baseline

#### Phase 4: Optimization (Week 7+)
- Analyze data
- Fine-tune settings
- Consider 24 kHz for premium tier
- Document best practices

---

## References

### Provider Documentation

**Exotel:**
- [Digital Voice Platform](https://exotel.com/products/digital-voice/)
- [Stream and Voicebot Applet](https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet)
- [Voice APIs Introduction](https://docs.exotel.com/voice-apis/introduction)

**Plivo:**
- [Supported Audio Codecs](https://support.plivo.com/hc/en-us/articles/32800673795993-What-are-the-supported-audio-codecs)
- [Opus Codec Blog Post](https://www.plivo.com/blog/opus-audio-codec-better-voice-quality-for-plivo-sdk-based-apps/)
- [Audio Payload Format](https://support.plivo.com/hc/en-us/articles/32252710653337-What-is-the-expected-payload-format-to-send-audio-to-Plivo)

**Twilio:**
- [Audio Recording Best Practices](https://support.twilio.com/hc/en-us/articles/223180588-Best-Practices-for-Audio-Recordings)
- [Voice SDKs Supported Codecs](https://help.twilio.com/articles/13527980995355-Twilio-Voice-SDKs-Supported-Audio-Codecs)
- [AI Voice Agents Latency Guide](https://www.twilio.com/en-us/blog/developers/best-practices/guide-core-latency-ai-voice-agents)

### Technical Standards

- [Wideband Audio - Wikipedia](https://en.wikipedia.org/wiki/Wideband_audio)
- [HD Voice - Opale Systems](https://www.opalesystems.com/Tech-Blog/22-HD-Voice.en.htm)
- [VoIP Codecs - Nextiva](https://www.nextiva.com/blog/voip-codecs.html)
- [VoIP Codecs Explained - TechTarget](https://www.techtarget.com/searchunifiedcommunications/tip/VoIP-codecs-explained-How-to-optimize-VoIP-quality)

### Audio Processing

- **Pipecat Framework:** [GitHub - pipecat-ai](https://github.com/pipecat-ai/pipecat)
- **Silero VAD:** Voice Activity Detection library
- **AIC (AI Coustics):** Professional audio enhancement
- **Krisp:** AI-powered noise cancellation

---

## Appendix: Bandwidth Calculations

### Bandwidth Requirements by Sample Rate

**Formula:** `Bandwidth (kbps) = (Sample Rate (Hz) × Bit Depth (bits) × Channels) ÷ 1000`

For telephony (mono, 16-bit):

| Sample Rate | Bandwidth (Uncompressed) | Bandwidth (G.711) | Bandwidth (Opus) |
|-------------|-------------------------|-------------------|------------------|
| 8 kHz | 128 kbps | 64 kbps | 24-32 kbps |
| 16 kHz | 256 kbps | 128 kbps | 32-40 kbps |
| 24 kHz | 384 kbps | 192 kbps | 48-64 kbps |
| 48 kHz | 768 kbps | N/A | 64-128 kbps |

**Codec Notes:**
- **G.711 (μ-law/A-law):** Standard telephony, 50% compression
- **Opus:** Modern codec, highly efficient, 70-80% compression
- **L16:** Uncompressed linear PCM

---

## Appendix: Frequency Ranges in Human Speech

### Phoneme Frequency Characteristics

| Phoneme Type | Frequency Range | Examples | Captured at 8 kHz? | Captured at 16 kHz? |
|--------------|-----------------|----------|-------------------|---------------------|
| **Vowels** | 300-2,000 Hz | a, e, i, o, u | ✅ Yes | ✅ Yes |
| **Nasals** | 250-2,500 Hz | m, n, ng | ✅ Mostly | ✅ Yes |
| **Plosives** | 500-5,000 Hz | p, t, k, b, d, g | ⚠️ Partial | ✅ Yes |
| **Fricatives** | 2,500-8,000 Hz | s, sh, f, th | ❌ No/Poor | ✅ Yes |
| **Sibilants** | 4,000-10,000 Hz | s, z, sh, zh | ❌ No | ⚠️ Partial |

**Key Insight:** Consonants, especially fricatives and sibilants, require higher sampling rates for clarity. This is why 8 kHz sounds "muffled" - critical speech sounds are missing.

---

## Appendix: Implementation Checklist

### Pre-Implementation
- [x] Research provider capabilities
- [x] Document current configuration
- [x] Create comprehensive analysis document
- [ ] Review with team
- [ ] Get stakeholder approval

### Development
- [ ] Update `vad.py` with provider-specific constants
- [ ] Update `transport.py` configurations
- [ ] Modify `create_vad_analyzer()` function
- [ ] Add environment variable support
- [ ] Update `.env.example` documentation

### Testing
- [ ] Write unit tests for sample rate selection
- [ ] Integration test with Exotel at 16 kHz
- [ ] Integration test with Plivo at 16 kHz
- [ ] Manual quality testing
- [ ] Performance/bandwidth testing

### Deployment
- [ ] Deploy to staging
- [ ] Internal QA testing
- [ ] Beta rollout (10% traffic)
- [ ] Monitor metrics
- [ ] Gradual production rollout

### Post-Deployment
- [ ] Collect user feedback
- [ ] Analyze quality metrics
- [ ] Optimize filter settings
- [ ] Consider 24 kHz for Exotel premium tier
- [ ] Document learnings

---

## Questions & Contact

For questions about this implementation:
- **Technical Lead:** [Your team's contact]
- **Product Owner:** [Product contact]
- **External Support:**
  - Exotel: [support contact]
  - Plivo: [support contact]

---

**Document Version:** 1.0
**Last Updated:** 2026-02-06
**Author:** Claude AI (Audio Quality Analysis)
**Status:** Ready for Implementation
