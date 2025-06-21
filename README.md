# Breeze Automatic

Breeze Automatic is a sophisticated, dual-component server designed to power advanced conversational AI experiences. It comprises a **Gemini Live Proxy Server** for direct, low-level integration with Google's Gemini API and a **Pipecat-based Voice Agent** for building robust, real-time voice assistants.

## 1. Core Components

### 1.1. Gemini Live Proxy Server
A FastAPI-based server that acts as a direct proxy to the Gemini Live API. It handles WebSocket connections, pre-fetches and caches analytics data from various sources (Juspay, Breeze), and enriches the Gemini model's context for more informed, real-time conversations.

### 1.2. Pipecat Voice Agent
A standalone voice agent built on the Pipecat framework. It's launched as a subprocess by the main FastAPI server and handles the end-to-end voice conversation flow, including:
*   Speech-to-Text (STT)
*   Language Model (LLM) interaction with dynamic tool use
*   Text-to-Speech (TTS)

## 2. Key Features

*   **Dual-Mode Operation:** Can run in `live` mode with real-time data fetching or `test` mode using dummy data.
*   **Dynamic Tool Loading:** The voice agent dynamically loads tools based on the operating mode and provided credentials (e.g., Juspay and Breeze tools are only loaded in `live` mode with valid tokens).
*   **Multi-Provider Analytics:** Integrates with both **Juspay** and **Breeze** APIs to fetch a wide range of analytics data, including sales, orders, marketing, and checkout metrics.
*   **Personalized Prompts:** The agent's system prompt can be personalized with the user's name for a more engaging experience.
*   **Real-time Audio Streaming:** Bidirectional audio streaming via WebSockets.
*   **Environment-Driven Configuration:** All sensitive keys and settings are managed via environment variables.
*   **Modular & Scalable Architecture:** The project is structured for clarity, maintainability, and easy extension with new tools or providers.

## 3. Project Structure

The project is organized into two main parts: the FastAPI server (`app/`) and the Pipecat voice agent (`app/agents/voice/automatic/`).

```
.
├── app/
│   ├── main.py                 # FastAPI app, agent endpoint, and subprocess management
│   ├── ws/live_session.py      # WebSocket session handling for Gemini Live Proxy
│   ├── services/gemini_service.py # Gemini API interaction logic for proxy
│   ├── api/                    # API clients for Juspay, Breeze, etc.
│   │   ├── juspay_metrics.py
│   │   └── breeze_metrics.py
│   ├── tools/                  # Tool definitions for the Gemini Live Proxy
│   │   └── ...
│   └── agents/voice/automatic/ # Pipecat Voice Agent
│       ├── __init__.py         # Main agent logic, pipeline definition
│       ├── prompts.py          # System prompts for the agent
│       └── tools/              # Tool definitions for the agent
│           ├── __init__.py     # Dynamic tool initializer
│           ├── system/         # System tools (e.g., get_current_time)
│           ├── dummy/          # Dummy tools for test mode
│           ├── juspay/         # Real-time Juspay analytics tools
│           └── breeze/         # Real-time Breeze analytics tools
├── static/
│   └── client.html             # HTML client for testing
├── requirements.txt
└── run.py                      # Script to run the server
```

## 4. Setup and Installation

### Prerequisites

*   Python 3.8+
*   Access to Google Gemini, Azure OpenAI, and Daily.co APIs with valid keys.

### Installation Steps

1.  **Clone the repository.**
2.  **Create and activate a virtual environment.**
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Set up Environment Variables:**
    Create a `.env` file in the project root with the following variables:
    *   `DAILY_API_KEY`: **Required**.
    *   `AZURE_OPENAI_API_KEY`: **Required**.
    *   `AZURE_OPENAI_ENDPOINT`: **Required**.
    *   `GOOGLE_CREDENTIALS_JSON`: **Required**. Path to your Google Cloud credentials JSON file.
    *   `GEMINI_API_KEY`: **Required** for the Gemini Live Proxy.

## 5. Running the Server

Execute the `run.py` script:
```bash
python run.py
```
The server will start on `http://0.0.0.0:8000` by default.

## 6. How It Works: The Voice Agent Flow

1.  A client sends a POST request to the `/agent/voice/automatic` endpoint on the FastAPI server.
2.  The payload includes the `mode` (`live` or `test`) and various tokens/IDs (`eulerToken`, `breezeToken`, `shopId`, etc.).
3.  The server creates a new Daily.co video room for the voice session.
4.  It then launches the Pipecat voice agent as a **new subprocess**, passing the mode, tokens, and shop details as command-line arguments.
5.  Inside the agent's `__init__.py`, the `initialize_tools` function is called.
6.  This function checks the `mode` and the presence of tokens to decide which toolsets to load:
    *   **System tools** are always loaded.
    *   In `test` mode, **dummy tools** are loaded.
    *   In `live` mode, if tokens are present, the corresponding **real-time Juspay and Breeze tools** are loaded.
7.  The agent's system prompt is personalized with the user's name if provided.
8.  The agent connects to the Daily room and begins the conversation, now equipped with the appropriate set of tools for the session.

This architecture allows for clean separation of concerns and enables the creation of highly contextual and capable voice assistants.
