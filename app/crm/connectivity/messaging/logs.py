"""Log helpers for messaging — today one: a bounded list of message ids
for a log line.

Used by the dispatcher (outbound/dispatch.py) and by the sweep that closes
abandoned replies (conversations/abandoned.py).
"""

from typing import Sequence

# Batch size is operator-configurable and the sweeps are unbounded, so never
# interpolate a whole id list into a log line — one bad tick would emit a
# multi-megabyte record.
LOG_ID_SAMPLE = 100


def sample_ids(ids: Sequence[str], limit: int = LOG_ID_SAMPLE) -> str:
    """Render ids for a log line, bounded. The full set is always recoverable
    from the rows themselves; an unbounded log line is not worth it."""
    if len(ids) <= limit:
        return ", ".join(ids)
    return f"{', '.join(ids[:limit])} … +{len(ids) - limit} more"
