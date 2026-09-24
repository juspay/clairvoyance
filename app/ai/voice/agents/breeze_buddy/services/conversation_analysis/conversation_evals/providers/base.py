"""The CONVERSATION_EVALS provider contract."""

from typing import Any, Dict, Protocol


class ConversationEvalsProvider(Protocol):
    # the vocabulary word the config row's ``provider`` is matched against
    name: str

    async def judge(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
        model: str,
    ) -> Dict[str, Any]:
        """Send one state + typed questions to the vendor and return its
        parsed body (``answers``, ``model``, ...). Retries and the fail
        posture belong to the vendor client underneath; raise on failure."""
        ...

    async def close(self) -> None:
        """Drain this provider's pooled client. Called once at process
        shutdown through ``close_conversation_evals_provider_pools``; safe when never opened.
        """
        ...
