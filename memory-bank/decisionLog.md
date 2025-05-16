# Decision Log

This file records architectural and implementation decisions using a list format.
2025-05-22 18:29:00 - Log of updates made.

*

## Decision

*   [2025-05-22 18:35:00] - Implement and populate the Memory Bank.

## Rationale 

*   To maintain project context, track progress, log decisions, and document system patterns effectively across chat sessions and for different modes. This enhances continuity and understanding of the project's evolution.

## Implementation Details

*   Created a `memory-bank/` directory.
*   Created and populated the following files with initial content and project-specific information:
    *   `productContext.md`: Outlines project goal, key features, and overall architecture.
    *   `activeContext.md`: Tracks current focus and recent changes.
    *   `progress.md`: Lists completed and current tasks.
    *   `decisionLog.md`: Records this decision.
    *   `systemPatterns.md`: Initialized for future use.
*   Updates to these files are timestamped.
*

## Decision

*   [2025-05-22 18:37:00] - Modify system instructions in `gemini_live_proxy_server.py`.

## Rationale

*   User request to add a specific instruction for interpreting spoken inputs: "Please interpret all spoken inputs as English, regardless of accent." This is to improve the accuracy and handling of voice input by the Gemini model.

## Implementation Details

*   Added the new instruction string to the `system_instr` variable within the `## Communication Style` section in `gemini_live_proxy_server.py`.
*   Updated `activeContext.md` and `progress.md` to reflect this change.
*

## Decision

*   [2025-05-22 18:43:00] - Refactor function response formatting in `gemini_live_proxy_server.py`.

## Rationale

*   User reported an issue where the Gemini Live API sometimes vocalizes "tool_outputs" during function calls.
*   An external recommendation suggests this occurs if function responses do not use an `output` key within the `response` dictionary of `types.FunctionResponse`.
*   The change aligns the server's function response structure with the API's expectation to prevent this unintended speech output.

## Implementation Details

*   Modified the `process_tool_calls` function in `gemini_live_proxy_server.py`.
*   For every `types.FunctionResponse` instance, the `response` dictionary was changed from `{'some_key': result}` to `{'output': result}`.
*   Updated `activeContext.md` and `progress.md` to reflect this refactoring.
*

## Decision

*   [2025-05-22 21:31:00] - Refactor project into a modular FastAPI application structure.

## Rationale

*   User request to transform the single-file server (`gemini_live_proxy_server.py`) into a well-structured, industry-standard Python server project.
*   The new structure aims for better organization, maintainability, and scalability, especially for adding new tool providers in the future.
*   Ensures current functionality remains identical.

## Implementation Details

*   Created an `app/` directory to house the core application logic.
*   **`app/main.py`**: FastAPI app instantiation, CORS, static file serving (for [`static/client.html`](static/client.html:1)), health check endpoint, WebSocket routing, and application lifecycle management.
*   **`app/core/config.py`**: Centralized configuration loading from environment variables (API keys, model settings, VAD parameters, etc.).
*   **`app/ws/live_session.py`**: Manages WebSocket connections, client communication, keepalive pings, and orchestrates data flow with the Gemini service.
*   **`app/services/gemini_service.py`**: Encapsulates all interactions with the Google GenAI Live API, including session management, system instructions, response modality configuration, and tool call processing.
*   **`app/tools/`**: Directory for managing tool definitions and implementations.
    *   `app/tools/__init__.py`: Aggregates tool declarations and functions from various providers.
    *   `app/tools/providers/juspay/juspay_tools.py`: Contains Juspay-specific tool logic.
    *   `app/tools/providers/another_provider/some_other_tools.py`: Placeholder for future tool providers.
*   **`static/client.html`**: The client-side HTML interface, moved to a dedicated static directory.
*   **`run.py`**: A root-level script to easily run the Uvicorn server for the FastAPI application.
*   All `__init__.py` files created as necessary to define Python packages.
*   The original `gemini_live_proxy_server.py` is now superseded by this new structure.
*   Updated `productContext.md`, `activeContext.md`, and `progress.md` to reflect these changes.
*

## Decision

*   [2025-05-22 21:52:00] - Refactor tool handling mechanism for improved scalability and maintainability.

## Rationale

*   The previous method of identifying tools (e.g., Juspay tools) in `app/services/gemini_service.py` by hardcoding names to pass context parameters (`juspay_token`, `session_id`) was not scalable.
*   A new system was needed where tools can declare their own context requirements.

