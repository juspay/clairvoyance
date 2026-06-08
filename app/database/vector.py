"""Shared pgvector/halfvec SQL serialization helpers."""

from typing import Sequence


def vector_literal(embedding: Sequence[float]) -> str:
    """Serialize a vector for a parameter cast without asyncpg codecs."""
    return "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"
