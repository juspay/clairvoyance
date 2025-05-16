from google.genai import types

# Import the rich tool definitions list from each provider
from app.tools.providers.juspay.juspay_tools import juspay_tools_definitions
from app.tools.providers.system.system_tools import system_tools_definitions
# References to another_provider removed.

# This map will store the full definition for each tool, keyed by tool name.
# The full definition includes the declaration, the function reference, and required_context_params.
all_tool_definitions_map = {}

# This list will store just the function declarations for the Gemini API.
all_function_declarations = []

def _register_tool_definitions(tool_definitions_list):
    """Helper function to populate the map and list."""
    if tool_definitions_list: # Ensure the list is not None or empty
        for tool_def in tool_definitions_list:
            declaration = tool_def.get("declaration")
            if declaration and isinstance(declaration, dict) and "name" in declaration:
                tool_name = declaration["name"]
                all_tool_definitions_map[tool_name] = tool_def
                all_function_declarations.append(declaration)
            else:
                # Log a warning or raise an error for malformed tool definitions
                print(f"Warning: Malformed tool definition encountered: {tool_def}")


# Register tools from all providers
_register_tool_definitions(juspay_tools_definitions)
_register_tool_definitions(system_tools_definitions)
# Registration for another_provider_tools_definitions removed.

# Prepare the final tools list for the Gemini API (list of types.Tool objects)
gemini_tools_for_api = []
if all_function_declarations:
    gemini_tools_for_api.append(types.Tool(function_declarations=all_function_declarations))

# What gets imported when someone does 'from app.tools import *'
__all__ = ["gemini_tools_for_api", "all_tool_definitions_map"]