"""The retire guard slot (rollout phase 14, ADR 0023 §6) — outreach's answer
to "who would still send this template?", filled at the composition root.

(merchant_id, channel, name) -> (open runs whose PINNED document names it,
live or paused plans whose LATEST document names it). Connectivity may not
import outreach (outreach already reads this module's contracts; the reverse
arrow would close an import cycle), so the slot is filled by
app/crm/worker_main.py — the record/consumers.py inversion (checker rule 12;
modules/00 §11). Empty until then, and templates.retire() fails CLOSED on
empty: a missing registration is a wiring bug, never permission to delete
what a run is about to send.

Its own file so templates.py holds the four lifecycle transitions and nothing
else; the guard is plumbing the transitions call, not a transition.
"""

from typing import Awaitable, Callable, Optional, Tuple

RetireGuard = Callable[[str, str, str], Awaitable[Tuple[int, int]]]
_retire_guard: Optional[RetireGuard] = None


def register_retire_guard(guard: RetireGuard) -> None:
    """Idempotent: imports can run more than once (tests, reload)."""
    global _retire_guard
    _retire_guard = guard


def registered() -> bool:
    """Whether the composition root has filled the slot."""
    return _retire_guard is not None


async def workflows_naming(
    merchant_id: str, channel: str, name: str
) -> Optional[Tuple[int, int]]:
    """GATHER for retire: (open runs whose pinned document sends this
    template, live or paused plans whose latest document does) — or None
    when nothing is registered, which the caller treats as a refusal."""
    if _retire_guard is None:
        return None
    return await _retire_guard(merchant_id, channel, name)
