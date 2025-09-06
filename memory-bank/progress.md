# Progress & Status

## 1. What Works

- **Remote Tool Integration:** The integration of the `MCPClient` is functional. The voice agent can successfully connect to the remote MCP server, fetch the list of tools, and register them with the LLM.
- **Dynamic Tool Calling:** The agent can correctly identify when a user's request requires a remote tool and can execute the `tools/call` method, passing the necessary arguments and context.
- **Robust Streaming and Parsing:** The `StreamableHTTPTransport` has been refactored to be highly resilient. It now processes streams line-by-line, uses Pydantic for strict data validation against the MCP spec, and correctly parses nested JSON in tool call results.
- **Improved Error Handling:** The transport layer and client now gracefully handle a wide range of exceptions. This includes network errors, HTTP status code errors, and data validation errors. The streaming logic now correctly manages the stream lifecycle during HTTP errors, preventing crashes from race conditions. The client timeout is also now configurable.
- **Context-Aware Sessions:** The system for passing session-specific data (`mcp_context`) via HTTP headers is in place and functional, allowing for authenticated and contextual tool execution.
- **Date-Preserving Summaries:** The summarization prompt has been updated to ensure that dates and time ranges are preserved in conversation summaries.
- **Banner Management Tool:** The new banner management tool is functional. It allows the voice agent to create, update, and remove announcement banners on login and payment pages. The tool includes comprehensive error handling and logging, and integrates with the shop configuration API.
- **Shop Configuration Utilities:** The utility functions for shop configuration management are working correctly. They provide a robust foundation for future tools that need to interact with shop configurations.
- **Enhanced Analytics:** The Breeze analytics tools now fetch more comprehensive metrics for all the tabs by utilizing the `getAllMetricsFromCKH` parameter.
- **Markdown Sanitization for TTS:** The system now correctly sanitizes AI-generated text to remove markdown formatting before sending it to the Text-to-Speech (TTS) service. This is handled in the `LLMSpyProcessor` by modifying the `TextFrame` in-place, which is a stable and performant solution.

## 2. What's Left to Build

- **Data Sanitization/Filtering Layer:** There is currently no mechanism to sanitize the data that flows between the remote MCP server and the LLM. A filtering layer could be developed to redact sensitive information from tool schemas and results before they are exposed to the LLM.
- **Comprehensive Testing:** While the core functionality and error handling are much improved, a full suite of integration tests is needed to validate the end-to-end tool-calling flow with a variety of tools and edge cases, including testing the new error-handling paths.
- **Expanded Content Type Support:** The Pydantic models currently only support `TextContent` from tool results. They could be expanded to handle other types like `ImageContent` or `AudioContent` as specified by the MCP documentation.
- **Enhanced Banner Management:** The current banner management tool supports basic functionality for login and payment page announcements. Future enhancements could include support for more banner types, customization options (colors, icons), scheduling (start/end dates), and targeting specific user segments.
- **Additional Shop Configuration Tools:** Building on the shop configuration utilities, more tools could be developed to manage other aspects of shop configuration, such as theme settings, payment options, or shipping methods.

## 3. Recent Improvements (Latest)

- **Dynamic Prompt Management:** Implemented LangFuse integration to make system prompts dynamic instead of hardcoded. Created `LangFuseService` class for fetching prompts from external LangFuse platform.
- **External Prompt Configuration:** Added comprehensive LangFuse configuration in `config.py` with environment variables for credentials, prompt names, and labels, enabling prompt management outside the codebase.
- **Graceful Fallback System:** Modified `get_system_prompt()` to prioritize LangFuse prompts with automatic fallback to hardcoded `SYSTEM_PROMPT` if LangFuse is unavailable or fails.
- **Template Variable Processing:** Added `{{current_time}}` template variable support in LangFuse prompts with dynamic replacement during prompt processing.
- **No-Code Prompt Updates:** System prompts can now be updated through LangFuse interface without code changes or application deployments.

## 4. Known Issues & Risks

- **Sensitive Data Exposure:** As documented in `activeContext.md`, there is a known risk of exposing sensitive information (from tool schemas and results) to the LLM. This remains the most significant risk.
- **Dependency on Remote Server:** While error handling has been improved, the agent's tooling capability is still entirely dependent on the availability and correctness of the remote MCP server.
- **Shop Configuration API Dependency:** The banner management tool relies on the shop configuration API. Any changes to this API could break the tool's functionality. The tool includes error handling to mitigate this risk, but it's still a dependency to be aware of.
