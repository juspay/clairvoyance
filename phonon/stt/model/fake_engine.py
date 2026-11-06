"""A deterministic engine with no model, so everything else is testable on a Mac.

Each step with audio adds one token `s<sid>c<n>` (n counts chunks within the
utterance) and returns the whole utterance text, as the contract requires. A
final resets the utterance, mirroring fresh state per utterance. It refuses
what the real model cannot do, so grouping bugs surface before PR-03.
"""

from typing import Sequence

from stt.model.engine import Key, StepKind, StepResult, WorkItem


class FakeEngine:
    def __init__(self, chunk_ms: int = 560) -> None:
        self.chunk_ms = chunk_ms
        self._tokens: dict[Key, list[str]] = {}

    def open(self, key: Key) -> None:
        self._tokens[key] = []

    def release(self, key: Key) -> None:
        self._tokens.pop(key, None)

    def step(self, kind: StepKind, items: Sequence[WorkItem]) -> list[StepResult]:
        if any(item.kind != kind for item in items):
            raise ValueError("mixed step kinds in one batch")
        results = []
        for item in items:
            if item.key not in self._tokens:
                raise KeyError(f"step for a stream that was never opened: {item.key}")
            tokens = self._tokens[item.key]
            if item.audio:
                tokens.append(f"s{item.key[1]}c{len(tokens)}")
            text = " ".join(tokens)
            if kind.final:
                tokens.clear()
            results.append(StepResult(item.key, kind, text))
        return results

    @property
    def open_streams(self) -> int:
        return len(self._tokens)
