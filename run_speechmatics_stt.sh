#!/bin/bash

# Run server with Speechmatics STT on port 8003
echo "🚀 Starting server with Speechmatics STT on port 8003..."
echo "📍 Configuration: STT_PROVIDER=speechmatics"
echo "🔊 STT Provider: Speechmatics with Enhanced Real-time Features"
echo "🎯 Features: Speaker diarization, Business vocabulary, Adaptive turn detection"
echo "🌐 Server URL: http://localhost:8003"
printf '=%.0s' {1..60}; echo

# Activate virtual environment
source venv/bin/activate

# Load base environment first, then override with Speechmatics STT config
set -a  # Enable automatic export of variables
source .env
source .env.speechmatics_stt
set +a  # Disable automatic export

# Verify STT provider is set correctly
echo "🔍 Environment check: STT_PROVIDER=$STT_PROVIDER, PORT=$PORT"
uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload