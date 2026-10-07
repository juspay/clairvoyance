"""Client tools — tools the BROWSER runs instead of the server.

Some answers exist only in the shopper's browser (which page is open, what
it shows). A client tool is published to the LLM like any other function,
but it rides the HITL approval gate instead of a server handler: the turn
ends at the call, the widget runs it, and the widget's answer comes back on
the approval endpoint as the call's ``result``. Expiry, the atomic claim,
superseding and dangling-row repair all come with the gate unchanged.

Opt-in per template via ``configurations.client_tools``; empty (the
default) publishes nothing.
"""

from typing import Any, Dict, List, Optional

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.template.types import ApprovalConfig
from app.core.logger import logger

GET_PAGE_CONTEXT = "get_page_context"

# The widget answers on its own (no human in the loop), so a short TTL: a
# browser that never answers expires the call instead of parking it for the
# approval default of an hour.
CLIENT_TOOL_EXPIRY_SECS = 60

_CLIENT_TOOLS: Dict[str, Dict[str, Any]] = {
    GET_PAGE_CONTEXT: {
        "description": (
            "Read the page the user currently has open on the website: "
            "url, title, page type (home / product / collection / cart / "
            "other), the product on it if any (id, handle, title, price), "
            "and the page's main text. Call this when the user refers to "
            "what they are looking at ('this', 'this product', 'this "
            "page', 'here') or when the answer depends on the current page. "
            "If the context's current_page differs from the page you last "
            "read, the user has moved: call it again. Takes no arguments."
        ),
        "properties": {},
        "required": [],
    },
}


def is_client_tool(name: str) -> bool:
    return name in _CLIENT_TOOLS


def enabled_client_tools(configurations: Optional[Any]) -> List[str]:
    """The template's known client tools, in declared order. An unknown name
    is dropped and logged, never fatal."""
    names = getattr(configurations, "client_tools", None) or []
    enabled = [n for n in names if is_client_tool(n)]
    for unknown in sorted(set(names) - set(enabled)):
        logger.warning(f"[client_tools] Unknown client tool '{unknown}' ignored")
    return enabled


async def _never_runs_on_server(args: Dict[str, Any], flow_manager: Any) -> Any:
    # Every client tool is in the approval map, so the gate partitions it out
    # before dispatch. Reaching here means it was dispatched anyway (e.g. a
    # per-node function of the same name shadowed it).
    return {"status": "error", "error": "this tool runs in the browser"}


def build_client_tool_functions(names: List[str]) -> List[FlowsFunctionSchema]:
    return [
        FlowsFunctionSchema(
            name=name,
            description=_CLIENT_TOOLS[name]["description"],
            properties=_CLIENT_TOOLS[name]["properties"],
            required=_CLIENT_TOOLS[name]["required"],
            handler=_never_runs_on_server,
        )
        for name in names
    ]


def client_tool_approval() -> ApprovalConfig:
    return ApprovalConfig(chat_expiry_secs=CLIENT_TOOL_EXPIRY_SECS)


def client_tool_result(row_function_name: str, result: Any) -> Dict[str, Any]:
    """The tool_result the LLM reads for a client tool's answer. A missing or
    non-object answer becomes an error the model can react to."""
    if isinstance(result, dict):
        return result
    logger.warning(
        f"[client_tools] '{row_function_name}' answered without a result object"
    )
    return {"status": "error", "error": "the browser did not return a result"}
