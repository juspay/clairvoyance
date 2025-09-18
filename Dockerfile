# Use Python 3.11 slim image for better performance and security
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    NLTK_DATA=/usr/local/nltk_data\
    KRISP_MODEL_PATH=/app/models/voice/krisp/krisp-viva-tel-v2.kef

# Install system dependencies required for audio processing and compilation + curl for GCP CLI
# Added cmake for Krisp native component compilation, unzip for manual wheel extraction
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    ffmpeg \
    libffi-dev \
    libssl-dev \
    pkg-config \
    portaudio19-dev \
    python3-dev \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Google Cloud CLI for downloading Krisp assets                                                                │ │
RUN curl -sSL https://sdk.cloud.google.com | bash
ENV PATH $PATH:/root/google-cloud-sdk/bin

# Create app and krisp directory
WORKDIR /app
RUN mkdir -p /app/models/voice/krisp

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies (without Krisp first)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Download Krisp assets from GCP Storage using authenticated context
ARG KRISP_BUCKET_PATH=gs://clairvoyance-models/krisp
ARG GCP_ACCESS_TOKEN

# Authenticate gcloud with access token and download Krisp files
RUN if [ -n "$GCP_ACCESS_TOKEN" ]; then \
        echo "$GCP_ACCESS_TOKEN" > /tmp/token.txt && \
        gcloud storage cp --access-token-file=/tmp/token.txt ${KRISP_BUCKET_PATH}/krisp-viva-tel-v2.kef /app/models/voice/krisp/ && \
        gcloud storage cp --access-token-file=/tmp/token.txt ${KRISP_BUCKET_PATH}/*linux_x86_64.whl /tmp/ && \
        gcloud storage cp --access-token-file=/tmp/token.txt ${KRISP_BUCKET_PATH}/*linux_aarch64.whl /tmp/ && \
        rm /tmp/token.txt; \
    else \
        echo "Warning: GCP_ACCESS_TOKEN not provided, skipping Krisp model download"; \
    fi

# Install Krisp wheel package (if downloaded) - auto-detect architecture
RUN if ls /tmp/*linux_*.whl 1> /dev/null 2>&1; then \
        ARCH=$(python -c "import platform; print(platform.machine())") && \
        echo "=== Platform Debug Info ===" && \
        echo "Detected architecture: $ARCH" && \
        if [ "$ARCH" = "x86_64" ]; then \
            WHEEL_FILE="/tmp/*linux_x86_64.whl"; \
        elif [ "$ARCH" = "aarch64" ]; then \
            WHEEL_FILE="/tmp/*linux_aarch64.whl"; \
        else \
            echo "Unsupported architecture: $ARCH" && exit 1; \
        fi && \
        echo "Using wheel file: $WHEEL_FILE" && \
        if ls $WHEEL_FILE 1> /dev/null 2>&1; then \
            echo "=== Attempting pip install ===" && \
            pip install -v $WHEEL_FILE && \
            echo "Krisp audio package installed successfully"; \
        else \
            echo "Warning: No wheel file found for architecture $ARCH"; \
        fi; \
    else \
        echo "Warning: No Krisp wheel files found, skipping installation"; \
    fi

# Verify krisp installation immediately after install
RUN echo "=== Verifying Krisp Installation ===" && \
    python -c "\
import sys; \
print('Python version:', sys.version); \
print('Python executable:', sys.executable); \
print('Python path:', sys.path); \
try: \
    import krisp_audio; \
    print('✓ Krisp import: SUCCESS'); \
    print('Krisp version:', krisp_audio.getVersion() if hasattr(krisp_audio, 'getVersion') else 'Version method not found'); \
except Exception as e: \
    print('✗ Krisp import: FAILED'); \
    print('Error:', str(e)); \
    print('Error type:', type(e).__name__); \
    import traceback; \
    traceback.print_exc(); \
" && echo "=== Krisp Verification Complete ==="

# Create NLTK data directory and download required data
RUN pip install --no-cache-dir nltk && \
    mkdir -p /usr/local/nltk_data && \
    python -m nltk.downloader punkt punkt_tab -d /usr/local/nltk_data

# Copy application code
COPY . .

# Set proper permissions
RUN chmod +x run.py

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app && \
    chown -R appuser:appuser /usr/local/nltk_data
USER appuser

# Expose port
EXPOSE ${PORT}

# Run the application
CMD ["python", "run.py"]
