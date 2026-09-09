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


def core_hash(prompt: str, skeleton: SkeletonSpec) -> str:
    """Short, stable fingerprint of ``shared_core`` (what the fleet table shows)."""
    return hashlib.sha256(shared_core(prompt, skeleton).encode()).hexdigest()[:12]


__all__ = ["core_hash", "shared_core", "split_prompt"]
