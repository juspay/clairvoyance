"""Utility for filtering HTTP response data using JMESPath expressions.

Values in ``expected_response_schema`` are standard JMESPath expressions.
See https://jmespath.org for full syntax reference.

Common patterns
---------------
- Simple field:         ``"status"``
- Nested field:         ``"order.status"``
- Array index:          ``"results[0].name"``
- Array wildcard:       ``"items[*].name"``
- Multi-field project:  ``"rides[*].{rideId: rideId, area: pickup.area}"``
- Filter (with args):   ``"coinEarnHistory[?rideId=='{ride_id}']"``
  The ``{ride_id}`` placeholder is resolved from the ``args`` dict via
  ``str.format(**args)`` before the JMESPath expression is evaluated.

When ``expected_response_schema`` is empty the full response is returned
unchanged (backward-compatible passthrough).
"""

from typing import Any, Dict, Optional

import jmespath


def apply_response_schema(
    data: Any,
    schema: Dict[str, str],
    args: Optional[Dict[str, Any]] = None,
) -> Any:
    """Extract fields from *data* according to *schema*.

    Args:
        data:   The parsed HTTP response (dict, list, or scalar).
        schema: Mapping of ``{llm_field_name: jmespath_expression}``.
                If empty, *data* is returned unchanged (passthrough).
        args:   The LLM-provided function call arguments. Used to resolve
                ``{placeholder}`` tokens in expressions before evaluation
                (e.g. ``"coinEarnHistory[?rideId=='{ride_id}']"``).

    Returns:
        A dict containing only the extracted fields, keyed by their
        LLM-facing names. Fields whose expression resolves to ``None`` are
        omitted from the result.

    Examples::

        apply_response_schema(
            {"order": {"status": "shipped", "id": 42}},
            {"order_status": "order.status"},
        )
        # → {"order_status": "shipped"}

        apply_response_schema(
            {"rides": [{"rideId": "abc", "pickup": {"area": "Koramangala"}}]},
            {"rides": "rides[*].{rideId: rideId, pickupArea: pickup.area}"},
        )
        # → {"rides": [{"rideId": "abc", "pickupArea": "Koramangala"}]}

        apply_response_schema(
            {"coinEarnHistory": [{"rideId": "abc", "coins": 20}, {"rideId": "xyz", "coins": 30}]},
            {"coinEarnHistory": "coinEarnHistory[?rideId=='{ride_id}']"},
            args={"ride_id": "abc"},
        )
        # → {"coinEarnHistory": [{"rideId": "abc", "coins": 20}]}
    """
    if not schema:
        return data  # type: ignore[return-value]

    resolved_args: Dict[str, Any] = args or {}
    result: Dict[str, Any] = {}
    for field_name, expression in schema.items():
        for key, val in resolved_args.items():
            expression = expression.replace(f"{{{key}}}", str(val))
        value = jmespath.search(expression, data)
        if value is not None:
            result[field_name] = value
    return result
