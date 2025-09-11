# Clairvoyance

Clairvoyance is a powerful, multi-agent conversational AI platform designed to support sophisticated, real-time voice and data interactions. It is built on a modular architecture featuring a FastAPI server that manages and orchestrates multiple specialized voice agents.

## 1. Core Architecture

The platform is built around a few key components:

*   **FastAPI Server:** The central application that exposes API endpoints, manages agent lifecycles, and handles incoming requests.
*   **Voice Agents:** Specialized, independent agents responsible for handling different conversational workflows. Each agent is built using a robust framework to manage real-time communication.
    *   **Automatic Agent:** A Pipecat-based agent designed for dynamic data retrieval and analytics conversations. It can operate in `live` mode with real-time data or `test` mode with dummy data.
    *   **Breeze Buddy Agent:** An agent focused on telephony and workflow-driven conversations, such as order confirmations. It integrates with multiple telephony providers like Twilio and Exotel.
*   **Database Integration:** The application uses a database to store configuration, track calls, and manage other persistent data.
*   **Docker Support:** The project includes a `Dockerfile` for easy containerization and deployment.

## 2. Key Features

*   **Multi-Agent Support:** Designed to run multiple, distinct voice agents (`Automatic`, `Breeze Buddy`) within a single platform.
*   **Telephony Integration:** The `Breeze Buddy` agent connects with external telephony providers (Twilio, Exotel) to manage real voice calls.
*   **Dynamic Tool Loading:** The `Automatic` agent dynamically loads tools based on the operating mode and credentials, allowing it to interact with services like Juspay and Breeze for analytics.
*   **Workflow-Driven Conversations:** Agents can follow predefined workflows, such as the order confirmation process in `Breeze Buddy`.
*   **Environment-Driven Configuration:** All sensitive keys, API endpoints, and settings are managed via a `.env` file.
*   **Modular & Scalable:** The project is structured for maintainability and easy extension with new agents, tools, or services.

## 3. Project Structure

The project is organized into a main FastAPI application (`app/`) with a clear separation of concerns for agents, API routing, database management, and core services.

```
.
├── app/
│   ├── main.py                 # Main FastAPI application entry point
│   ├── agents/
│   │   └── voice/
│   │       ├── automatic/      # Pipecat-based analytics agent
│   │       └── breeze_buddy/   # Telephony and workflow agent
│   ├── api/
│   │   └── routers/            # FastAPI routers for different endpoints
│   ├── core/
│   │   └── config.py           # Configuration and environment variable management
│   ├── database/
│   │   ├── accessor/           # Database access logic
│   │   └── queries/            # SQL queries
│   ├── scripts/
│   │   └── create_tables.py    # Script to initialize database tables
│   └── services/
│       └── langfuse/           # Integration with Langfuse for tracing
├── Dockerfile                  # Docker configuration for containerization
├── requirements.txt            # Python dependencies
└── run.py                      # Script to run the server
```

## 4. Setup and Installation

### Prerequisites

*   Python 3.8+
*   Database (e.g., PostgreSQL)
*   Access to required third-party APIs (e.g., Azure OpenAI, Daily.co, Twilio/Exotel) with valid keys.

### Installation Steps

1.  **Clone the repository.**
2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Set up Environment Variables:**
    Create a `.env` file in the project root by copying `.env.example` and filling in the required values for the database, API keys, and other configurations.
5.  **Initialize the Database:**
    Run the script to create the necessary tables in your database.
    ```bash
    python -m app.scripts.create_tables
    ```

## 5. Running the Server

Execute the `run.py` script to start the FastAPI server:
```bash
python run.py
```
The server will start on `http://0.0.0.0:8000` by default.

## 6. How It Works

1.  The FastAPI server starts and initializes the API routers.
2.  When a request is made to an agent-specific endpoint (e.g., `/breeze-buddy/make-call`), the corresponding router handles it.
3.  The router logic invokes the appropriate agent manager or service (e.g., `CallsManager` for `Breeze Buddy`).
4.  The agent manager orchestrates the workflow, which may involve:
    *   Interacting with a database to fetch configuration.
    *   Making calls to external services (e.g., starting a call via Twilio).
    *   Launching an agent as a subprocess to handle the real-time conversation.
5.  The voice agent connects to the communication service (like Daily.co or a direct telephony stream) and manages the STT -> LLM -> TTS pipeline, using its specialized tools to complete its task.
