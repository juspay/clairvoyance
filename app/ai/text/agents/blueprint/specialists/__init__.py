"""Pure-function utilities for Blueprint template validation.

No LLM specialists — the turn handler does all LLM work directly.
Only two pure functions survive:

* :func:`find_validation_issues` — coupling + required-field checks.
* :func:`lint_template` — 24-point pre-finalize linter with auto-fixes.
"""

from __future__ import annotations

from app.ai.text.agents.blueprint.specialists.template_linter import (
    LintResult,
    lint_template,
)
from app.ai.text.agents.blueprint.specialists.validator import (
    find_validation_issues,
)

__all__ = [
    "LintResult",
    "find_validation_issues",
    "lint_template",
]
