#!/bin/bash

# Run both servers simultaneously for STT comparison
echo "🎯 Starting Dual STT Comparison Setup"
printf '=%.0s' {1..60}; echo
echo "🔊 Port 8000: Google Cloud Speech-to-Text"
echo "🔊 Port 8001: Speechmatics STT with Audio Filtering"
printf '=%.0s' {1..60}; echo

# Check dependencies
if ! command -v tmux &> /dev/null; then
    echo "❌ Error: tmux is required but not installed"
    echo "Install with: brew install tmux"
    exit 1
fi

if [ ! -f "./run_google_stt.sh" ] || [ ! -f "./run_speechmatics_stt.sh" ]; then
    echo "❌ Error: Required scripts not found in current directory"
    exit 1
fi

# Get current directory
CURRENT_DIR=$(pwd)

# Create tmux session with two panes
tmux new-session -d -s stt_comparison

# Split window vertically
tmux split-window -h -t stt_comparison

# Run Google STT server in left pane
tmux send-keys -t stt_comparison:0.0 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.0 './run_google_stt.sh' Enter

# Run Speechmatics STT server in right pane  
tmux send-keys -t stt_comparison:0.1 "cd '$CURRENT_DIR'" Enter
tmux send-keys -t stt_comparison:0.1 './run_speechmatics_stt.sh' Enter

# Set pane titles
tmux select-pane -t stt_comparison:0.0 -T "Google STT (8000)"
tmux select-pane -t stt_comparison:0.1 -T "Speechmatics STT (8001)"

echo ""
echo "✅ Both servers are starting..."
echo "📱 Google STT Server:      http://localhost:8000"
echo "📱 Speechmatics STT Server: http://localhost:8001"
echo ""
echo "🖥️  To view servers in tmux:"
echo "   tmux attach-session -t stt_comparison"
echo ""
echo "🛑 To stop both servers:"
echo "   tmux kill-session -t stt_comparison"
echo ""
echo "📊 Compare STT performance by testing both endpoints!"