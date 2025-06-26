# Use Python 3.11 slim image for better performance and security
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000

# Install system dependencies required for audio processing and compilation
# Removed 'git' and 'curl' as they are not required for the build
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libffi-dev \
    libssl-dev \
    pkg-config \
    portaudio19-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set proper permissions
RUN chmod +x run.py

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE ${PORT}

# Run the application
CMD ["python", "run.py"]