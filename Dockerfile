# Use Python 3.11 slim image for better performance and security
FROM python:3.11-slim

# Standard Quail ships native 8 kHz and 16 kHz artifacts. Voice Focus ships
# only a 16 kHz artifact; the AIC SDK resamples 8 kHz telephony input internally.
# Do not add a quail_vf_*_8khz path: ai-coustics does not publish that model.
# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    NLTK_DATA=/usr/local/nltk_data\
    AIC_MODEL_PATH=/app/models/voice/aic/quail_l_8khz.aicmodel \
    AIC_MODEL_PATH_16KHZ=/app/models/voice/aic/quail_l_16khz.aicmodel \
    AIC_VOICE_FOCUS_MODEL_PATH=/app/models/voice/aic/quail_vf_2_1_l_16khz.aicmodel \
    UV_CACHE_DIR=/app/.uv-cache

# Install system dependencies required for audio processing and compilation + curl for GCP CLI
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libffi-dev \
    libssl-dev \
    pkg-config \
    portaudio19-dev \
    python3-dev \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*


# Create app and model directories
WORKDIR /app
RUN mkdir -p /app/models/voice/aic

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first for better Docker layer caching
COPY pyproject.toml uv.lock ./

# Install Python dependencies using uv
# Use --no-install-project to avoid installing the app/ package at this stage
# This allows optimal Docker layer caching - dependencies layer is cached separately
# The uv cache (~861MB) is deleted in the SAME layer that creates it. Removing
# it in a later layer would not help: layers are additive, so the bytes would
# still ship inside this layer with only a whiteout on top.
RUN uv sync --frozen --no-dev --no-install-project && \
    uv pip show pipecat-ai && \
    rm -rf /app/.uv-cache

# Download AIC assets from GCP Storage using authenticated context
ARG AIC_BUCKET_PATH=gs://breeze-clairvoyance-models/aic

# Download AIC files using plain curl + Bearer token against GCS JSON API.
# Replaces installing google-cloud-sdk (~845MB unpacked, ~170MB compressed).
# Uses the same BuildKit secret (gcp_token) as before — same auth contract.
# curl is already installed at the apt step above, so NOTHING extra is added.
RUN --mount=type=secret,id=gcp_token \
    if [ -f /run/secrets/gcp_token ]; then \
        echo "=== Downloading AIC assets ===" && \
        TOKEN=$(cat /run/secrets/gcp_token) && \
        GCS_PATH="${AIC_BUCKET_PATH#gs://}" && \
        for m in quail_l_8khz.aicmodel quail_l_16khz.aicmodel quail_vf_2_1_l_16khz.aicmodel; do \
            echo "Fetching ${m}"; \
            curl -fsSL \
                -H "Authorization: Bearer ${TOKEN}" \
                -o "/app/models/voice/aic/${m}" \
                "https://storage.googleapis.com/${GCS_PATH}/${m}" \
            || echo "Warning: Failed to download ${m}"; \
        done; \
    else \
        echo "Warning: GCP token secret not provided, skipping AIC installation (AWS deployment)"; \
    fi

# Create non-root user BEFORE big COPY so --chown can set ownership inline.
# Avoids a `chown -R /app` layer that duplicates every file (~1.8GB unpacked,
# ~500MB compressed). Venv (.venv) stays root-owned but world-readable —
# appuser only needs read+execute on it.
# Create user + writable dirs before big COPY. .uv-cache already has content from
# the earlier `uv sync` (sdists/wheels/, few MB) — recursive chown on this TINY
# dir only, NOT on all of /app (which would duplicate every file in image).
RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    mkdir -p /app/.uv-cache /usr/local/nltk_data && \
    chown -R appuser:appuser /app/.uv-cache /usr/local/nltk_data

# Download NLTK data (as root; chown after — NLTK data is small ~6MB so the
# chown layer is a one-time few-KB cost).
# This is the last build-time uv invocation, so drop the cache it recreates —
# again in the same layer, for the reason noted above.
RUN uv run --no-sync python -m nltk.downloader punkt punkt_tab -d /usr/local/nltk_data && \
    chown -R appuser:appuser /usr/local/nltk_data && \
    rm -rf /app/.uv-cache

# Copy application code with ownership set inline (no chown -R layer)
COPY --chown=appuser:appuser . .

# An empty, appuser-owned cache dir. `uv run` initialises a cache even with
# --no-sync, and it cannot create this itself because /app is root-owned.
RUN mkdir -p /app/.uv-cache && chown appuser:appuser /app/.uv-cache
USER appuser

# Expose port
EXPOSE ${PORT}

# --no-sync is load-bearing. Plain `uv run` re-resolves the project on every
# container start: it rebuilds the clairvoyance wheel and re-adds the dev
# dependency group that `uv sync --no-dev` deliberately left out, then fails
# writing into the root-owned /app/.venv:
#   Failed to create directory `/app/.venv/.../iniconfig`: Permission denied
# --no-sync runs the already-built environment as-is, which is what makes the
# image immutable and lets the venv stay root-owned (no chown -R needed).
CMD ["uv", "run", "--no-sync", "python", "run.py"]