## Implementation Details

*   **Tool Definition Enhancement (e.g., [`app/tools/providers/juspay/juspay_tools.py`](app/tools/providers/juspay/juspay_tools.py:1)):**
    *   Tool definitions were changed from separate lists of declarations and functions to a single list of "rich tool definitions".
    *   Each rich definition is a dictionary containing:
        *   `"declaration"`: The standard Gemini function declaration.
        *   `"function"`: A direct reference to the callable Python function for the tool.
        *   `"required_context_params"`: A new list of strings specifying context parameter names (e.g., `["juspay_token", "session_id"]`) that the tool's function expects.
*   **Tool Aggregation ([`app/tools/__init__.py`](app/tools/__init__.py:1)):**
    *   This module now imports the lists of rich tool definitions from each provider (e.g., `juspay_tools_definitions`).
    *   It populates `all_tool_definitions_map`: A dictionary mapping each tool's name to its full rich definition.
    *   It also creates `gemini_tools_for_api`: A list of `types.Tool` objects (containing only the function declarations) for configuring the Gemini API.
*   **Service Layer Update ([`app/services/gemini_service.py`](app/services/gemini_service.py:1)):**
    *   The `process_tool_calls` function was refactored:
        *   It now uses `all_tool_definitions_map` to retrieve a tool's full definition by its name.
        *   It extracts the `tool_function` and `required_context_params` from this definition.
        *   It prepares an `available_context` dictionary (e.g., from `websocket.state`).
        *   It dynamically builds the `kwargs` for the `tool_function` by combining `fc.args` with the values of `required_context_params` found in `available_context`.
    *   The `get_live_connect_config` function was updated to use `gemini_tools_for_api` when setting up the Gemini session.
*   This change makes the system more extensible, as adding new tools or providers with different context needs only requires updating their respective definition files and potentially the `available_context` in `gemini_service.py` if new context sources are introduced.
*   Updated `activeContext.md`, `progress.md`, and `systemPatterns.md` to reflect this refactoring.
*

## Decision

*   [2025-05-22 21:55:00] - Create a dedicated 'system' tool provider for generic utility tools.

## Rationale

*   To better organize tools and separate general utility functions (like `getCurrentTime`) from provider-specific tools (like Juspay analytics).
*   Improves modularity and clarity of the tool structure, aligning with the goal of a scalable system.

## Implementation Details

*   Created a new directory: `app/tools/providers/system/`.
*   Created `app/tools/providers/system/__init__.py`.
*   Created `app/tools/providers/system/system_tools.py`.
    *   Moved the `get_current_time_declaration` and `get_current_time` function from `app/tools/providers/juspay/juspay_tools.py` to this new file.
    *   Defined a `system_tools_definitions` list in `system_tools.py` containing the rich definition for `getCurrentTime` (with an empty `required_context_params` list).
*   Modified `app/tools/providers/juspay/juspay_tools.py`:
    *   Removed the `getCurrentTime` declaration, function, and its entry from `juspay_tools_definitions`.
*   Modified `app/tools/__init__.py`:
    *   Imported `system_tools_definitions` from `app.tools.providers.system.system_tools`.
    *   Added `system_tools_definitions` to the `_register_tool_definitions` call, so system tools are aggregated along with other providers.
*   Updated `activeContext.md`, `progress.md`, `systemPatterns.md`, and `productContext.md` to reflect this new tool provider structure.
*

## Decision

*   [2025-05-22 22:00:00] - Remove placeholder `another_provider` tool directory and references.

## Rationale

*   User request to remove unused placeholder code to keep the project clean.
*   New providers will be added only when a specific requirement arises.

## Implementation Details

*   Modified `app/tools/__init__.py` to remove commented-out import and registration lines for `another_provider_tools_definitions`.
*   User advised to manually delete the `app/tools/providers/another_provider/` directory and its files ([`app/tools/providers/another_provider/__init__.py`](app/tools/providers/another_provider/__init__.py:1), [`app/tools/providers/another_provider/some_other_tools.py`](app/tools/providers/another_provider/some_other_tools.py:1)) as direct file deletion is not available via tools.
*   Updated `productContext.md`, `activeContext.md`, `progress.md`, and `systemPatterns.md` to remove mentions of the `another_provider` placeholder.
*

