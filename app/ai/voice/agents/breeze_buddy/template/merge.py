"""Three-way merge of a family's parent-template edit into one child.

Pure functions, no I/O. See docs/TEMPLATE_LINEAGE.md §6.1: for every field
the family edit changed (parent != base), the child's current value decides
the outcome — equal to base means the merchant never customized it (auto
apply), equal to parent means it is already there (no-op), anything else is
a conflict for a human. Fields the family edit did NOT change are never
reported: the child keeps them untouched.

Field paths are the conflict unit and the address used to write the
resolution back:

    flow.<key>                        top-level flow key (never 'nodes')
    flow.nodes.<node_name>            a whole node (added / removed / absent
                                      from the child)
    flow.nodes.<node_name>.<key>      one key of a node all three sides have
    configurations.<key>              top-level configurations key
"""

import copy
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.breeze_buddy.template_version import (
    ChildMergeOutcome,
    MergeFieldChange,
    MergeFieldConflict,
)

# Distinguishes "key absent" from "key present with value None" — the
# difference between deleting a field and setting it to JSON null.
_MISSING = object()


def parse_field_path(field_path: str) -> Tuple[str, ...]:
    """Validate + split a field path. Raises ValueError if unsupported."""
    parts = tuple(field_path.split("."))
    if (
        len(parts) == 2
        and parts[0] in ("flow", "configurations")
        and parts[1]
        # 'flow.nodes' is never a leaf: nodes are addressed by node_name
        and not (parts[0] == "flow" and parts[1] == "nodes")
    ):
        return parts
    if (
        len(parts) in (3, 4)
        and parts[0] == "flow"
        and parts[1] == "nodes"
        and all(parts[2:])
    ):
        return parts
    raise ValueError(f"unsupported field_path '{field_path}'")


def _check_key(container: str, key: Any) -> None:
    if not isinstance(key, str) or not key or "." in key:
        raise ValueError(
            f"'{key}' in {container} is not addressable by the merge field-path "
            "grammar (keys and node names must be non-empty and contain no '.')"
        )


def _same(a: Any, b: Any) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    return a == b


