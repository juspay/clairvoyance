# Voice Agent Reconnection - Frontend Implementation

## Overview
This guide implements a robust reconnection system where the frontend communicates with the main server to restart voice agent subprocesses while preserving conversation context.

## Architecture

### Current Flow
```
Frontend → HTTP Request → Main Server → New Subprocess → Daily.co
```

The main server stays alive and can always receive reconnection requests, then launches new subprocesses with restored session context.

## Implementation

### 1. Session Management

```javascript
// Global session variables
let currentSessionId = null;
let isReconnecting = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 3;

// Store session information
function storeSessionInfo(sessionId) {
    currentSessionId = sessionId;
    localStorage.setItem('currentSessionId', sessionId);
    localStorage.setItem('lastConnectionTime', Date.now().toString());
}

// Load session on page refresh
function loadSessionInfo() {
    currentSessionId = localStorage.getItem('currentSessionId');
    return currentSessionId;
}

// Clear session data
function clearSession() {
    currentSessionId = null;
    reconnectAttempts = 0;
    localStorage.removeItem('currentSessionId');
    localStorage.removeItem('lastConnectionTime');
}
```

### 2. WebSocket Message Handling

```javascript
ws.onmessage = (event) => {
    if (typeof event.data === "string") {
        try {
            const msg = JSON.parse(event.data);
            
            if (msg.type === "session-start") {
                // Store session ID when backend sends it
                console.log(`Session started: ${msg.payload.session_id}`);
                currentSessionId = msg.payload.session_id;
                storeSessionInfo(msg.payload.session_id);
                setStatus(`Connected as ${msg.payload.user_name}`);
                reconnectAttempts = 0; // Reset on successful connection
                
            } else if (msg.type === "session-disconnected") {
                // Handle backend-initiated disconnection
                console.log(`Session disconnected: ${msg.payload.reason} - ${msg.payload.message}`);
                
                if (msg.payload.reason === "idle_timeout") {
                    setStatus("Session timed out due to inactivity");
                    setTimeout(() => showDisconnectedState(), 1000);
                } else {
                    showDisconnectedState();
                }
                
            } else if (msg.type === "initialization_done") {
                isInitialized = true;
                setStatus("Microphone connected. You can speak now!");
                
            } // ... other message types
            
        } catch (e) {
            console.error("Error parsing message:", e);
        }
    }
    // ... handle binary data
};
```

### 3. Disconnected State UI

Add to your HTML:

```html
<!-- Disconnected state overlay -->
<div id="disconnected-state" class="disconnected-state" style="display: none;">
    <div class="disconnected-message">
        <h3>Connection Lost</h3>
        <p id="disconnect-reason">Your conversation is saved. Click reconnect to continue.</p>
        <button id="reconnect-btn" class="reconnect-button">
            <span id="reconnect-text">Reconnect</span>
        </button>
        <button id="new-session-btn" class="new-session-button" style="display: none;">
            Start New Session
        </button>
    </div>
</div>
```

Add CSS:

```css
.disconnected-state {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: rgba(0, 0, 0, 0.8);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
}

.disconnected-message {
    background-color: var(--surface-color);
    padding: 2rem;
    border-radius: 12px;
    text-align: center;
    max-width: 400px;
    box-shadow: var(--card-shadow);
}

.reconnect-button, .new-session-button {
    background-color: #fd8414;
    color: white;
    border: none;
    padding: 12px 24px;
    border-radius: 8px;
    font-size: 1rem;
    cursor: pointer;
    margin: 0.5rem;
    min-width: 150px;
}

.reconnect-button:hover, .new-session-button:hover {
    background-color: #e6750f;
}

.reconnect-button:disabled {
    background-color: #666;
    cursor: not-allowed;
}

.new-session-button {
    background-color: #666;
}
```

### 4. Disconnection Detection & UI Management

```javascript
function showDisconnectedState() {
    setStatus("Disconnected");
    document.getElementById("disconnected-state").style.display = "flex";
    
    // Hide call controls but keep messages visible
    document.getElementById("call-active-controls").style.display = "none";
    document.getElementById("call-inactive-controls").style.display = "none";
    
    // Show appropriate buttons based on session availability
    if (currentSessionId && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        document.getElementById("reconnect-btn").style.display = "inline-block";
        document.getElementById("new-session-btn").style.display = "none";
    } else {
        document.getElementById("reconnect-btn").style.display = "none";
        document.getElementById("new-session-btn").style.display = "inline-block";
        document.getElementById("disconnect-reason").textContent = 
            "Session cannot be restored. Start a new conversation.";
    }
}

function hideDisconnectedState() {
    document.getElementById("disconnected-state").style.display = "none";
}
```

### 5. Reconnection Logic

