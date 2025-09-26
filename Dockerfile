# Use Python 3.11 slim image for better performance and security
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    NLTK_DATA=/usr/local/nltk_data

# Install system dependencies required for audio processing and compilation
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libffi-dev \
    libssl-dev \
    pkg-config \
    portaudio19-dev \
    python3-dev \
    libsndfile1-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create NLTK data directory and download required data
RUN pip install --no-cache-dir nltk && \
    mkdir -p /usr/local/nltk_data && \
    python -m nltk.downloader punkt punkt_tab -d /usr/local/nltk_data

# Accept HF_TOKEN as build argument for pyannote models
ARG HF_TOKEN
ENV HF_TOKEN=${HF_TOKEN}

# Pre-download SpeechBrain and Pyannote models to package them in the image
RUN mkdir -p /app/pretrained_models /app/.cache/huggingface && \
    python -c "\
import os; \
try: \
    from speechbrain.pretrained import EncoderClassifier; \
    print('Downloading SpeechBrain ECAPA-TDNN model...'); \
    model = EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', savedir='/app/pretrained_models/spkrec-ecapa-voxceleb'); \
    print('✅ SpeechBrain model downloaded and packaged'); \
except Exception as e: \
    print(f'⚠️ SpeechBrain model download failed: {e}'); \
\
hf_token = os.environ.get('HF_TOKEN'); \
if hf_token: \
    try: \
        from pyannote.audio import Pipeline; \
        print('Downloading Pyannote speaker diarization model...'); \
        pipeline = Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', use_auth_token=hf_token); \
        print('✅ Pyannote model downloaded and packaged'); \
    except Exception as e: \
        print(f'⚠️ Pyannote model download failed: {e}'); \
        print('Pyannote will be downloaded at runtime instead'); \
else: \
    print('⚠️ No HF_TOKEN provided, Pyannote models will be downloaded at runtime'); \
"

# Copy application code
COPY . .

# Set proper permissions
RUN chmod +x run.py

# Create directories for embeddings and debug audio (if needed)
RUN mkdir -p /app/embeddings /app/debug_audio

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app && \
    chown -R appuser:appuser /usr/local/nltk_data && \
    chown -R appuser:appuser /app/pretrained_models && \
    chown -R appuser:appuser /app/.cache/huggingface && \
    chown -R appuser:appuser /app/embeddings && \
    chown -R appuser:appuser /app/debug_audio
USER appuser

# Expose port
EXPOSE ${PORT}

# Run the application
CMD ["python", "run.py"]
