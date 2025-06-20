from pipecat.adapters.schemas.tools_schema import ToolsSchema
from .dummy import tools as dummy_tools, tool_functions as dummy_tool_functions
from .system import tools as system_tools, tool_functions as system_tool_functions

# Aggregate all tools and tool functions from different sub-packages
# In the future, you can add more tool packages here
all_tools = dummy_tools.standard_tools + system_tools.standard_tools
all_tool_functions = {**dummy_tool_functions, **system_tool_functions}

# Create a single ToolsSchema with all aggregated tools
tools = ToolsSchema(standard_tools=all_tools)

# Expose the aggregated tools and tool functions
__all__ = ["tools", "all_tool_functions"]