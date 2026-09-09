"""The commerce vertical's prompt skeleton (Beyond Bound v2).

Merchant slots inside the shared operating block: the contact LinkButton
example and phrase, the guided-shopping example phrases, and the sizing /
selection help section (heading varies per merchant). Kept in lockstep
with the ops rollout script that standardised the fleet.
"""

from __future__ import annotations

import re

from app.ai.voice.agents.breeze_buddy.assist.engine.skeleton import SkeletonSpec

COMMERCE_V2 = SkeletonSpec(
    id="commerce-v2",
    vertical_section_end="\n### UI emission",
    slot_patterns=(
        (
            re.compile(r'`link=\{"label": "[^"]+", "url": "https://[^"]+"\}`\.'),
            "<LINK>",
        ),
        (re.compile(r'\("I\'ve added an? [^"]+ button below"\)'), "<LINKPHRASE>"),
        (
            re.compile(r"- \*\*Named product or line\*\* \([^)]*\)"),
            "- **Named product or line** <NAMED>",
        ),
        (re.compile(r"- \*\*Broad ask\*\* \([^)]*\)"), "- **Broad ask** <BROAD>"),
        (
            re.compile(r"ONE grounded question \([^)]*\)"),
            "ONE grounded question <AXES>",
        ),
        (
            re.compile(r"\(checkout, or the one category that [^)]*\)"),
            "(checkout, or the one category that <COMPLETES>)",
        ),
        (
            re.compile(r"narrowing filters right after a category \([^)]*\)"),
            "narrowing filters right after a category <CHIPS>",
        ),
    ),
)

__all__ = ["COMMERCE_V2"]
