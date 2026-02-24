"""
Tracing utility helpers for Breeze Buddy.
"""

from typing import Any, Dict, List


def extract_possible_outcomes(flow: Dict[str, Any]) -> List[str]:
    """
    Extract all possible outcome values from a template flow definition.

    Walks through all nodes → functions → hooks to find every
    ``update_outcome_in_database`` hook with a static ``outcome`` field and
    collects the unique values.

    Args:
        flow: The raw template flow dict (``template.flow``).

    Returns:
        Deduplicated list of outcome strings defined in the template.
    """
    outcomes: list[str] = []
    seen: set[str] = set()

    for node in flow.get("nodes", []):
        for func in node.get("functions", []):
            for hook in func.get("hooks", []):
                if hook.get("name") != "update_outcome_in_database":
                    continue
                expected_fields = hook.get("expected_fields", {})
                outcome_field = expected_fields.get("outcome", {})
                if outcome_field.get("source") == "static" and outcome_field.get(
                    "value"
                ):
                    value = outcome_field["value"]
                    if value not in seen:
                        seen.add(value)
                        outcomes.append(value)

    return outcomes
