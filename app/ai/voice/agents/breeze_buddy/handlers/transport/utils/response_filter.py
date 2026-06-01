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
- Filter (with args):   ``"coinEarnHistory[?rideId==`{ride_id}`]"``
  The `` `{ride_id}` `` placeholder is replaced with a single-quoted
  JMESPath string literal (``'value'``).  Any single-quotes in the value
  are escaped as ``\'`` so they cannot break out of the string boundary —
  injection-safe while remaining compatible with the Python jmespath library.

  The old single-quoted form ``"coinEarnHistory[?rideId=='{ride_id}']"`` is
  **not supported** and raises ``ValueError`` at call time.  Migrate all
  placeholders to the backtick form.

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
                `` `{placeholder}` `` tokens in expressions before evaluation.
                Placeholders **must** be wrapped in backticks so they are
                substituted as JMESPath JSON literals via ``json.dumps``,
                preventing injection via LLM-controlled argument values.

                Example: ``"coinEarnHistory[?rideId==`{ride_id}`]"``

                Using the old single-quoted form (``'{ride_id}'``) raises a
                ``ValueError`` — migrate to the backtick form.

    Returns:
        A dict containing only the extracted fields, keyed by their
        LLM-facing names. Fields whose expression resolves to ``None`` are
        omitted from the result.

    Raises:
        ValueError: If a placeholder is found in the unsupported single-quoted
            form ``'{key}'`` instead of the required backtick form `` `{key}` ``.

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
            {"coinEarnHistory": "coinEarnHistory[?rideId==`{ride_id}`]"},
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
            backtick_placeholder = f"`{{{key}}}`"
            legacy_placeholder = f"{{{key}}}"

            if backtick_placeholder in expression:
                # Substitute as a single-quoted JMESPath string literal.
                # Single-quotes in the value are escaped as \' so they cannot
                # break out of the string boundary — injection-safe.
                # We intentionally avoid json.dumps here because wrapping the
                # value in backtick JSON literals (e.g. `"abc"`) causes the
                # Python jmespath library to fail string equality comparisons
                # in filter expressions (returns [] instead of matching rows).
                safe_val = str(val).replace("'", "\\'")
                expression = expression.replace(backtick_placeholder, f"'{safe_val}'")
            elif legacy_placeholder in expression:
                # Old single-quoted form is no longer supported.
                raise ValueError(
                    f"[response_filter] Unsupported placeholder syntax "
                    f"'{legacy_placeholder}' in expression {expression!r}. "
                    f"Migrate to backtick form: '`{legacy_placeholder}`'."
                )

        value = jmespath.search(expression, data)
        if value is not None:
            result[field_name] = value
    return result
