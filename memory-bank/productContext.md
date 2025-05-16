# Product Context

This file provides a high-level overview of the project and the expected product that will be created. Initially it is based upon projectBrief.md (if provided) and all other available project-related information in the working directory. This file is intended to be updated as the project evolves, and should be used to inform all other modes of the project's goals and context.
2025-05-22 18:29:00 - Log of updates made will be appended as footnotes to the end of this file.

*

## Project Goal

* Deploy a FastAPI server that acts as a proxy to the Gemini Live API. This server will expose a WebSocket endpoint for client applications to connect, enabling real-time, bidirectional communication with the Gemini model. The server will handle audio streaming, voice activity detection (VAD), function calls, and present itself as 'Breeze Automatic', a D2C business assistant.

## Key Features

*   Real-time audio streaming to and from the Gemini API.
*   WebSocket endpoint for client communication.
*   Integration with Gemini Live API, specifically using a model like "gemini-2.0-flash-live-001".
*   Support for function calling with predefined tools (e.g., `getCurrentTime`, various Juspay analytics tools), designed for scalability with multiple providers (including system-level tools).
*   Voice Activity Detection (VAD) for managing speech input.
*   CORS middleware for broad client accessibility.
*   Configuration via environment variables (API key, model, response modality).
*   Health check endpoint.
*   Graceful shutdown handling.
*   System instructions to define the assistant's persona ("Breeze Automatic") and capabilities.
*   Modular and scalable project structure.

## Overall Architecture

*   **Frontend (Client):** A client application ([`static/client.html`](static/client.html:1)) connects to the backend via WebSockets to send audio input and receive responses. Served via the FastAPI backend.
*   **Backend (Proxy Server):** A modular Python FastAPI application ([`app/main.py`](app/main.py:1)).
    *   **`app/main.py`**: Initializes the FastAPI app, CORS, static file serving, health check, and WebSocket routing. Manages application lifecycle (startup/shutdown).
    *   **`app/core/config.py`**: Manages all environment variable loading and application settings (API keys, model name, VAD parameters, etc.).
    *   **`app/ws/live_session.py`**: Handles the WebSocket endpoint (`/ws/live`) logic, including client connection, keepalive pings, and orchestrating data flow between the client and the Gemini service.
    *   **`app/services/gemini_service.py`**: Encapsulates all interaction with the Google GenAI Live API. Manages the `LiveConnectSession`, system instructions, response modalities, and processes tool calls by invoking registered tool functions.
    *   **`app/tools/`**: Manages function tool definitions and implementations.
        *   **`app/tools/__init__.py`**: Aggregates tools from different providers.
        *   **`app/tools/providers/`**: Directory containing subdirectories for each tool provider.
            *   **`system/system_tools.py`**: Contains generic system-level utility tools (e.g., `getCurrentTime`).
            *   **`juspay/juspay_tools.py`**: Contains Juspay-specific tool declarations and their implementation functions (e.g., `get_sr_success_rate_by_time`, `make_genius_api_request`).
    *   **`app/utils/`**: (Currently minimal, intended for shared utility functions like VAD if separated further).
    *   Exposes a WebSocket endpoint (`/ws/live`) via `app.ws.live_session.py`.
    *   Receives audio data from the client and forwards it to the Gemini Live API via `app.services.gemini_service.py`.
    *   Manages a `LiveConnectSession` via `app.services.gemini_service.py`.
    *   Implements Voice Activity Detection (VAD) configuration within `app.services.gemini_service.py` (passed to Gemini API).
    *   Handles tool calls initiated by the Gemini model, processed in `app.services.gemini_service.py` which then calls functions from `app.tools`.
    *   Streams responses (audio, text transcripts) from Gemini back to the client via `app.ws.live_session.py`.
*   **External Services:**
    *   **Google Gemini Live API:** The core AI service providing live conversational capabilities, speech-to-text, text-to-speech, and function calling.
    *   **Juspay Genius API:** An external API used by some of the defined function tools for fetching analytics data.
*   **Entry Point:** [`run.py`](run.py:1) script to launch the Uvicorn server for the FastAPI application.

2025-05-22 18:34:00 - Initial population of Project Goal, Key Features, and Overall Architecture.
2025-05-22 21:31:00 - Updated Overall Architecture to reflect the new modular project structure (FastAPI app within `app/` directory, tools organized by providers, services, and core config). Key Features updated to include modularity.
2025-05-22 21:55:00 - Updated Overall Architecture in `app/tools/providers/` to include the new `system/system_tools.py` for generic utilities like `getCurrentTime`. Key Features updated for tool scalability.
2025-05-22 22:00:00 - Removed `another_provider` placeholder from `app/tools/providers/` in Overall Architecture.