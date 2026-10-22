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
    AIC_MODEL_PATH_S=/app/models/voice/aic/quail_s_8khz.aicmodel \
    AIC_MODEL_PATH_S_16KHZ=/app/models/voice/aic/quail_s_16khz.aicmodel \
    BREEZE_BUDDY_NOISE_MODEL_DIR=/app/models/voice/buddy \
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
RUN mkdir -p /app/models/voice/aic /app/models/voice/buddy

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first for better Docker layer caching
COPY pyproject.toml uv.lock ./

# Install Python dependencies using uv
# Use --no-install-project to avoid installing the app/ package at this stage
# This allows optimal Docker layer caching - dependencies layer is cached separately
# --compile-bytecode: write .pyc at BUILD time. Without it every bot subprocess
# recompiles ~12k source files on each call (PYTHONDONTWRITEBYTECODE=1 means the
# result is never cached). Measured 1.4-1.8x faster bot startup under CPU pressure.
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode && \
    uv pip show pipecat-ai

# Download AIC assets from GCP Storage using authenticated context
ARG AIC_BUCKET_PATH=gs://breeze-clairvoyance-models/aic
# Buddy noise filter models: Ceva DPDFNet (Apache-2.0; docs/NOISE_FILTER.md),
# mirrored unmodified; a missing or mismatched file is a build warning, as for AIC.
ARG DPDFNET_BUCKET_PATH=gs://breeze-clairvoyance-models/dpdfnet

# Install Google Cloud CLI and download model files (only for GCP deployments)
# Use BuildKit secret mount to avoid leaking token in image layers
RUN --mount=type=secret,id=gcp_token \
    if [ -f /run/secrets/gcp_token ]; then \
        echo "=== Installing Google Cloud CLI for model assets ===" && \
        curl -sSL https://sdk.cloud.google.com | bash && \
        export PATH=$PATH:/root/google-cloud-sdk/bin && \
        echo "=== Downloading Buddy noise filter models ===" && \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${DPDFNET_BUCKET_PATH}/dpdfnet2_8khz.onnx /app/models/voice/buddy/ || echo "Warning: Failed to download dpdfnet2_8khz.onnx"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${DPDFNET_BUCKET_PATH}/dpdfnet_baseline_16khz.onnx /app/models/voice/buddy/ || echo "Warning: Failed to download dpdfnet_baseline_16khz.onnx"; \
        printf '%s\n' \
            "6218f1dbd6e4bac5768c63b7d899fe7b84b3788f2a35c4e246d4ab0946165c5d  /app/models/voice/buddy/dpdfnet2_8khz.onnx" \
            "371d26182aff0e1e0d31354e24c81f79cd57458f5a7dd003fc10a7ebf64255e0  /app/models/voice/buddy/dpdfnet_baseline_16khz.onnx" \
            | sha256sum -c - || echo "Warning: Buddy noise filter models do not match their pinned checksums"; \
        echo "=== Downloading AIC assets ===" && \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_l_8khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_l_8khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_l_16khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_l_16khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_vf_2_1_l_16khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_vf_2_1_l_16khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_s_8khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_s_8khz.aicmodel"; \
        gcloud storage cp --access-token-file=/run/secrets/gcp_token ${AIC_BUCKET_PATH}/quail_s_16khz.aicmodel /app/models/voice/aic/ || echo "Warning: Failed to download quail_s_16khz.aicmodel"; \
    else \
        echo "Warning: GCP token secret not provided, skipping AIC and Buddy noise filter models (AWS deployment)"; \
    fi

# Create NLTK data directory and download required data
RUN mkdir -p /usr/local/nltk_data && \
    uv run python -m nltk.downloader punkt punkt_tab -d /usr/local/nltk_data

# Copy application code
COPY . .

# Precompile the app's own bytecode too (same reason as --compile-bytecode above).
RUN /app/.venv/bin/python -m compileall -q -j 0 /app/app || true

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
