# System Patterns *Optional*

This file documents recurring patterns and standards used in the project.
It is optional, but recommended to be updated as the project evolves.
2025-05-22 18:30:00 - Log of updates made.

*

## Coding Patterns

*

## Architectural Patterns

*   **[2025-05-22 22:00:00] - Modular FastAPI Application Structure:**
    *   **Description:** The backend server is organized into a main `app/` directory containing sub-modules for distinct responsibilities:
        *   `main.py`: FastAPI application setup, routing, and lifecycle.
        *   `core/`: Core components like configuration (`config.py`).
        *   `services/`: Business logic for interacting with external services (e.g., `gemini_service.py`).
        *   `tools/`: Manages tool definitions and implementations, further organized by `providers/` (e.g., `juspay/`, `system/`).
        *   `ws/`: WebSocket specific logic (e.g., `live_session.py`).
        *   `utils/`: Shared utility functions.
    *   **Rationale:** Promotes separation of concerns, maintainability, scalability (especially for adding new tool providers or features), and testability. Adheres to common practices for structuring Python web applications.
    *   **Entry Point:** A `run.py` script in the project root is used to launch the Uvicorn server for the FastAPI app.
    *   **Static Files:** Client-side assets (like `client.html`) are served from a `static/` directory.

*   **[2025-07-14] - Remote Tooling via MCP Client:**
    *   **Description:** The system architecture was updated to support fetching and executing tools from a remote server that adheres to the Model Context Protocol (MCP). This decouples the agent from the tool implementations.
        *   **`MCPClient` Service:** A dedicated client (`app/agents/voice/automatic/services/mcp/automatic_client.py`) handles all communication with the remote MCP server.
        *   **`StreamableHTTPTransport`:** A robust transport layer within the client manages the SSE (Server-Sent Events) connection, including request signing and response parsing.
        *   **Pydantic Model Validation:** The transport layer uses Pydantic models (`app/agents/voice/automatic/types/models.py`) to validate all incoming data against the MCP specification, ensuring resilience against malformed or unexpected responses.
        *   **Dynamic Registration:** On startup, the `MCPClient` connects to the remote server, fetches the list of available tools, and dynamically registers them with the LLM service, making the agent's capabilities extensible without redeployment.
    *   **Rationale:**
        *   Decouples the voice agent from the tool logic, allowing tools to be updated independently.
        *   Enhances security by centralizing tool execution on a remote server.
        *   Improves code organization by moving client logic and type definitions to dedicated modules within the `automatic` agent's folder structure.
    *   **Impact:** Replaces the previous pattern of locally defined tools with a more scalable and flexible remote tooling architecture.

*   **[2025-08-24] - Shop Configuration Management Pattern:**
    *   **Description:** A new pattern has been established for managing shop configurations through the voice agent. This pattern is first implemented with the banner management tool.
        *   **Utility Functions:** Common shop configuration operations are abstracted into utility functions in `app/agents/voice/automatic/tools/breeze/utils.py`:
            *   `get_current_shop_config_data`: Fetches the current configuration for a shop.
            *   `patch_shop_config`: Updates the shop configuration with new values.
            *   Helper functions for URL parsing and shop identification.
        *   **Tool Implementation:** Specific tools like the banner management tool in `app/agents/voice/automatic/tools/breeze/banner.py` use these utility functions to perform their operations.
        *   **Context Passing:** Shop-specific context (shop ID, URL, merchant ID, user ID) is passed during tool initialization and stored as module-level variables.
        *   **Error Handling:** Comprehensive error handling with detailed logging and user-friendly error messages.
    *   **Rationale:**
        *   Promotes code reuse by centralizing common shop configuration operations.
        *   Enhances maintainability by separating the utility functions from the specific tool implementations.
        *   Improves error handling and logging for better debugging and user experience.
    *   **Impact:** Establishes a pattern for future tools that need to interact with shop configurations, ensuring consistency and reducing code duplication.

## Testing Patterns

*