```javascript
async function reconnect() {
    if (!currentSessionId) {
        console.error("No session ID available for reconnection");
        startNewSession();
        return;
    }
    
    if (isReconnecting) {
        console.log("Reconnection already in progress");
        return;
    }
    
    isReconnecting = true;
    reconnectAttempts++;
    
    // Update UI
    const reconnectBtn = document.getElementById("reconnect-btn");
    const reconnectText = document.getElementById("reconnect-text");
    reconnectBtn.disabled = true;
    reconnectText.textContent = "Reconnecting...";
    
    try {
        setStatus("Attempting to reconnect...");
        
        // Make reconnection request to main server
        const response = await fetch('/agent/voice/automatic/reconnect', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                sessionId: currentSessionId,
                userName: userName || "guest",
                mode: userMode || "LIVE",
                // Add other parameters as needed from original connection
            }),
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const data = await response.json();
        
        if (data.error) {
            throw new Error(data.message || data.error);
        }
        
        console.log(`Reconnection successful: ${data.message}`);
        console.log(`Restoring ${data.conversation_turns} conversation turns`);
        
        hideDisconnectedState();
        
        // Connect to new Daily.co room
        await connectToDaily(data.room_url, data.token);
        
        setStatus("Reconnected successfully!");
        
    } catch (error) {
        console.error("Reconnection failed:", error);
        
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            setStatus("Reconnection failed. Please start a new session.");
            document.getElementById("reconnect-btn").style.display = "none";
            document.getElementById("new-session-btn").style.display = "inline-block";
            document.getElementById("disconnect-reason").textContent = 
                "Multiple reconnection attempts failed. Please start a new session.";
        } else {
            setStatus(`Reconnection failed. ${MAX_RECONNECT_ATTEMPTS - reconnectAttempts} attempts remaining.`);
            // Re-enable reconnect button for retry
            reconnectBtn.disabled = false;
            reconnectText.textContent = "Reconnect";
        }
        
    } finally {
        isReconnecting = false;
    }
}

async function startNewSession() {
    clearSession();
    hideDisconnectedState();
    
    // Clear existing messages
    const messages = chatContainer.querySelectorAll('.message');
    messages.forEach(msg => msg.remove());
    
    // Show welcome state
    document.getElementById("welcome-state").style.display = "flex";
    
    // Start fresh connection
    await start();
}
```

### 6. Daily.co Connection

```javascript
async function connectToDaily(roomUrl, token) {
    // Store the new connection details
    currentRoomUrl = roomUrl;
    currentToken = token;
    
    // Initialize WebSocket with potential session restoration
    const wsUrl = `ws://localhost:8000/ws/live?token=${encodeURIComponent(userToken)}`;
    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    
    // Set up WebSocket handlers
    ws.onopen = () => {
        setStatus("Initializing...");
        isConnecting = false;
        lastPongTime = Date.now();
        
        // Start ping interval
        if (pingInterval) clearInterval(pingInterval);
        pingInterval = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                try {
                    ws.send(JSON.stringify({ type: "ping" }));
                    
                    if (Date.now() - lastPongTime > PING_INTERVAL * 3) {
                        console.warn("No pong received, connection may be lost");
                        ws.close();
                    }
                } catch (e) {
                    console.error("Error sending ping:", e);
                }
            }
        }, PING_INTERVAL);
        
        setupAudio();
    };
    
    ws.onclose = () => {
        isConnecting = false;
        if (pingInterval) clearInterval(pingInterval);
        
        // Only show disconnected state if this wasn't an intentional close
        if (isActiveSession && !isReconnecting) {
            showDisconnectedState();
        }
    };
    
    ws.onerror = (err) => {
        console.error("WebSocket error:", err);
        isConnecting = false;
    };
    
    ws.onmessage = /* your existing message handler */;
}
```

### 7. Event Listeners

```javascript
document.addEventListener('DOMContentLoaded', function() {
    // Load existing session on page refresh
    loadSessionInfo();
    
    // Add event listeners
    document.getElementById("reconnect-btn").addEventListener('click', reconnect);
    document.getElementById("new-session-btn").addEventListener('click', startNewSession);
    
    // Initialize UI based on stored session
    if (currentSessionId) {
        console.log(`Found stored session: ${currentSessionId}`);
    }
});
```

## Testing

### Automatic Idle Timeout Test (10 seconds)
1. Start a conversation
2. Stop talking for 10+ seconds
3. Backend sends `session-disconnected` event
4. Frontend shows reconnect UI
5. Click reconnect
6. Verify conversation continues seamlessly

### Manual Disconnect Test
1. Start conversation
2. Simulate network failure (disable network in dev tools)
3. Frontend detects WebSocket close
4. Shows reconnect UI
5. Re-enable network and click reconnect

### Session Not Found Test
1. Clear backend session data
2. Attempt reconnection
3. Should show "Start New Session" button
4. Gracefully fall back to fresh session

## Key Features

- **Persistent Session ID**: Stored in localStorage across page refreshes
- **Automatic Reconnection**: Triggered by idle timeout or connection loss
- **Retry Logic**: Up to 3 reconnection attempts with exponential backoff
- **Graceful Fallbacks**: Falls back to new session if reconnection fails
- **User Feedback**: Clear status messages and button states
- **Conversation Preservation**: Frontend messages remain visible during reconnection
- **Error Handling**: Comprehensive error handling with user-friendly messages

This implementation provides a robust reconnection experience where users can seamlessly continue their conversations after disconnections.