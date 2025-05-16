# Breeze Automatic - Gemini Live Proxy Server

## 1. Overview

Breeze Automatic is a FastAPI-based proxy server designed to connect to the Google Gemini Live API. It facilitates real-time, bidirectional communication with the Gemini model, enabling features like audio streaming, voice activity detection (VAD), and dynamic function calling. The server presents itself as 'Breeze Automatic', a D2C business assistant.

This project provides a robust backend for client applications (like the included [`static/client.html`](static/client.html:1)) to interact seamlessly with Gemini's advanced conversational AI capabilities.

## 2. Key Features

*   **Real-time Audio Streaming:** Bidirectional audio streaming to and from the Gemini API.
*   **WebSocket Endpoint:** Secure WebSocket (`/ws/live`) for client communication.
*   **Gemini Live API Integration:** Utilizes models like `gemini-2.0-flash-live-001`.
*   **Scalable Function Calling:** Supports dynamic function calling with tools organized by providers (e.g., `system` tools like `getCurrentTime`, `juspay` analytics tools). The system is designed for easy addition of new tool providers.
*   **Voice Activity Detection (VAD):** Manages speech input effectively.
*   **CORS Enabled:** Allows broad client accessibility.
*   **Environment-Driven Configuration:** API keys, model names, and other settings are managed via environment variables.
*   **Health Check:** Includes a `/health` endpoint for monitoring.
*   **Graceful Shutdown:** Ensures clean termination of connections.
*   **Modular & Scalable Architecture:** Built with a clean, maintainable, and extensible project structure.

## 3. Project Structure

The project follows a modular structure within the `app/` directory:

```
.
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, routing, lifecycle
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py       # Configuration and settings
│   ├── services/
│   │   ├── __init__.py
│   │   └── gemini_service.py # Gemini API interaction logic
│   ├── tools/
│   │   ├── __init__.py     # Tool aggregation
│   │   └── providers/      # Tool provider modules
│   │       ├── __init__.py
│   │       ├── system/     # System-level utility tools
│   │       │   ├── __init__.py
│   │       │   └── system_tools.py
│   │       └── juspay/     # Juspay-specific tools
│   │           ├── __init__.py
│   │           └── juspay_tools.py
│   ├── ws/
│   │   ├── __init__.py
│   │   └── live_session.py # WebSocket session handling
│   └── utils/              # Shared utilities (currently minimal)
│       └── __init__.py
├── static/
│   └── client.html         # HTML client for testing
├── memory-bank/            # Project context and decision logs
│   └── ...
├── .gitignore
├── README.md               # This file
├── requirements.txt        # Python dependencies
└── run.py                  # Script to run the server
```

## 4. Setup and Installation

### Prerequisites

*   Python 3.8 or higher
*   Access to Google Gemini API and a valid API Key.
*   (Optional) Juspay account and web login token for using Juspay analytics tools.

### Installation Steps

1.  **Clone the repository (if applicable):**
    ```bash
    git clone <repository-url>
    cd clairvoyance
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up Environment Variables:**
    Create a `.env` file in the project root or set the following environment variables in your system:

    *   `GEMINI_API_KEY`: **Required**. Your Google Gemini API Key.
    *   `GEMINI_MODEL`: Optional. Defaults to `gemini-2.0-flash-live-001`.
    *   `RESPONSE_MODALITY`: Optional. Defaults to `AUDIO`. Can be `TEXT` if only text responses are desired.
    *   `WS_PING_INTERVAL`: Optional. WebSocket ping interval in seconds. Defaults to `5`.
    *   `WS_PING_TIMEOUT`: Optional. WebSocket ping timeout in seconds. Defaults to `10`.
    *   `PORT`: Optional. Port for the server to run on. Defaults to `8000`.
    *   `HOST`: Optional. Host for the server. Defaults to `0.0.0.0`.
    *   `UVICORN_RELOAD`: Optional. Set to `true` or `false` to enable/disable Uvicorn auto-reload. Defaults to `true`.
    *   `UVICORN_LOG_LEVEL`: Optional. Uvicorn log level (e.g., `info`, `debug`). Defaults to `info`.

    Example `.env` file:
    ```env
    GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
    PORT="8000"
    # JUSPAY_WEB_LOGIN_TOKEN="YOUR_JUSPAY_TOKEN_HERE" # Needed by client.html for Juspay tools
    ```
    *Note: The application itself (FastAPI server) expects the Juspay token to be passed by the client via the WebSocket connection URL, not as a server-side environment variable for API calls.*

## 5. Running the Server

Execute the `run.py` script:

```bash
python run.py
```

The server will start, typically on `http://0.0.0.0:8000` (or the configured host/port).
You should see log output indicating the server has started, e.g.:
`INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)`

## 6. Using the Client

A simple HTML client is provided in [`static/client.html`](static/client.html:1).

1.  Once the server is running, open [`static/client.html`](static/client.html:1) in your web browser.
    *   If the server is running locally on port 8000, you can usually access this by navigating to `http://localhost:8000/` in your browser, as `main.py` is configured to serve `client.html` at the root.
2.  **Enter Token:** The client will first ask for a "token". This refers to the **Juspay Web Login Token** if you intend to use the Juspay-specific analytics tools. If you are only using system tools like `getCurrentTime`, any non-empty string can be entered here, but Juspay tools will fail without a valid token.
3.  **Start Call:** Click the "Start Call" button (phone icon) to establish the WebSocket connection and activate the microphone.
4.  **Interact:** Speak into your microphone. The assistant should respond based on its system instructions and available tools.
5.  **Controls:**
    *   **Speaker Toggle:** Mute/unmute audio output from the assistant.
    *   **Microphone Toggle:** Mute/unmute your microphone.
    *   **End Call:** Disconnect the session.
    *   **More Options (Three Dots):**
        *   Clear chat history from the client's display.
        *   Clear the stored Juspay token from the client's local storage.

## 7. Tool System

The server features a dynamic tool system:

*   **Providers:** Tools are organized by "providers" (e.g., `system`, `juspay`). Each provider has its own module under `app/tools/providers/`.
*   **Tool Definition:** Each tool is defined with:
    *   Its `declaration` (schema for Gemini).
    *   A reference to its Python `function`.
    *   `required_context_params`: A list of context keys (e.g., `juspay_token`, `session_id`) that the server will automatically inject into the tool function if available in the WebSocket session state.
*   **Extensibility:** To add new tools or providers:
    1.  Create a new module under `app/tools/providers/your_new_provider/`.
    2.  Define your tool declarations, functions, and rich tool definitions (including `required_context_params`).
    3.  Export a `your_new_provider_tools_definitions` list.
    4.  Import and register this list in `app/tools/__init__.py`.
    5.  If your new tools require new context variables from the client/session, ensure they are added to `websocket.state` in `app/ws/live_session.py` and made available in the `available_context` dictionary within `app/services/gemini_service.py`.

## 8. Development Notes

*   The `memory-bank/` directory contains markdown files used by an AI assistant (like Roo) to maintain context about the project's goals, decisions, and progress. It's not directly used by the server at runtime but is crucial for development with AI assistance.
*   The original `gemini_live_proxy_server.py` and root-level `client.html` are now obsolete and should be deleted after confirming the refactored application works correctly.
*   The placeholder `app/tools/providers/another_provider/` has been removed. New providers should be added as needed.