## Decision
*   [2025-05-22 22:26:00] - Create `app/services/__init__.py`.

## Rationale
*   To ensure the `app/services/` directory is correctly recognized as a Python package, maintaining consistency with other package directories and adhering to Python best practices. This was an oversight during the initial refactoring.

## Implementation Details
*   Created an empty file `app/services/__init__.py` with a comment indicating its purpose.
*   Updated `activeContext.md` and `progress.md` to log this corrective action.
*

## Decision
*   [2025-05-22 23:11:00] - Update, Reformat, Refine Tone, and Add Numeral Instruction to System Prompt in `app/services/gemini_service.py`.

## Rationale
*   User provided a new, detailed system prompt to replace the existing one.
*   Further user feedback indicated the need to reformat the prompt (using `#` for headings, single newlines).
*   Additional feedback requested emphasizing "Warm & Engaging" and "Sensual" personality aspects.
*   User requested an explicit instruction for the AI to use numerals for specific data like percentages, instead of spelling them out.

## Implementation Details
*   The `text` attribute of the `system_instr` variable (a `types.Content` object) in [`app/services/gemini_service.py`](app/services/gemini_service.py:1) was replaced with the new prompt text.
*   The prompt text was then reformatted:
    *   Headings (e.g., "Role & Identity") were changed to use a single `#` prefix (e.g., "# Role & Identity").
    *   Double newlines (`\n\n`) were reduced to single newlines (`\n`) throughout the prompt.
*   The "Personality & Communication Style" section within the prompt was further refined to enhance the "Warm & Engaging" and "Sensual" characteristics.
*   A new instruction was added to the "Personality & Communication Style" section: `"When presenting numerical data, especially percentages or specific figures, use numerals (e.g., "81.33%", "25 units") for precision and clarity, while still adhering to the Indian numbering system for large values (e.g., "₹2.5 lakh")."\n`
*   Updated `activeContext.md` and `progress.md` to reflect all these changes.
*

## Decision
*   [2025-05-24 18:29:00] - Implement conditional server startup based on `enableSimpleV2Flow` flag.

## Rationale
*   User request to allow switching between the main modular application (in `app/`) and a simplified single-file application (`v2.py`) via a configuration flag.
*   This provides flexibility for testing or running different versions of the server.

## Implementation Details
*   Read and understood the structure of [`v2.py`](v2.py:1), which is a self-contained FastAPI application.
*   Added a new boolean configuration `ENABLE_SIMPLE_V2_FLOW` to [`app/core/config.py`](app/core/config.py:1).
    *   It defaults to `False`.
    *   It's loaded from the `ENABLE_SIMPLE_V2_FLOW` environment variable.
*   Modified [`run.py`](run.py:1):
    *   Imported `ENABLE_SIMPLE_V2_FLOW` from `app.core.config`.
    *   If `ENABLE_SIMPLE_V2_FLOW` is `True`, `uvicorn.run` now targets `"v2:app"` (referring to the `app` instance in [`v2.py`](v2.py:1)).
    *   Otherwise, `uvicorn.run` targets the default `"app.main:app"`.
*   Updated `activeContext.md` and `progress.md` to reflect these changes.
*

## Decision
*   [2025-05-24 18:45:00] - Update system prompt in `v2.py` for a more sensual, cheesy, flirty, and expressive personality.

## Rationale
*   User request to modify the AI's personality in the `v2.py` version of the application.
*   The goal is to make the AI more engaging by incorporating specific traits like sensuality, flirtatiousness, cheesiness, and natural vocal expressions (e.g., laughter, amazement).

## Implementation Details
*   Modified the `system_instr` variable in [`v2.py`](v2.py:1).
*   Updated the "Personality" section to:
    *   Enhance "Warm & Engaging" to "Warm, Engaging & Flirty," encouraging a subtly sensual tone, cheekiness, and light flirtatiousness.
    *   Refine "Cheesy & Chill" to emphasize playful, witty banter.
    *   Added a "Naturally Expressive" point, guiding the AI to use natural vocal cues like soft laughs or sounds of amazement sparingly and appropriately.
*   Updated the "Communication Style" section to:
    *   Include a specific instruction to weave in natural expressions (e.g., 'haha', 'ooh', 'wow') when contextually appropriate, to reinforce the playful and engaging personality.
*   Updated `activeContext.md` and `progress.md` to reflect these changes.