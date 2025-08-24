#!/bin/bash

# Run server with Google STT on port 8000
echo "🚀 Starting server with Google STT on port 8000..."
echo "📍 Configuration: STT_PROVIDER=google"
echo "🔊 STT Provider: Google Cloud Speech-to-Text"
echo "🌐 Server URL: http://localhost:8000"
printf '=%.0s' {1..50}; echo

# Activate virtual environment
source venv/bin/activate

# Load base environment first, then override with Google STT config
set -a  # Enable automatic export of variables
source .env
source .env.google_stt
set +a  # Disable automatic export

# Verify STT provider is set correctly
echo "🔍 Environment check: STT_PROVIDER=$STT_PROVIDER, PORT=$PORT"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload