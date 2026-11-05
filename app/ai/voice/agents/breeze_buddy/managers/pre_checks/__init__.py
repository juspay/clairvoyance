"""Pre-check package: go/no-go checks run before a lead is dialled.

- ``functions.py`` -- the ``internal_function`` registry (in-repo checks)
- ``http.py`` -- HTTP/MCP fetch + response-matching helpers for
  ``external_api`` checks
- ``executor.py`` -- ``run_pre_checks``, the orchestrator both paths feed into

Re-exported here so external callers keep importing from
``managers.pre_checks`` rather than reaching into a submodule.
"""

from .executor import (
    PreCheckDecision,
    PreCheckResult,
    SinglePreCheckResult,
    run_pre_checks,
)
from .functions import PRE_CHECK_FUNCTIONS, PreCheckFunctionContext

__all__ = [
    "PreCheckDecision",
    "PreCheckResult",
    "SinglePreCheckResult",
    "run_pre_checks",
    "PRE_CHECK_FUNCTIONS",
    "PreCheckFunctionContext",
]
