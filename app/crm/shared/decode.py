"""Row-decoding primitives every module's db/decoder.py needs.

Both TOTAL: they answer for any input the driver can hand them and never
raise. That is why they live here rather than being reimplemented per
module — a decoder that raises on one malformed row strands the whole
claimed batch, which is then reclaimed, decoded, and raises again forever.

Imported directly (shared/ is exempt from the contracts-only rule).
"""

import json
from typing import Any, Dict, List, Optional


def jsonb_object(value: Any) -> Dict[str, Any]:
    """A jsonb column as a dict. Anything else becomes an empty dict.

    Plain jsonb accepts scalars and arrays too, so 42 and [1, 2] are legal
    stored values. Non-objects are DROPPED, not converted: dict() would turn
    [["a", 1]] into {"a": 1} and invent a template variable nobody wrote.

    The driver returns jsonb as a string today; the non-string branch covers
    a codec being registered later.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def jsonb_list(value: Any) -> List[Any]:
    """A jsonb column as a list. Anything else becomes an empty list.

    Same totality contract as ``jsonb_object`` and the same reason for it:
    template components are decoded in the sync loop's batch, where one raise
    would strand every row alongside it. A stored object or scalar is DROPPED
    rather than wrapped — [42] is not what "42" meant, and a caller reading
    components has already handled the empty case.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return list(value) if isinstance(value, list) else []


def uuid_or_none(value: Any) -> Optional[str]:
    """A uuid column as a string, preserving NULL as None — asyncpg hands
    back UUID objects, schemas type these as Optional[str]."""
    return str(value) if value is not None else None
