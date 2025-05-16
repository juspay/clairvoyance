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

*   **[2025-05-22 22:00:00] - Scalable Tool Handling with Context Injection and Provider-Based Organization:**
    *   **Description:** A system for managing and invoking tools (functions callable by the AI model) that allows tools to declare their specific context dependencies and organizes tools by providers.
        *   **Tool Definition & Providers:** Tools are organized into provider-specific modules (e.g., `app/tools/providers/juspay/juspay_tools.py`, `app/tools/providers/system/system_tools.py`). Each tool is defined as a "rich object" or dictionary containing:
            *   `"declaration"`: The function declaration schema for the Gemini API.
            *   `"function"`: A direct reference to the callable Python function.
            *   `"required_context_params"`: A list of strings specifying context parameter names (e.g., `["juspay_token", "session_id"]`) that the tool's function expects. System tools might have an empty list if they require no special context.
        *   **Tool Aggregation (`app/tools/__init__.py`):**
            *   Imports rich tool definitions from all active provider modules (e.g., `juspay_tools_definitions`, `system_tools_definitions`).
            *   Creates `all_tool_definitions_map`: A dictionary mapping tool names to their full rich definition.
            *   Creates `gemini_tools_for_api`: A list of `types.Tool` objects (containing only declarations) for the Gemini API.
        *   **Context Injection (`app/services/gemini_service.py` - `process_tool_calls`):**
            *   When a tool is called, its definition is retrieved from `all_tool_definitions_map`.
            *   The `required_context_params` are identified.
            *   An `available_context` dictionary (sourced from `websocket.state` or other relevant places) is used to supply values for these required parameters.
            *   The tool function is called with its standard arguments (`fc.args`) plus the injected context parameters.
    *   **Rationale:**
        *   Decouples the tool invocation logic in the service layer from the specific context needs of individual tools.
        *   Enhances scalability: New tools or providers can be added by defining their context needs without modifying the core `process_tool_calls` logic for parameter passing.
        *   Improves maintainability: Tool-specific dependencies are explicit and co-located with tool definitions within their respective provider modules.
        *   Clearer organization of tools by their source or type (e.g., Juspay-specific vs. general system utilities).
    *   **Impact:** Replaces hardcoded checks for tool names when passing context, making the system more generic and robust. Facilitates easier addition of diverse toolsets.

## Testing Patterns

*