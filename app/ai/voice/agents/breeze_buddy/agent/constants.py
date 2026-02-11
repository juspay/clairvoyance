"""Constants for Breeze Buddy voice agent configuration."""

# Audio Sample Rates
# ===================
# Provider-specific sample rates for optimal audio quality
# See docs/AUDIO_SAMPLING_RATE_ANALYSIS.md for detailed analysis

# Bandwidth-based sample rates (used with CallBandwidth configuration)
SAMPLE_RATE_LOW = 8000  # 8 kHz narrowband - compatible with all providers
SAMPLE_RATE_HIGH = 16000  # 16 kHz wideband - better quality, requires provider support

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
    "daily": DAILY_SAMPLE_RATE,
}

# Providers that support high bandwidth (16 kHz)
# Twilio is always 8 kHz due to network limitations
PROVIDERS_SUPPORTING_HIGH_BANDWIDTH = {"exotel", "plivo"}


def get_sample_rate_for_provider(provider: str, bandwidth: str = "low") -> int:
    """Get the appropriate sample rate for a provider based on bandwidth setting.

    Args:
        provider: The telephony provider name (e.g., "exotel", "plivo", "twilio")
        bandwidth: The bandwidth setting ("low" or "high")

    Returns:
        Sample rate in Hz (8000 or 16000)
    """
    # Twilio is always limited to 8 kHz regardless of bandwidth setting
    if provider == "twilio":
        return SAMPLE_RATE_LOW

    # Telnyx capabilities unknown, use conservative 8 kHz
    if provider == "telnyx":
        return SAMPLE_RATE_LOW

    # For providers that support high bandwidth, use the configured setting
    if provider in PROVIDERS_SUPPORTING_HIGH_BANDWIDTH:
        return SAMPLE_RATE_HIGH if bandwidth == "high" else SAMPLE_RATE_LOW

    # Default to low bandwidth for unknown providers
    return SAMPLE_RATE_LOW
