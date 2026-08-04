"""Shared runtime pieces of the ChatAgent package: module constants,
pure helpers, and the small per-turn dataclasses. No agent state —
everything here is importable by every sibling mixin module without
cycles."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat_flows import FlowsFunctionSchema

# Each tool-call → handler → re-invoke counts as one cycle. The guard stops a
# pathological template (handler always returns a transition that loops back)
# from burning unbounded LLM calls. Set to 20 (was 8): legitimate multi-item
# flows — e.g. "build a pink combo: top + bottom + socks" — fan out several
# searches plus cart calls in a single turn and were tripping the old cap
# mid-task, ending the turn with no reply. Identity projection (see
# ``tool_context_projection``) keeps prior-search data in context so the model
# stops re-searching what it already found, which keeps real turns well under
# this ceiling; 20 is headroom, not a target.
_MAX_TOOL_CYCLES = 20

# The forced final chips cycle's user-role nudge (quick_replies=
# 'forced_final'). It rides an internal USER row so live context and
# next-turn replay stay identical AND user/model alternation holds around
# the chips function call (Vertex rejects consecutive model contents);
# widget-facing read paths filter internal blocks, so it never shows.
_CHIPS_NUDGE = (
    "(final check: call render_ui with quick_replies=[2-4 SHORT follow-ups "
    "(<=4 words each) grounded in your reply], or decision='no_ui'. Never "
    "duplicate an action already available on UI rendered this turn.)"
)


def _chip_labels(raw: Any) -> List[str]:
    """Lift a `quick_replies` arg (strings canonical; {'label': …} dicts
    tolerated) into clean labels — same tolerance as execute_render_ui's
    extraction, kept tiny here for the rider-harvest path."""
    if not isinstance(raw, list):
        return []
    labels: List[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            labels.append(entry.strip())
        elif isinstance(entry, dict) and isinstance(entry.get("label"), str):
            if entry["label"].strip():
                labels.append(entry["label"].strip())
    return labels[:5]


def is_approval_gated(
    tool_name: str,
    approval_map: Dict[str, Any],
    node: Dict[str, Any],
) -> bool:
    """Whether ``tool_name`` is HITL-gated for a dispatch in ``node``.

    A gated name shadowed by a per-node function in the CURRENT node is
    treated as UNGATED: in that node the caller reaches the per-node
    function (which the author did not gate), not the gated global of the
    same name. Non-shadow nodes are unaffected — a gated global is still
    gated. This keeps chat consistent with voice, whose wrapper gates only
    globals.

    The single definition of "is this call gated", shared by the LLM path
    (:func:`_partition_gated_calls`) and the no-LLM direct/intent path — a
    tool must not be gated on one surface and free on the other.
    """
    if tool_name not in approval_map:
        return False
    # ``node["functions"]`` holds FlowsFunctionSchema objects in flow mode
    # (FlowConfigBuilder._build_node runs every per-node function through
    # _build_function_schema) — NOT plain dicts. Match the idiom used by
    # _dispatch_tool_call / _tools_schema below: filter on FlowsFunctionSchema
    # and read ``.name``. The builder renames function_name→name before the
    # schema exists, so there is no alias to fall back to here.
    node_fn_names = {
        fn.name
        for fn in (node.get("functions") or [])
        if isinstance(fn, FlowsFunctionSchema)
    }
    return tool_name not in node_fn_names


def _partition_gated_calls(
    tool_calls: List[Any],
    approval_map: Dict[str, Any],
    node: Dict[str, Any],
) -> Tuple[List[Any], List[Any]]:
    """Split a tool-call batch into (gated, ungated) for HITL — see
    :func:`is_approval_gated` for the shadowing rule."""
    gated = [
        c for c in tool_calls if is_approval_gated(c.function_name, approval_map, node)
    ]
    ungated = [
        c
        for c in tool_calls
        if not is_approval_gated(c.function_name, approval_map, node)
    ]
    return gated, ungated


@dataclass
class _PreparedTools:
    """Per-turn tool surface shared by ``run_turn`` and ``run_approval_turn``."""

    flow_config: Dict[str, Any]
    global_funcs: List[FlowsFunctionSchema]
    tool_retention: Optional[Dict[str, str]]
    tool_projection: Optional[Dict[str, List[str]]]


@dataclass
class _KbMessage:
    """This turn's ephemeral knowledge base message + where it seeds.

    ``prefix`` = right after task messages (full injection, stable across
    turns → prompt-cache friendly). ``tail`` = just before the user turn
    (per-turn retrieved chunks). Never persisted to chat_message.
    """

    message: Dict[str, Any]
    placement: str  # "prefix" | "tail"


def _tools_schema(
    node: Dict[str, Any], global_funcs: List[FlowsFunctionSchema]
) -> ToolsSchema:
    """ToolsSchema concatenating per-node ``functions`` with globals.

    ``FlowsFunctionSchema.to_function_schema()`` strips flow-only fields
    (handler, cancel_on_interruption, timeout_secs); ToolsSchema accepts
    plain FunctionSchema and FlowsDirectFunction interchangeably.

    Direct mode synthesizes a node with empty ``functions`` and lets every
    tool flow through ``global_funcs`` (which the builder populates from
    ``flow.functions``). Flow mode keeps both lists — per-node first so a
    naming collision shadows the global, matching ``_dispatch_tool_call``.
    """
    standard: List[Any] = [
        fn.to_function_schema() if isinstance(fn, FlowsFunctionSchema) else fn
        for fn in (node.get("functions") or [])
    ]
    standard.extend(fn.to_function_schema() for fn in global_funcs)
    return ToolsSchema(standard_tools=standard)


def _summarize_result(value: Any) -> Any:
    """Coerce a tool result to JSON-clean for the SSE payload. Full payload
    still lands in DB / logs."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)
