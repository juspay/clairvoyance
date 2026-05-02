"""Pydantic -> FieldNode introspection.

Walks ``TemplateModel`` (canonical source) recursively and emits a flat
list of :class:`FieldNode`. Deprecated fields are flagged, not hidden —
the planner decides what to do with them.

Sub-schemas that aren't reachable by recursion (because the parent typed
them as ``Dict[str, Any]`` — notably ``flow``) are introspected separately
and returned via ``SubSchema`` so flow/function specialists can look them
up when designing those structures.
"""

from __future__ import annotations

import enum
import inspect
import types
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from app.ai.text.agents.blueprint.schema.groups import group_for_path
from app.ai.text.agents.blueprint.schema.models import (
    FieldKind,
    FieldNode,
    SubSchema,
)

# Fields on TemplateModel that are server-set — Blueprint never asks about
# these, they're populated by the backend on create/update.
READ_ONLY_TEMPLATE_FIELDS: set[str] = {
    "id",
    "reseller_id",
    "merchant_id",
    "created_at",
    "updated_at",
}


def introspect_template(
    root: type[BaseModel],
    *,
    extra_sub_schemas: list[type[BaseModel]] | None = None,
) -> tuple[list[FieldNode], dict[str, SubSchema]]:
    """Walk ``root`` and return ``(fields, sub_schemas)``.

    Args:
        root: Top-level template model — usually ``TemplateModel``.
        extra_sub_schemas: Additional BaseModel classes to introspect as
            standalone sub-schemas (e.g. ``FlowNodeModel``,
            ``GlobalHttpFunction``). Their fields are NOT merged into the
            top-level list — they live under ``graph.sub_schemas[Name]``.

    Returns:
        Tuple of (flat field list rooted at ``root``'s fields,
        sub-schema dict keyed by class name).
    """
    fields: list[FieldNode] = []
    _walk_model(root, prefix="", out=fields, skip=READ_ONLY_TEMPLATE_FIELDS)

    sub: dict[str, SubSchema] = {}
    for cls in extra_sub_schemas or []:
        sub_fields: list[FieldNode] = []
        # Sub-schemas are walked with their own class name as the path
        # prefix, so paths like "FlowNodeModel.node_name" are unambiguous.
        _walk_model(cls, prefix=cls.__name__, out=sub_fields, skip=set())
        sub[cls.__name__] = SubSchema(
            name=cls.__name__,
            description=_clean_doc(cls.__doc__),
            fields=sub_fields,
        )

    return fields, sub


# ---------------------------------------------------------------------------
# Core walker
# ---------------------------------------------------------------------------


def _walk_model(
    model: type[BaseModel],
    *,
    prefix: str,
    out: list[FieldNode],
    skip: set[str],
) -> None:
    """Append one FieldNode per field of ``model`` to ``out``.

    Recurses into nested BaseModel fields. Non-BaseModel fields are emitted
    as a leaf with the appropriate :class:`FieldKind`.
    """
    for field_name, info in model.model_fields.items():
        if field_name in skip:
            continue

        path = f"{prefix}.{field_name}" if prefix else field_name
        node = _analyze_field(path, info)
        out.append(node)

        # If this field is a nested BaseModel, recurse for its children too.
        if node.kind == FieldKind.NESTED and node.nested_model:
            nested_cls = _extract_base_model(info.annotation)
            if nested_cls is not None:
                _walk_model(nested_cls, prefix=path, out=out, skip=set())


def _analyze_field(path: str, info: FieldInfo) -> FieldNode:
    """Build a FieldNode from a single Pydantic FieldInfo."""
    annotation = info.annotation
    description = info.description
    deprecated = _looks_deprecated(description)
    required = info.is_required()
    default = None if info.default is PydanticUndefined else info.default

    # Strip Optional[T] -> T for kind detection.
    unwrapped = _unwrap_optional(annotation)
    kind, enum_values, nested_name, item_kind = _classify(unwrapped)

    return FieldNode(
        path=path,
        kind=kind,
        py_type=_render_type(annotation),
        required=required,
        default=default,
        enum_values=enum_values,
        nested_model=nested_name,
        item_kind=item_kind,
        description=description,
        deprecated=deprecated,
        group=group_for_path(path),
    )


# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------


def _classify(
    annotation: Any,
) -> tuple[FieldKind, list[str] | None, str | None, FieldKind | None]:
    """Return (kind, enum_values, nested_model_name, item_kind)."""
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Literal[...] -> enum-like
    if origin is Literal:
        return FieldKind.ENUM, [str(a) for a in args], None, None

    # Enum subclass
    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        return (
            FieldKind.ENUM,
            [str(v.value) for v in annotation],
            None,
            None,
        )

    # BaseModel subclass -> nested
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return FieldKind.NESTED, None, annotation.__name__, None

    # list[T]
    if origin in (list,):
        item = args[0] if args else Any
        item_kind, _, _, _ = _classify(_unwrap_optional(item))
        return FieldKind.LIST, None, _item_nested_name(item), item_kind

    # dict[K, V]
    if origin in (dict,):
        val = args[1] if len(args) >= 2 else Any
        if val is Any:
            return FieldKind.OPAQUE, None, None, None
        val_kind, _, _, _ = _classify(_unwrap_optional(val))
        return FieldKind.DICT, None, _item_nested_name(val), val_kind

    # Union (other than Optional which was already unwrapped)
    if origin in (Union, types.UnionType):
        # Distinguish "Union of all BaseModels" (discriminated) from mixed.
        nested_names = [
            a.__name__ for a in args if inspect.isclass(a) and issubclass(a, BaseModel)
        ]
        if nested_names:
            return FieldKind.UNION, nested_names, None, None
        return FieldKind.UNION, [_render_type(a) for a in args], None, None

    # Primitive / Any
    if annotation in (str, int, float, bool):
        return FieldKind.PRIMITIVE, None, None, None
    if annotation is Any:
        return FieldKind.OPAQUE, None, None, None

    # Fallback — treat as opaque but keep the type string for the LLM.
    return FieldKind.OPAQUE, None, None, None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``Optional[T]`` / ``T | None`` down to ``T``. No-op otherwise."""
    origin = get_origin(annotation)
    if origin not in (Union, types.UnionType):
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return args[0]
    # Preserve real (non-Optional) unions — the caller uses get_origin on
    # the returned type, so we need to return a real union type back.
    return Union[tuple(args)]  # type: ignore[return-value]


def _extract_base_model(annotation: Any) -> type[BaseModel] | None:
    """Pull the BaseModel class out of ``Optional[SomeModel]`` etc."""
    unwrapped = _unwrap_optional(annotation)
    if inspect.isclass(unwrapped) and issubclass(unwrapped, BaseModel):
        return unwrapped
    return None


def _item_nested_name(item: Any) -> str | None:
    inner = _unwrap_optional(item)
    if inspect.isclass(inner) and issubclass(inner, BaseModel):
        return inner.__name__
    return None


def _render_type(annotation: Any) -> str:
    try:
        return repr(annotation)
    except Exception:
        return str(annotation)


def _looks_deprecated(description: str | None) -> bool:
    if not description:
        return False
    return "DEPRECATED" in description.upper()


def _clean_doc(doc: str | None) -> str | None:
    if not doc:
        return None
    return inspect.cleandoc(doc)


__all__ = [
    "READ_ONLY_TEMPLATE_FIELDS",
    "introspect_template",
]
