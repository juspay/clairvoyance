"""Patching primitives for bulk template updates.

RFC 7386 JSON Merge Patch semantics: dict patches merge recursively, a
``None`` value deletes the key, any non-dict patch value replaces the
target outright — including arrays, which are replaced wholesale. Because
``flow.nodes`` is an array, whole-flow merge patches cannot edit one node;
``node_patches`` addresses nodes by ``node_name`` for that.
"""

import copy
from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    FlowMode,
)


def apply_merge_patch(target: Optional[Dict[str, Any]], patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result: Dict[str, Any] = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(result.get(key), value)
    return result


def apply_template_patches(
    flow: Dict[str, Any],
    configurations: Optional[Dict[str, Any]],
    flow_patch: Optional[Dict[str, Any]],
    node_patches: Optional[Dict[str, Dict[str, Any]]],
    configurations_patch: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Apply the three patch kinds to one template's current state.

    Raises ValueError when node_patches references a node_name the flow
    does not contain — bulk ops must fail loudly, not skip silently.
    """
    new_flow = copy.deepcopy(flow)
    if flow_patch:
        new_flow = apply_merge_patch(new_flow, flow_patch)

    if node_patches:
        nodes = new_flow.get("nodes") or []
        by_name = {
            n.get("node_name"): i for i, n in enumerate(nodes) if isinstance(n, dict)
        }
        for node_name, node_patch in node_patches.items():
            if node_name not in by_name:
                raise ValueError(f"node_patches references unknown node '{node_name}'")
            idx = by_name[node_name]
            nodes[idx] = apply_merge_patch(nodes[idx], node_patch)
        new_flow["nodes"] = nodes

    new_configurations = (
        copy.deepcopy(configurations) if configurations is not None else None
    )
    if configurations_patch:
        new_configurations = apply_merge_patch(
            new_configurations or {}, configurations_patch
        )

    return new_flow, new_configurations


def validate_patched_template(
    flow: Dict[str, Any],
    configurations: Optional[Dict[str, Any]],
    template_id: str,
    template_name: str,
) -> Optional[str]:
    """Same flow-shape rules as the create/replace handlers, plus a full
    ConfigurationModel validation. Returns an error message or None."""
    if not flow:
        return "Flow structure is required"
    if flow.get("mode") == FlowMode.DIRECT.value:
        if "system_prompt" not in flow:
            return "system_prompt must be specified in direct-mode flow structure"
    else:
        if "initial_node" not in flow:
            return "initial_node must be specified in flow structure"
        if "nodes" not in flow or not flow["nodes"]:
            return "nodes must be specified in flow structure"

    if configurations is not None:
        try:
            ConfigurationModel.model_validate(
                configurations,
                context={
                    "template_id": template_id,
                    "template_name": template_name,
                },
            )
        except Exception as exc:  # pydantic ValidationError and friends
            return f"configurations invalid after patch: {exc}"
    return None
