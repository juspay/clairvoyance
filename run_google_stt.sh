#!/bin/bash

# Run server with Google STT on port 8000
echo "🚀 Starting server with Google STT on port 8000..."
echo "📍 Configuration: USE_SPEECHMATICS=false"
echo "🔊 STT Provider: Google Cloud Speech-to-Text"
echo "🌐 Server URL: http://localhost:8000"
printf '=%.0s' {1..50}; echo

# Activate virtual environment
source myenv/bin/activate

# Load Google STT environment and start server
export $(cat .env.google_stt | grep -v '^#' | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload