"""
Source-type-aware chunking.

Prose (pdf/docx/md/txt): heading-aware recursive splitting to ~450 tokens
with sentence-level overlap; paragraphs are never split mid-way unless a
single paragraph alone exceeds the hard cap, so Q&A pairs and numbered
procedures stay intact (Retell guidance for voice KBs).

Tabular (csv/xlsx/sheets): one row = one chunk with column headers prepended
("Product: X | Price: 499") plus one sheet-summary chunk (Onyx pattern) --
paired with the keyword leg of hybrid search this is what makes exact
prices/SKUs retrievable.
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.services.knowledge_base.types import (
    NormalizedContent,
    PreparedChunk,
    TableData,
)

TARGET_CHUNK_TOKENS = 450
OVERLAP_TOKENS = 50
MAX_CHUNK_TOKENS = 900

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Western/Devanagari enders need trailing whitespace; CJK/Arabic enders are
# split points on their own (CJK prose has no space after 。！？).
_SENTENCE_RE = re.compile(r"(?<=[.!?।])\s+|(?<=[。！？؟۔])")

_encoder = None
_encoder_failed = False


def count_tokens(text: str) -> int:
    """Token count via tiktoken (cl100k_base), heuristic fallback if the
    encoding files can't be loaded (offline/restricted environments)."""
    global _encoder, _encoder_failed
    if not _encoder_failed and _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            _encoder_failed = True
            logger.warning(f"tiktoken unavailable, using len/4 heuristic: {e}")
    if _encoder is not None:
        try:
            return len(_encoder.encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _hash(text: str) -> str:
    """Stable SHA-256 fingerprint of chunk text.

    This is the identity used by ingestion's hash-diff: same text -> same
    hash -> chunk is skipped instead of re-embedded on re-sync.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_chunk(text: str, metadata: Dict[str, Any]) -> PreparedChunk:
    """Wrap final chunk text into a PreparedChunk (token count + hash).

    Single construction point so every chunk — prose, table row, summary —
    gets its fingerprint and token count computed the same way.
    """
    return PreparedChunk(
        text=text,
        metadata=metadata,
        token_count=count_tokens(text),
        content_hash=_hash(text),
    )


def _split_sections(text: str) -> List[Tuple[str, str]]:
    """Split prose on markdown headings into (breadcrumb, body) sections.

    Text without headings becomes a single section with an empty breadcrumb.
    """
    lines = text.split("\n")
    sections: List[Tuple[str, List[str]]] = []
    breadcrumb: List[Tuple[int, str]] = []
    current: List[str] = []

    def flush():
        # Close the section being accumulated: emit (breadcrumb, body) if it
        # has any non-blank content, then reset the line buffer.
        if current and any(line.strip() for line in current):
            crumb = " > ".join(title for _, title in breadcrumb)
            sections.append((crumb, current.copy()))
        current.clear()

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            breadcrumb[:] = [(lv, t) for lv, t in breadcrumb if lv < level]
            breadcrumb.append((level, title))
        else:
            current.append(line)
    flush()

    return [(crumb, "\n".join(body).strip()) for crumb, body in sections]


def _split_paragraphs(body: str) -> List[str]:
    """Split on blank lines, keeping numbered/bulleted list items glued to
    their block so procedures never split across chunks."""
    raw = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    merged: List[str] = []
    for para in raw:
        starts_listish = bool(re.match(r"^(\d+[\.\)]|[-*•])\s", para))
        if merged and starts_listish:
            prev = merged[-1]
            prev_listish = bool(
                re.match(r"^(\d+[\.\)]|[-*•])\s", prev.split("\n")[-1].strip())
            ) or prev.rstrip().endswith(":")
            if prev_listish and count_tokens(prev) < MAX_CHUNK_TOKENS:
                merged[-1] = prev + "\n" + para
                continue
        merged.append(para)
    return merged


def _hard_split(text: str) -> List[str]:
    """Last-resort halving for text with no usable sentence boundaries
    (minified blobs, giant spreadsheet cells, unpunctuated prose).

    Guarantees every piece fits MAX_CHUNK_TOKENS regardless of language —
    embedding APIs hard-reject single inputs past their token cap (8192 for
    OpenAI), which would otherwise fail the whole document.
    """
    if count_tokens(text) <= MAX_CHUNK_TOKENS:
        return [text]
    mid = len(text) // 2
    return _hard_split(text[:mid].strip()) + _hard_split(text[mid:].strip())


def _split_oversized(paragraph: str) -> List[str]:
    """Sentence-split a single paragraph that alone exceeds MAX_CHUNK_TOKENS.

    Normal paragraphs are never cut (Q&A pairs / procedures stay intact);
    this is the escape hatch for wall-of-text paragraphs. Pieces target
    TARGET_CHUNK_TOKENS; anything still oversized (no sentence boundaries
    at all) falls through to ``_hard_split``. Called from ``chunk_prose``.
    """
    sentences = [s for s in _SENTENCE_RE.split(paragraph) if s and s.strip()]
    pieces: List[str] = []
    buf: List[str] = []
    buf_tokens = 0
    for sentence in sentences:
        tokens = count_tokens(sentence)
        if buf and buf_tokens + tokens > TARGET_CHUNK_TOKENS:
            pieces.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(sentence)
        buf_tokens += tokens
    if buf:
        pieces.append(" ".join(buf))
    # A "sentence" with no boundaries at all can still exceed the hard cap.
    capped: List[str] = []
    for piece in pieces:
        capped.extend(_hard_split(piece))
    return capped


def chunk_prose(
    text: str, base_metadata: Optional[Dict[str, Any]] = None
) -> List[PreparedChunk]:
    """Heading-aware chunking of prose text (pdf/docx/md/txt content).

    Pipeline per section: split on markdown headings (breadcrumb kept as a
    ``Section:`` prefix + metadata), pack paragraphs to ~TARGET_CHUNK_TOKENS,
    carry sentence-tail overlap between adjacent chunks of a section.
    Called from ``build_chunks`` for a connector's ``NormalizedContent.text``.
    """
    base_metadata = base_metadata or {}
    chunks: List[PreparedChunk] = []

    for crumb, body in _split_sections(text):
        paragraphs: List[str] = []
        for para in _split_paragraphs(body):
            if count_tokens(para) > MAX_CHUNK_TOKENS:
                paragraphs.extend(_split_oversized(para))
            else:
                paragraphs.append(para)

        buf: List[str] = []
        buf_tokens = 0

        def flush_buf():
            # Emit the buffered paragraphs as one chunk, then seed the next
            # buffer with a sentence-tail overlap (~OVERLAP_TOKENS) so a
            # thought spanning the boundary is present in both chunks. The
            # final flush below deliberately does NOT reuse this: it must
            # not seed overlap after the last chunk of a section.
            nonlocal buf, buf_tokens
            if not buf:
                return
            body_text = "\n\n".join(buf)
            prefix = f"Section: {crumb}\n\n" if crumb else ""
            metadata = dict(base_metadata)
            if crumb:
                metadata["section"] = crumb
            chunks.append(_make_chunk(prefix + body_text, metadata))
            # Sentence-tail overlap into the next chunk of the same section.
            tail = buf[-1]
            tail_sentences = _SENTENCE_RE.split(tail)
            overlap: List[str] = []
            overlap_tokens = 0
            for sentence in reversed(tail_sentences):
                overlap_tokens += count_tokens(sentence)
                if overlap_tokens > OVERLAP_TOKENS:
                    break
                overlap.insert(0, sentence)
            buf = [" ".join(overlap)] if overlap else []
            buf_tokens = count_tokens(buf[0]) if buf else 0

        for para in paragraphs:
            tokens = count_tokens(para)
            if buf and buf_tokens + tokens > TARGET_CHUNK_TOKENS:
                flush_buf()
            buf.append(para)
            buf_tokens += tokens
        # Final flush without seeding overlap.
        if buf:
            body_text = "\n\n".join(buf)
            prefix = f"Section: {crumb}\n\n" if crumb else ""
            metadata = dict(base_metadata)
            if crumb:
                metadata["section"] = crumb
            chunks.append(_make_chunk(prefix + body_text, metadata))

    return chunks


def chunk_table(table: TableData) -> List[PreparedChunk]:
    """Row-as-chunk with headers prepended, plus one sheet-summary chunk."""
    chunks: List[PreparedChunk] = []
    headers = [h.strip() for h in table.headers]

    non_empty_rows = 0
    for row_index, row in enumerate(table.rows):
        cells = [str(cell).strip() if cell is not None else "" for cell in row]
        if not any(cells):
            continue
        non_empty_rows += 1
        pairs = [
            f"{header}: {cell}"
            for header, cell in zip(headers, cells)
            if header and cell
        ]
        if not pairs:
            continue
        text = (
            f"{table.name} — " + " | ".join(pairs) if table.name else " | ".join(pairs)
        )
        row_key = f"{table.name}:{row_index}"
        row_hash = _hash("|".join(cells))
        # A pathological row (multi-MB cell) must not exceed the embedding
        # API's per-input cap; split parts share row_key/row_hash so the
        # incremental diff still treats the row as one unit.
        for part_index, part in enumerate(_hard_split(text)):
            metadata: Dict[str, Any] = {
                "table": table.name,
                "row_key": row_key,
                "row_hash": row_hash,
            }
            if part_index:
                metadata["row_part"] = part_index
            chunks.append(_make_chunk(part, metadata))

    if headers and non_empty_rows:
        summary = (
            f"Table '{table.name}' contains {non_empty_rows} rows with columns: "
            + ", ".join(h for h in headers if h)
            + "."
        )
        chunks.insert(0, _make_chunk(summary, {"table": table.name, "summary": True}))

    return chunks


def build_chunks(content: NormalizedContent) -> List[PreparedChunk]:
    """Chunk a connector's normalized output (prose and/or tables)."""
    chunks: List[PreparedChunk] = []
    if content.text and content.text.strip():
        chunks.extend(chunk_prose(content.text))
    for table in content.tables:
        chunks.extend(chunk_table(table))
    return chunks
