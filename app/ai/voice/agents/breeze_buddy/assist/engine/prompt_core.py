"""The shared operating core of a prompt, for any vertical's skeleton.

Every production Assist template of one vertical is the same skeleton with
a handful of merchant slots swapped. ``shared_core`` strips those slots
(as the vertical's ``SkeletonSpec`` describes them) so two prompts built
from the same skeleton hash identically — the consistency guarantee the
fleet, the onboarding gate and re-sync rely on. Nothing here knows what a
slot says; the commerce vertical's patterns live in
``assist/commerce/skeleton.py``.
"""

from __future__ import annotations

import hashlib
from typing import Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import SkeletonSpec


def split_prompt(prompt: str, skeleton: SkeletonSpec) -> Tuple[str, str]:
    """(brand block, operating block); ``ValueError`` when not this skeleton."""
    index = prompt.find(skeleton.operating_head)
    if index < 0:
        raise ValueError("prompt has no operating block")
    return prompt[:index], prompt[index:]


def shared_core(prompt: str, skeleton: SkeletonSpec) -> str:
    """The operating block with every merchant slot normalized."""
    _, op = split_prompt(prompt, skeleton)
    if skeleton.vertical_section_end:
        section_end = op.find(skeleton.vertical_section_end)
        if section_end >= 0:
            section_start = op.rfind("\n### ", 0, section_end)
            if section_start >= 0:
                op = op[:section_start] + "\n<VERTICAL>" + op[section_end:]
    for pattern, replacement in skeleton.slot_patterns:
        op = pattern.sub(replacement, op)
    return op


def replace_vertical_section(prompt: str, skeleton: SkeletonSpec, section: str) -> str:
    """Swap the merchant-specific help section for ``section``.

    The blueprint ships one written for whatever store it was cut from, and
    the heading is a merchant field — a shoe shop's deciding question is not a
    rug shop's. Located exactly as ``shared_core`` locates it for hashing, so
    the section the fleet table normalises away is the section replaced here.

    Returns the prompt untouched when the skeleton declares no such section or
    the blueprint does not contain one: a missing section is a blueprint that
    never had merchant help, not an error.
    """
    if not skeleton.vertical_section_end or not section.strip():
        return prompt
    end = prompt.find(skeleton.vertical_section_end)
    if end < 0:
        return prompt
    start = prompt.rfind("\n### ", 0, end)
    if start < 0:
        return prompt
    return prompt[: start + 1] + section.rstrip("\n") + "\n" + prompt[end:]


def core_hash(prompt: str, skeleton: SkeletonSpec) -> str:
    """Short, stable fingerprint of ``shared_core`` (what the fleet table shows)."""
    return hashlib.sha256(shared_core(prompt, skeleton).encode()).hexdigest()[:12]


__all__ = [
    "core_hash",
    "replace_vertical_section",
    "shared_core",
    "split_prompt",
]
