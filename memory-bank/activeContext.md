# Active Context: Remote Tooling with Custom MCPClient and Banner Management

## 1. Current Work Focus

The primary focus of recent development has been to enable the voice agent to use tools hosted on a remote server, rather than relying solely on locally defined functions. This enhances flexibility and decouples the agent's core logic from the tool implementations. Additionally, a new banner management tool has been implemented to allow the voice agent to create, update, and remove announcement banners on login and payment pages.

## 2. Key Changes and Implementations

- **`MCPClient` Relocated and Refactored:** The `MCPClient` service was moved to a more appropriate location at `app/agents/voice/automatic/services/mcp/automatic_client.py`. Its associated Pydantic models were also moved to `app/agents/voice/automatic/types/models.py` to improve code organization.

- **Robust `StreamableHTTPTransport`:** The transport layer was significantly refactored for robustness. It now uses the relocated Pydantic models to validate all incoming responses against the official MCP specification. The streaming logic now correctly handles HTTP errors by reading the response body before raising an exception, preventing crashes from race conditions on closed streams. The client timeout has also been made configurable via the `MCP_CLIENT_TIMEOUT` environment variable, defaulting to 30 seconds.

- **Dynamic Tool Registration:** The voice agent, when `AUTOMATIC_MCP_TOOL_SERVER_USAGE` is true, now uses the `MCPClient` to:
    1.  Fetch the list of available tools from the remote server at startup.
    2.  Use the shared Pydantic models to validate and convert the tool schemas into a format compatible with PipeCat's LLM service.
    3.  Dynamically register a handler for these tools with the LLM. The registration process is now wrapped in more explicit error logging and handling to prevent agent startup failures.

- **Context-Aware Tool Calls:** All calls made to the remote server by the client include a secure authentication token and a session-specific context. The client now correctly parses both simple `tools/list` responses and complex `tools/call` responses that contain nested JSON, ensuring the LLM receives clean data.

- **Date-Preserving Summarization:** The summarization logic has been updated to explicitly instruct the LLM to preserve dates and time ranges, improving the accuracy of long-term context.

- **Banner Management Tool:** A new tool has been implemented to allow the voice agent to manage announcement banners on login and payment pages:
    1. The tool is defined in `app/agents/voice/automatic/tools/breeze/banner.py` and provides functionality to create, update, and remove banners.
    2. Utility functions for shop configuration management have been added in `app/agents/voice/automatic/tools/breeze/utils.py`.
    3. The tool requires shop ID, shop URL, merchant ID, and user ID to function properly, which are passed during initialization.
    4. Two types of banners are supported: login page announcements and payment page announcements.
    5. The tool interacts with the shop configuration API to manage these banners.

- **Enhanced Analytics Metrics:** The Breeze analytics tools have been updated to include the `getAllMetricsFromCKH` parameter. This parameter is set to `True` for all the operational tabs

## 3. Next Steps & Considerations

- **Security Analysis:** An analysis was performed to identify how sensitive data is exposed. Key risks include the direct exposure of tool schemas and results to the LLM. The `mcp_context` is not directly exposed, but it defines the permissions for the tools the LLM can use.

- **Future Work:** Potential future work could involve creating a sanitization layer to filter or redact sensitive information passing between the remote server and the LLM, or expanding the Pydantic models to support more `ToolResult` content types like images or audio.

- **Banner Management Enhancements:** Consider expanding the banner management tool to support more types of banners and additional customization options like colors, icons, or expiration dates.
