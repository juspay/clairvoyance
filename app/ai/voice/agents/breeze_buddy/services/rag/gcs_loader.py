"""
GCS Document Loader for Breeze Buddy RAG.

Downloads all supported files from a GCS bucket/prefix and chunks them for
embedding.  PDF extraction uses pdfminer.six (optional) when available;
falls back to raw text for plain/markdown files.
"""

from __future__ import annotations

import io
import json
from typing import List, Optional

from google.cloud import storage
from google.oauth2 import service_account

from app.ai.voice.agents.breeze_buddy.services.rag.chunker import chunk_text
from app.ai.voice.agents.breeze_buddy.services.rag.types import DocumentChunk
from app.core.config.static import GCS_CREDENTIALS_JSON
from app.core.logger import logger


def _get_storage_client(credentials_json: Optional[str] = None) -> storage.Client:
    """Build a GCS client from service-account JSON.

    Args:
        credentials_json: JSON string for a GCS service account.
            Defaults to the ``GCS_CREDENTIALS_JSON`` env var.

    Returns:
        Authenticated GCS client.
    """
    creds_json = credentials_json or GCS_CREDENTIALS_JSON
    if not creds_json:
        raise RuntimeError(
            "GCS_CREDENTIALS_JSON env var is not set – cannot connect to GCS."
        )
    creds_dict = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(creds_dict)
    return storage.Client(credentials=credentials, project=creds_dict.get("project_id"))


def _extract_text_from_blob(blob: storage.Blob, extension: str) -> Optional[str]:
    """Download a GCS blob and extract its text content.

    Args:
        blob: GCS Blob object.
        extension: Lowercased file extension (e.g. '.pdf', '.txt').

    Returns:
        Extracted text or ``None`` if the file could not be read.
    """
    raw_bytes = blob.download_as_bytes()

    if extension == ".pdf":
        try:
            from pdfminer.high_level import extract_text_to_fp
            from pdfminer.layout import LAParams

            output = io.StringIO()
            extract_text_to_fp(
                io.BytesIO(raw_bytes),
                output,
                laparams=LAParams(),
                output_type="text",
                codec="utf-8",
            )
            return output.getvalue()
        except ImportError:
            logger.warning(
                "pdfminer.six is not installed — skipping PDF file: %s", blob.name
            )
            return None
        except Exception as exc:
            logger.warning("Failed to extract PDF %s: %s", blob.name, exc)
            return None

    # Plain-text / markdown / rst
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw_bytes.decode("latin-1")
        except Exception as exc:
            logger.warning("Cannot decode %s: %s", blob.name, exc)
            return None


def load_gcs_documents(
    gcs_bucket: str,
    gcs_prefix: str = "",
    extensions: Optional[List[str]] = None,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    credentials_json: Optional[str] = None,
) -> List[DocumentChunk]:
    """Download and chunk all knowledge files from GCS.

    Args:
        gcs_bucket: GCS bucket name.
        gcs_prefix: Path prefix inside the bucket.
        extensions: Supported file extensions to ingest.
        chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlap between adjacent chunks.
        credentials_json: Service-account JSON (defaults to env var).

    Returns:
        List of ``DocumentChunk`` objects ready for embedding.
    """
    _exts: set[str] = (
        {".txt", ".md", ".rst", ".text", ".pdf"}
        if extensions is None
        else {e.lower() for e in extensions}
    )

    client = _get_storage_client(credentials_json)
    bucket = client.bucket(gcs_bucket)

    # Normalise prefix: GCS list expects no leading slash, trailing slash is fine
    prefix = gcs_prefix.lstrip("/")

    blobs = list(bucket.list_blobs(prefix=prefix))
    logger.info(
        "GCS loader: found %d blobs under gs://%s/%s",
        len(blobs),
        gcs_bucket,
        prefix,
    )

    all_chunks: List[DocumentChunk] = []

    for blob in blobs:
        name: str = blob.name
        # Skip "directory marker" blobs (end with /)
        if name.endswith("/"):
            continue

        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _exts:
            continue

        try:
            text = _extract_text_from_blob(blob, ext)
        except Exception as exc:
            logger.warning("Failed to download gs://%s/%s: %s", gcs_bucket, name, exc)
            continue

        if not text or not text.strip():
            logger.debug("Empty content in gs://%s/%s — skipping", gcs_bucket, name)
            continue

        chunks = chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        for i, chunk in enumerate(chunks):
            all_chunks.append(
                DocumentChunk(
                    text=chunk,
                    metadata={
                        "source": f"gs://{gcs_bucket}/{name}",
                        "chunk_index": i,
                        "file_name": name.split("/")[-1],
                    },
                )
            )

        logger.info("GCS loader: gs://%s/%s → %d chunks", gcs_bucket, name, len(chunks))

    logger.info(
        "GCS loader: total %d chunks from gs://%s/%s",
        len(all_chunks),
        gcs_bucket,
        prefix,
    )
    return all_chunks
