#!/bin/bash

# Run server with OpenAI STT on port 8002
echo "🚀 Starting server with OpenAI STT on port 8002..."
echo "📍 Configuration: STT_PROVIDER=openai"
echo "🔊 STT Provider: OpenAI Whisper with business prompt"
echo "🌐 Server URL: http://localhost:8002"
printf '=%.0s' {1..50}; echo

# Activate virtual environment
source venv/bin/activate

# Load base environment first, then override with OpenAI STT config
set -a  # Enable automatic export of variables
source .env
source .env.openai_stt
set +a  # Disable automatic export

# Verify STT provider is set correctly
echo "🔍 Environment check: STT_PROVIDER=$STT_PROVIDER, PORT=$PORT"
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload