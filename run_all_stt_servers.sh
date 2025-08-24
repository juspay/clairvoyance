#!/bin/bash

# Run all STT servers simultaneously for comprehensive comparison
echo "🎯 Starting All STT Services Comparison Setup"
printf '=%.0s' {1..70}; echo
echo "🔊 Port 8000: Google Cloud Speech-to-Text (Enhanced)"
echo "🔊 Port 8001: AssemblyAI STT (Real-time Optimized)"
echo "🔊 Port 8002: OpenAI Whisper STT (Business Focused)"
echo "🔊 Port 8003: Speechmatics STT (Advanced Features)"
printf '=%.0s' {1..70}; echo

# Check dependencies
if ! command -v tmux &> /dev/null; then
    echo "❌ Error: tmux is required but not installed"
    echo "Install with: brew install tmux"
    exit 1
fi

# Check for required scripts
REQUIRED_SCRIPTS=("run_google_stt.sh" "run_assemblyai_stt.sh" "run_openai_stt.sh" "run_speechmatics_stt.sh")
for script in "${REQUIRED_SCRIPTS[@]}"; do
    if [ ! -f "./$script" ]; then
        echo "❌ Error: Required script $script not found in current directory"
        exit 1
    fi
done

# Get current directory
CURRENT_DIR=$(pwd)

# Create tmux session with 4 panes layout (2x2 grid)
tmux new-session -d -s stt_comparison

# Create 4 panes in a 2x2 grid
tmux split-window -h -t stt_comparison       # Split horizontally (left/right)
tmux split-window -v -t stt_comparison:0.0   # Split left pane vertically
tmux split-window -v -t stt_comparison:0.1   # Split right pane vertically

# Configure each pane with STT servers
# Top-left: Google STT (8000)
tmux send-keys -t stt_comparison:0.0 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.0 './run_google_stt.sh' Enter

# Bottom-left: AssemblyAI STT (8001)
tmux send-keys -t stt_comparison:0.2 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.2 './run_assemblyai_stt.sh' Enter

# Top-right: OpenAI STT (8002)
tmux send-keys -t stt_comparison:0.1 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.1 './run_openai_stt.sh' Enter

# Bottom-right: Speechmatics STT (8003)
tmux send-keys -t stt_comparison:0.3 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.3 './run_speechmatics_stt.sh' Enter

# Set pane titles for easy identification
tmux select-pane -t stt_comparison:0.0 -T "Google STT (8000)"
tmux select-pane -t stt_comparison:0.1 -T "OpenAI STT (8002)"
tmux select-pane -t stt_comparison:0.2 -T "AssemblyAI STT (8001)"
tmux select-pane -t stt_comparison:0.3 -T "Speechmatics STT (8003)"

echo ""
echo "✅ All STT servers are starting..."
echo ""
echo "📱 Server Endpoints:"
echo "   Google STT:       http://localhost:8000  (Enhanced accuracy & features)"
echo "   AssemblyAI STT:   http://localhost:8001  (Real-time optimized)"
echo "   OpenAI STT:       http://localhost:8002  (Business terminology focus)"
echo "   Speechmatics STT: http://localhost:8003  (Advanced diarization & vocab)"
echo ""
echo "🖥️  To view all servers in tmux:"
echo "   tmux attach-session -t stt_comparison"
echo ""
echo "🔄 To switch between panes in tmux:"
echo "   Ctrl+b + arrow keys"
echo ""
echo "🛑 To stop all servers:"
echo "   tmux kill-session -t stt_comparison"
echo ""
echo "📊 Compare STT performance across all 4 providers!"
echo "🎤 Test with different voices, accents, and audio quality"
echo "⚡ Measure latency, accuracy, and transcription quality"
echo "🧠 Compare: Google (accuracy) vs AssemblyAI (speed) vs OpenAI (context) vs Speechmatics (features)"