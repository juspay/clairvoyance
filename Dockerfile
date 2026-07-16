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
    libopus0 \
    libvpx7 \
    && rm -rf /var/lib/apt/lists/*
# libopus0/libvpx7: audio+video codecs for aiortc (SmallWebRTC transport).
# aiortc's manylinux wheels bundle codecs and ffmpeg already pulls these in
# transitively, so this is explicit insurance in case ffmpeg is dropped later.


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
RUN uv sync --frozen --no-dev --no-install-project && \
    uv pip show pipecat-ai

# Download AIC assets from GCP Storage using authenticated context
ARG AIC_BUCKET_PATH=gs://breeze-clairvoyance-models/aic

# Install Google Cloud CLI and download AIC files (only for GCP deployments)
# Use BuildKit secret mount to avoid leaking token in image layers
RUN --mount=type=secret,id=gcp_token \
    if [ -f /run/secrets/gcp_token ]; then \
        echo "=== Installing Google Cloud CLI for model assets ===" && \
        curl -sSL https://sdk.cloud.google.com | bash && \
        export PATH=$PATH:/root/google-cloud-sdk/bin && \
        echo "=== Downloading AIC assets ===" && \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_l_8khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_l_8khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_l_16khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_l_16khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_vf_2_1_l_16khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_vf_2_1_l_16khz.aicmodel"; \
    else \
        echo "Warning: GCP token secret not provided, skipping AIC installation (AWS deployment)"; \
    fi

# Create NLTK data directory and download required data
RUN mkdir -p /usr/local/nltk_data && \
    uv run python -m nltk.downloader punkt punkt_tab -d /usr/local/nltk_data

# Copy application code
COPY . .

# Set proper permissions
RUN chmod +x run.py

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN mkdir -p /app/.uv-cache && \
    chown -R appuser:appuser /app && \
    chown -R appuser:appuser /usr/local/nltk_data
USER appuser

# Expose port
EXPOSE ${PORT}

# Run the application
CMD ["uv", "run", "python", "run.py"]
