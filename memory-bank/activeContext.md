# Active Context

This file tracks the project's current status, including recent changes, current goals, and open questions.
2025-05-22 18:29:00 - Log of updates made.

*

## Current Focus

*   [2025-05-24 18:45:00] - Updated system prompt in [`v2.py`](v2.py:1) for a more sensual, cheesy, flirty, and expressive personality.

## Recent Changes

*   [2025-05-24 18:45:00] - Updated system prompt in [`v2.py`](v2.py:1) to enhance sensual, cheesy, flirty personality aspects and encourage natural expressions (e.g., laughter, amazement).
*   [2025-05-24 18:29:00] - Added `enableSimpleV2Flow` boolean flag to [`app/core/config.py`](app/core/config.py:1) (default `False`), loaded from `ENABLE_SIMPLE_V2_FLOW` env var.
*   [2025-05-24 18:29:00] - Modified [`run.py`](run.py:1) to import `enableSimpleV2Flow` and conditionally run Uvicorn with target `"v2:app"` if True, or `"app.main:app"` if False.
*   [2025-05-22 18:34:00] - Memory Bank initialized: Created `productContext.md`, `activeContext.md`, `progress.md`, `decisionLog.md`, `systemPatterns.md`.
*   [2025-05-22 18:34:00] - `productContext.md` populated with Project Goal, Key Features, and Overall Architecture.
*   [2025-05-22 18:34:00] - `progress.md` updated with completed and current tasks related to Memory Bank population.
*   [2025-05-22 18:35:00] - `decisionLog.md` updated with the decision to implement and populate the Memory Bank.
*   [2025-05-22 18:37:00] - Modified `gemini_live_proxy_server.py`: Added system instruction "Please interpret all spoken inputs as English, regardless of accent." to the `system_instr` variable.
*   [2025-05-22 18:43:00] - Refactored `process_tool_calls` in `gemini_live_proxy_server.py`: Changed function response structure to use `{'output': result}` to align with Gemini API expectations and prevent vocalization of "tool_outputs".
*   [2025-05-22 21:31:00] - Major Refactoring: Restructured the `gemini_live_proxy_server.py` into a modular FastAPI application within an `app/` directory.
    *   Created `app/main.py` as the FastAPI entry point.
    *   Moved configuration to `app/core/config.py`.
    *   Organized tools under `app/tools/providers/` (e.g., `juspay_tools.py`).
    *   Centralized Gemini API interaction in `app/services/gemini_service.py`.
    *   Handled WebSocket logic in `app/ws/live_session.py`.
    *   Added `run.py` for server execution.
    *   Moved `client.html` to `static/client.html` and configured static serving.
*   [2025-05-22 21:31:00] - `productContext.md` updated to reflect the new modular architecture.
*   [2025-05-22 21:36:00] - Bug Fix: Corrected `AttributeError` in `app/services/gemini_service.py` by changing `websocket_state.get("attr")` to `websocket_state.attr` for accessing `juspay_token` and `session_id`.
*   [2025-05-22 21:41:00] - Logging Adjustment: Changed log level for audio data messages in `app/ws/live_session.py` from INFO to DEBUG to reduce log verbosity.
*   [2025-05-22 21:52:00] - Tool Handling Refactoring:
    *   Modified `app/tools/providers/juspay/juspay_tools.py` and placeholder `another_provider` to define tools with `declaration`, `function` reference, and `required_context_params` metadata.
    *   Updated `app/tools/__init__.py` to aggregate these rich tool definitions into `all_tool_definitions_map` and `gemini_tools_for_api`.
    *   Refactored `process_tool_calls` in `app/services/gemini_service.py` to dynamically use this map for context parameter passing.
    *   Updated `get_live_connect_config` in `app/services/gemini_service.py` to use the new `gemini_tools_for_api`.
*   [2025-05-22 21:55:00] - System Tools Provider:
    *   Created `app/tools/providers/system/system_tools.py`.
    *   Moved `getCurrentTime` tool (declaration and function) from Juspay tools to system tools.
    *   Updated `app/tools/providers/juspay/juspay_tools.py` to remove `getCurrentTime`.
    *   Updated `app/tools/__init__.py` to register tools from the new system provider.
*   [2025-05-22 22:00:00] - Removed Placeholder Provider:
    *   Updated `app/tools/__init__.py` to remove references to `another_provider`.
    *   User advised to manually delete the `app/tools/providers/another_provider/` directory and its files.
*   [2025-05-22 22:09:00] - Confirmed with user that old root-level files (`gemini_live_proxy_server.py`, `client.html`) can be manually deleted.
*   [2025-05-22 22:14:00] - Created comprehensive `README.md` for the project.
*   [2025-05-22 22:26:00] - Created missing `app/services/__init__.py` file to ensure proper package structure.
*   [2025-05-22 22:50:00] - Updated system prompt in `app/services/gemini_service.py` with new detailed instructions provided by the user.
*   [2025-05-22 22:54:00] - Reformatted system prompt in `app/services/gemini_service.py` (used `#` for headings, single newlines).
*   [2025-05-22 23:02:00] - Refined system prompt in `app/services/gemini_service.py` to emphasize "Warm & Engaging" and "Sensual" personality aspects.
*   [2025-05-22 23:11:00] - Added instruction to system prompt in `app/services/gemini_service.py` for using numerals for specific numerical data (e.g., percentages).

## Open Questions/Issues

*   User to manually delete `gemini_live_proxy_server.py` (root).
*   User to manually delete `client.html` (root).
*   User to manually delete `app/tools/providers/another_provider/` directory.
*   Thorough testing of the new server structure and all functionalities after manual deletions and system prompt update.