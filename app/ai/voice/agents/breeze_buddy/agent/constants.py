"""Constants for Breeze Buddy voice agent configuration."""

# Audio Sample Rates
# ===================
# Provider-specific sample rates for optimal audio quality
# See docs/AUDIO_SAMPLING_RATE_ANALYSIS.md for detailed analysis

# Legacy/fallback telephony rate
TELEPHONY_SAMPLE_RATE = 8000

# Provider-specific sample rates based on capability analysis:
# - Exotel: Supports 8/16/24 kHz. Using 16 kHz for 2x better quality (wideband)
# - Plivo: Supports 8/16 kHz. Using 16 kHz for 2x better quality (wideband)
# - Twilio: Limited to 8 kHz by network transcoding
# - Telnyx: Unknown capabilities, using conservative 8 kHz
# - Daily: WebRTC capable, using 16 kHz (can go up to 48 kHz)

EXOTEL_SAMPLE_RATE = 16000  # Wideband (supports up to 24 kHz for HD)
PLIVO_SAMPLE_RATE = 16000  # Wideband (maximum supported)
TWILIO_SAMPLE_RATE = 8000  # Narrowband (network limitation)
TELNYX_SAMPLE_RATE = 8000  # Narrowband (capabilities unknown)
DAILY_SAMPLE_RATE = 16000  # Wideband WebRTC

# Provider sample rate lookup dictionary for efficient mapping
PROVIDER_SAMPLE_RATES = {
    "exotel": EXOTEL_SAMPLE_RATE,
    "plivo": PLIVO_SAMPLE_RATE,
    "twilio": TWILIO_SAMPLE_RATE,
    "telnyx": TELNYX_SAMPLE_RATE,
}
