"""The engine contract. The fake engine (PR-01) and Nemotron (PR-03) both follow it.

Only the feeder thread may call an engine: open, step and release all touch
per-stream state that will live on the GPU.
"""

from dataclasses import dataclass
from typing import Protocol, Sequence

Key = tuple[int, int]  # (conn_id, sid)


class FatalEngineError(Exception):
    """The engine is broken for the whole process (e.g. a CUDA illegal memory
    access). Every later step would fail too, so the feeder exits the process
    instead of failing call after call (review #1)."""


@dataclass(frozen=True)
class StepKind:
    """The cache-aware model cannot mix these in one batched call."""

    first: bool  # first chunk of an utterance: different shape, no pre-encode cache
    final: bool  # flush the lookahead (keep_all_outputs) and end the utterance


@dataclass(frozen=True)
class WorkItem:
    key: Key
    kind: StepKind
    audio: bytes  # PCM16 at sample_rate; whole samples only; at most one chunk
    sample_rate: int


@dataclass(frozen=True)
class StepResult:
    key: Key
    kind: StepKind
    # The WHOLE utterance text so far; it replaces the previous text. RNNT word
    # pieces continue across steps ("kar" + "ta" = "karta"), so the caller must
    # never join step outputs itself (review #13).
    text: str


class Engine(Protocol):
    # New audio per step. Set by the model (560 ms for right context 6), never
    # configured separately, so the two can't disagree (review #17).
    chunk_ms: int

    def open(self, key: Key) -> None: ...

    def release(self, key: Key) -> None: ...

    def step(self, kind: StepKind, items: Sequence[WorkItem]) -> list[StepResult]: ...