def _nodes_by_name(flow: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    nodes = (flow or {}).get("nodes")
    if not isinstance(nodes, list):
        return {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("node_name")
        if name is None:
            continue
        # Not validated here: a child-only node (merchant addition) must
        # never abort the merge, even if its name contains a '.'. Only
        # names that can become a field path (base/parent union) are
        # validated, in _merge_nodes.
        by_name[name] = node
    return by_name


def _flow_top_level(flow: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: v for k, v in (flow or {}).items() if k != "nodes"}


def _classify(
    field_path: str,
    base: Any,
    parent: Any,
    child: Any,
    outcome: ChildMergeOutcome,
) -> None:
    if _same(base, parent):
        return  # the family edit did not touch this field
    if _same(child, parent):
        outcome.noop.append(field_path)
        return
    if _same(child, base):
        outcome.auto_apply.append(
            MergeFieldChange(field_path=field_path, op="remove", value=None)
            if parent is _MISSING
            else MergeFieldChange(field_path=field_path, op="set", value=parent)
        )
        return
    outcome.conflicts.append(
        MergeFieldConflict(
            field_path=field_path,
            base=None if base is _MISSING else base,
            parent=None if parent is _MISSING else parent,
            child=None if child is _MISSING else child,
            base_present=base is not _MISSING,
            parent_present=parent is not _MISSING,
            child_present=child is not _MISSING,
        )
    )


def _merge_dict(
    prefix: str,
    base: Dict[str, Any],
    parent: Dict[str, Any],
    child: Dict[str, Any],
    outcome: ChildMergeOutcome,
) -> None:
    for key in sorted(set(base) | set(parent)):
        _check_key(prefix, key)
        _classify(
            f"{prefix}.{key}",
            base.get(key, _MISSING),
            parent.get(key, _MISSING),
            child.get(key, _MISSING),
            outcome,
        )


def _merge_nodes(
    base: Dict[str, Dict[str, Any]],
    parent: Dict[str, Dict[str, Any]],
    child: Dict[str, Dict[str, Any]],
    outcome: ChildMergeOutcome,
) -> None:
    # Validate before sorting, not inside the loop: node_name is a JSON value,
    # so a family flow holding {"node_name": 5} would make sorted() raise
    # TypeError, which escapes the per-child `except ValueError` in the
    # propagation accessor and fails the whole preview/apply instead of
    # reporting one bad child.
    names = set(base) | set(parent)
    for name in names:
        _check_key("flow.nodes", name)
    for name in sorted(names):
        base_node = base.get(name, _MISSING)
        parent_node = parent.get(name, _MISSING)
        child_node = child.get(name, _MISSING)
        # isinstance rather than `is not _MISSING`: same test (the maps only
        # ever hold dicts) but it narrows the type for the _merge_dict call.
        if not (
            isinstance(base_node, dict)
            and isinstance(parent_node, dict)
            and isinstance(child_node, dict)
        ):
            # Added, removed, or absent from the child: the whole node is the
            # unit, so a "take parent" resolution writes a complete node.
            _classify(f"flow.nodes.{name}", base_node, parent_node, child_node, outcome)
            continue
        _merge_dict(f"flow.nodes.{name}", base_node, parent_node, child_node, outcome)


def merge_family_into_child(
    *,
    base_flow: Dict[str, Any],
    base_configurations: Optional[Dict[str, Any]],
    parent_flow: Dict[str, Any],
    parent_configurations: Optional[Dict[str, Any]],
    child_flow: Dict[str, Any],
    child_configurations: Optional[Dict[str, Any]],
) -> ChildMergeOutcome:
    """Classify every field the family edit changed, for one child.

    ``base_*`` is the family content the child last synced from (see the
    NULL-sync rule in the plan / accessor), ``parent_*`` the family's current
    content, ``child_*`` the child's own current content.
    """
    outcome = ChildMergeOutcome()
    _merge_dict(
        "flow",
        _flow_top_level(base_flow),
        _flow_top_level(parent_flow),
        _flow_top_level(child_flow),
        outcome,
    )
    _merge_nodes(
        _nodes_by_name(base_flow),
        _nodes_by_name(parent_flow),
        _nodes_by_name(child_flow),
        outcome,
    )
    _merge_dict(
        "configurations",
        base_configurations or {},
        parent_configurations or {},
        child_configurations or {},
        outcome,
    )
    return outcome


def _set_or_remove(
    container: Dict[str, Any], key: str, change: MergeFieldChange
) -> None:
    if change.op == "remove":
        container.pop(key, None)
    else:
        container[key] = copy.deepcopy(change.value)


def apply_merge_decisions(
    flow: Dict[str, Any],
    configurations: Optional[Dict[str, Any]],
    changes: List[MergeFieldChange],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Write auto-applied + resolved changes onto the child's OWN content.

    Never mutates the inputs. Raises ValueError for a node-key path whose
    node is absent from the child (the merge only emits those for nodes all
    three sides have, so this means the caller hand-built a bad path).
    """
    new_flow: Dict[str, Any] = copy.deepcopy(flow) if flow else {}
    new_configurations = (
        copy.deepcopy(configurations) if configurations is not None else None
    )
    for change in changes:
        parts = parse_field_path(change.field_path)
        if parts[0] == "configurations":
            if new_configurations is None:
                new_configurations = {}
            _set_or_remove(new_configurations, parts[1], change)
            continue
        if len(parts) == 2:
            _set_or_remove(new_flow, parts[1], change)
            continue
        node_name = parts[2]
        nodes = new_flow.get("nodes")
        if not isinstance(nodes, list):
            nodes = []
        index = next(
            (
                i
                for i, n in enumerate(nodes)
                if isinstance(n, dict) and n.get("node_name") == node_name
            ),
            None,
        )
        if len(parts) == 3:
            if change.op == "remove":
                if index is not None:
                    nodes.pop(index)
            elif index is None:
                nodes.append(copy.deepcopy(change.value))
            else:
                nodes[index] = copy.deepcopy(change.value)
        else:
            if index is None:
                raise ValueError(
                    f"cannot apply '{change.field_path}': node '{node_name}' "
                    "not in flow"
                )
            node = dict(nodes[index])
            _set_or_remove(node, parts[3], change)
            nodes[index] = node
        new_flow["nodes"] = nodes
    return new_flow, new_configurations
