#!/bin/bash

# Run server with AssemblyAI STT on port 8001
echo "🚀 Starting server with AssemblyAI STT on port 8001..."
echo "📍 Configuration: STT_PROVIDER=assemblyai"
echo "🔊 STT Provider: AssemblyAI with Silero VAD"
echo "🌐 Server URL: http://localhost:8001"
printf '=%.0s' {1..50}; echo

# Activate virtual environment
source venv/bin/activate

# Load base environment first, then override with AssemblyAI STT config
set -a  # Enable automatic export of variables
source .env
source .env.assemblyai_stt
set +a  # Disable automatic export

# Verify STT provider is set correctly
echo "🔍 Environment check: STT_PROVIDER=$STT_PROVIDER, PORT=$PORT"
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload