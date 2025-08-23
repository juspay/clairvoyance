#!/bin/bash

# Run server with Speechmatics STT on port 8001
echo "🚀 Starting server with Speechmatics STT on port 8001..."
echo "📍 Configuration: USE_SPEECHMATICS=true"
echo "🔊 STT Provider: Speechmatics with Audio Filtering (threshold: 3.4)"
echo "🌐 Server URL: http://localhost:8001"
echo "🎯 Features: Background speech removal, Volume labeling, Enhanced operating point"
printf '=%.0s' {1..50}; echo

# Activate virtual environment
source myenv/bin/activate

# Load Speechmatics STT environment and start server
export $(cat .env.speechmatics_stt | grep -v '^#' | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload