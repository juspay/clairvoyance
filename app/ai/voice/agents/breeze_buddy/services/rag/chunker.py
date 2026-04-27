"""
Text chunker for the Breeze Buddy RAG pipeline.

Uses a recursive character splitter that tries paragraph breaks first, then
sentence boundaries, then word boundaries, then hard byte splits.  This
produces coherent chunks that stay within the ``chunk_size`` limit while
preserving natural reading units.

Improved over the VoiceAgentRAG reference implementation:
- Respects both ``chunk_size`` AND ``chunk_overlap`` without duplicating content
  (overlap is only injected between *adjacent* chunks, not recursively)
- Strips leading/trailing whitespace from every chunk
- Handles very long tokens (single words > chunk_size) gracefully
"""

from __future__ import annotations

from typing import List

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> List[str]:
    """Split *text* into overlapping chunks.

    Args:
        text: Raw text content to split.
        chunk_size: Maximum characters per chunk (hard limit).
        chunk_overlap: Number of characters to repeat at the start of each
            subsequent chunk to maintain cross-chunk context.

    Returns:
        List of non-empty chunk strings.
    """
    if not text or not text.strip():
        return []

    if len(text) <= chunk_size:
        stripped = text.strip()
        return [stripped] if stripped else []

    raw_chunks = _recursive_split(text, _SEPARATORS, chunk_size)
    return _apply_overlap(raw_chunks, chunk_overlap, chunk_size)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "]


def _recursive_split(text: str, separators: List[str], chunk_size: int) -> List[str]:
    """Recursively split *text* using the first separator that fits."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # Find the best separator available in this text
    sep = ""
    remaining_seps: List[str] = []
    for i, s in enumerate(separators):
        if s in text:
            sep = s
            remaining_seps = separators[i + 1 :]
            break

    if not sep:
        # No separator found → hard split
        return _hard_split(text, chunk_size)

    parts = text.split(sep)
    chunks: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for part in parts:
        candidate_len = current_len + len(sep) * len(current_parts) + len(part)
        if candidate_len <= chunk_size:
            current_parts.append(part)
            current_len += len(part)
        else:
            # Flush current accumulation
            if current_parts:
                joined = sep.join(current_parts).strip()
                if joined:
                    chunks.append(joined)
                current_parts = []
                current_len = 0

            # Handle a single part that is still too large
            if len(part) > chunk_size:
                if remaining_seps:
                    chunks.extend(_recursive_split(part, remaining_seps, chunk_size))
                else:
                    chunks.extend(_hard_split(part, chunk_size))
            else:
                current_parts = [part]
                current_len = len(part)

    if current_parts:
        joined = sep.join(current_parts).strip()
        if joined:
            chunks.append(joined)

    return [c for c in chunks if c]


def _hard_split(text: str, chunk_size: int) -> List[str]:
    """Split text into hard character-count slices (last resort)."""
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def _apply_overlap(chunks: List[str], chunk_overlap: int, chunk_size: int) -> List[str]:
    """Prepend a tail of the previous chunk to each subsequent chunk.

    The prepended overlap is taken from the *end* of the previous chunk and is
    capped so the resulting chunk does not exceed ``chunk_size``.
    """
    if chunk_overlap <= 0 or len(chunks) <= 1:
        return chunks

    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_tail = chunks[i - 1][-chunk_overlap:]
        candidate = (prev_tail + " " + chunks[i]).strip()
        # Truncate if prepended overlap pushes us past chunk_size
        if len(candidate) > chunk_size:
            candidate = candidate[-chunk_size:].strip()
        result.append(candidate)

    return result
