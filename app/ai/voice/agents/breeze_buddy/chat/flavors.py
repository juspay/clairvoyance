"""Flavor scoping — which registered vocabulary applies to THIS session.

Flavors (commerce today) register their vocabulary into process-global
registries at lazy import time: step labels, result summarizers, tool
annotations, verifiers, result annotators, selector transforms. That is
the right lifecycle — a worker imports a flavor once and every session on
a template that enables it pays nothing further — but a process-global
registry read process-globally is a leak: the FIRST commerce template to
warm a worker changes tool behaviour for every OTHER merchant sharing
that process, none of whom enabled the flavor.

This module is the gate. Two independent things have to be true before a
flavor's entry applies to a tool call:

1. **The template enabled the group.** Every flavor registry is keyed by
   catalog group (``configurations.ui_catalog.enabled_groups``), the same
   key the render_ui pack and prompt section already use, and every
   resolver consults only the groups in the caller's :class:`FlavorScope`.
   A session with no flavor groups sees engine defaults, always.

2. **The entry matches the tool by ROLE, not by name.** A flavor names
   its tools ("search_catalog"), but a template may bind that role to a
   different tool via ``configurations.ui_intents.tools`` — and then the
   flavor's verifier, annotation and step label silently stopped
   applying, because the registries were keyed on the flavor's default
   name while dispatch used the template's. Flavors register a role map
   here; the scope inverts it, so a remapped tool keeps its metadata.

A registry key is therefore EITHER a role or a literal tool name, and the
two live in separate namespaces: role keys are written :func:`role_key`
(``"role:search"``), literal keys are the bare tool name. Keeping them
apart is not cosmetic — a flavor's role name is not always its tool name
(commerce's ``search`` role binds to ``search_catalog``), so a shared
namespace would let a merchant's unrelated tool that happens to be called
``search`` inherit the catalog role's verifier, annotation and label.

The scope is resolved ONCE per turn (the agent holds it) and passed down.
Nothing here is cached across templates: the role map depends on template
config, and templates change under a live process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from app.core.logger import logger

# ``template -> {role: tool_name}`` for one flavor group. Called once per
# turn per enabled group, so it must be cheap and side-effect free.
RoleMapFn = Callable[[Any], Mapping[str, str]]

_ROLE_MAPS: Dict[str, RoleMapFn] = {}

# Namespace marker for role-keyed registry entries. Roles and tool names
# share a key space otherwise, and they are NOT interchangeable: a role is
# a job the template binds to a tool, so ``search`` (commerce's catalog
# role, bound to ``search_catalog``) must never be matched by a merchant's
# unrelated tool that happens to be named ``search``.
ROLE_PREFIX = "role:"


def role_key(role: str) -> str:
    """The registry key for ``role``.

    Flavors write ``role_key(ROLE_SEARCH)`` for entries that follow the
    template's binding and a bare tool name for entries that do not — the
    distinction is explicit at the registration site precisely because
    getting it wrong is silent.
    """
    return f"{ROLE_PREFIX}{role}"


def register_flavor_roles(group: str, fn: RoleMapFn) -> None:
    """Declare how ``group`` binds its tool ROLES for a given template.

    Called by a flavor package at (lazy) import time, alongside its other
    registrations. Idempotent — same-key overwrite on re-import.

    A group that registers no role map still works: its registry entries
    then match tool names literally, which is exactly the behaviour of a
    flavor whose tools are not template-rebindable.
    """
    _ROLE_MAPS[group] = fn


@dataclass(frozen=True)
class FlavorScope:
    """The flavor vocabulary one session may see.

    ``groups`` is the template's enabled catalog groups, in order —
    resolution walks them and the first hit wins, mirroring
    ``resolve_render_ui_flavor_pack``. ``roles`` maps ``(group,
    tool_name) -> role`` and ``tools`` its inverse; both are derived from
    the registered role maps under one specific template.
    """

    groups: Tuple[str, ...] = ()
    roles: Mapping[Tuple[str, str], str] = field(default_factory=dict)
    tools: Mapping[Tuple[str, str], str] = field(default_factory=dict)

    def role_for(self, group: str, tool_name: str) -> Optional[str]:
        """The role ``tool_name`` is bound to in ``group``, if any."""
        return self.roles.get((group, tool_name))

    def tool_for(self, role_or_name: str) -> str:
        """Resolve a :func:`role_key` to the tool this template bound it
        to; a bare tool name passes through untouched.

        Lets a flavor name its own ROLES in config-facing defaults (e.g.
        the render_ui pack's ``default_force_after``) and still point at
        whatever tool the template bound them to, without a literal name
        ever being mistaken for a role.
        """
        if not role_or_name.startswith(ROLE_PREFIX):
            return role_or_name
        role = role_or_name[len(ROLE_PREFIX) :]
        for group in self.groups:
            bound = self.tools.get((group, role))
            if bound is not None:
                return bound
        return role


# The scope of a session with no flavor enabled. Also the default for every
# resolver, so a call site that forgets to pass one degrades to engine
# behaviour rather than leaking somebody else's vocabulary.
EMPTY_SCOPE = FlavorScope()


def resolve_flavor_scope(template: Any, groups: Optional[Sequence[str]]) -> FlavorScope:
    """Build the scope for ``template`` over its enabled ``groups``.

    Fail-open per group, in the safe direction: a role map that raises
    costs that group its role bindings and never the turn. Its
    role-keyed entries then match NOTHING (only literal-keyed ones still
    resolve), and anything resolved through :meth:`FlavorScope.tool_for`
    — the forced render_ui think-step — goes unbound. The flavor loses
    vocabulary; no other template gains any.
    """
    if not groups:
        return EMPTY_SCOPE
    roles: Dict[Tuple[str, str], str] = {}
    tools: Dict[Tuple[str, str], str] = {}
    for group in groups:
        role_map_fn = _ROLE_MAPS.get(group)
        if role_map_fn is None:
            continue
        try:
            mapping = role_map_fn(template)
        except Exception:  # noqa: BLE001 — a flavor is never load-bearing
            logger.exception(
                f"flavors: role map for group {group!r} raised; its entries "
                "will match tool names literally for this turn"
            )
            continue
        for role, tool_name in (mapping or {}).items():
            if not isinstance(role, str) or not isinstance(tool_name, str):
                continue
            # First role wins a tool name: a template that binds two roles
            # to one tool gets the earlier role's metadata rather than an
            # arbitrary one.
            roles.setdefault((group, tool_name), role)
            tools[(group, role)] = tool_name
    return FlavorScope(groups=tuple(groups), roles=roles, tools=tools)


def scoped_keys(tool_name: str, scope: FlavorScope) -> Iterator[Tuple[str, str]]:
    """The ``(group, key)`` candidates for ``tool_name``, in priority order.

    Per enabled group: the ROLE key this template bound the tool to (when
    it bound one), then the literal tool name. Callers try each against
    their own group-keyed registry and stop at the first hit.

    The two candidates cannot collide — role keys carry
    :data:`ROLE_PREFIX` — so a tool only ever picks up a role's metadata
    by actually being bound to that role.
    """
    for group in scope.groups:
        role = scope.roles.get((group, tool_name))
        if role is not None:
            yield group, role_key(role)
        yield group, tool_name


__all__ = [
    "EMPTY_SCOPE",
    "ROLE_PREFIX",
    "FlavorScope",
    "RoleMapFn",
    "role_key",
    "register_flavor_roles",
    "resolve_flavor_scope",
    "scoped_keys",
]
