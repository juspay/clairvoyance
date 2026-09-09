"""The shared operating core of a Buddy Assist v2 prompt.

Every production Assist template is the same skeleton with a handful of
merchant "slots" swapped: the ``## Brand identity`` block, the sizing /
selection section, the contact LinkButton example, and the guided-shopping
example phrases. ``shared_core`` strips those slots so two prompts built from
the same skeleton hash identically — the consistency guarantee the fleet,
the onboarding gate and the future re-sync all rely on.

Kept in lockstep with the ops workspace's rollout script (the script that rolled the fleet out); change both or neither.
"""

from __future__ import annotations

import hashlib
import re
from typing import Tuple

OPERATING_HEAD = "## Operating principles"
SIZING_END = "\n### UI emission"

_LINK_EXAMPLE = re.compile(r'`link=\{"label": "[^"]+", "url": "https://[^"]+"\}`\.')
_LINK_PHRASE = re.compile(r'\("I\'ve added an? [^"]+ button below"\)')
_NAMED = re.compile(r"- \*\*Named product or line\*\* \([^)]*\)")
_BROAD = re.compile(r"- \*\*Broad ask\*\* \([^)]*\)")
_AXES = re.compile(r"ONE grounded question \([^)]*\)")
_COMPLETES = re.compile(r"\(checkout, or the one category that [^)]*\)")
_CHIPS = re.compile(r"narrowing filters right after a category \([^)]*\)")


def split_prompt(prompt: str) -> Tuple[str, str]:
    """(brand block, operating block) of a v2 prompt.

    Raises ``ValueError`` when the prompt is not a v2 skeleton.
    """
    index = prompt.find(OPERATING_HEAD)
    if index < 0:
        raise ValueError("prompt has no operating block")
    return prompt[:index], prompt[index:]


def shared_core(prompt: str) -> str:
    """The operating block with every merchant slot normalized."""
    _, op = split_prompt(prompt)
    sizing_end = op.find(SIZING_END)
    if sizing_end >= 0:
        sizing_start = op.rfind("\n### ", 0, sizing_end)
        if sizing_start >= 0:
            op = op[:sizing_start] + "\n<SIZING>" + op[sizing_end:]
    op = _LINK_EXAMPLE.sub("<LINK>", op)
    op = _LINK_PHRASE.sub("<LINKPHRASE>", op)
    op = _NAMED.sub("- **Named product or line** <NAMED>", op)
    op = _BROAD.sub("- **Broad ask** <BROAD>", op)
    op = _AXES.sub("ONE grounded question <AXES>", op)
    op = _COMPLETES.sub("(checkout, or the one category that <COMPLETES>)", op)
    op = _CHIPS.sub("narrowing filters right after a category <CHIPS>", op)
    return op


def core_hash(prompt: str) -> str:
    """Short, stable fingerprint of ``shared_core`` (what the fleet table shows)."""
    return hashlib.sha256(shared_core(prompt).encode()).hexdigest()[:12]


__all__ = ["OPERATING_HEAD", "core_hash", "shared_core", "split_prompt"]
