"""
Google Sheets connector: merchant-shared spreadsheet -> NormalizedContent.

Auth model: the platform's service account; the merchant shares their sheet
with the SA email as Viewer (lowest-friction B2B pattern — no OAuth flow).
Data access is REST via the shared aiohttp factory; only token minting uses
the google-auth library (sync, run in a thread, cached until near expiry).

Sync model (see also sheets_poll.py): Sheets has no row-level diff API, so
change detection is a cheap Drive ``files.get(modifiedTime)`` probe and a
"changed" verdict triggers a full ``values.batchGet`` re-read; the ingestion
worker's chunk-hash diff then re-embeds only rows that actually changed.
"""

import asyncio
import datetime
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from app.core.config.static import (
    GCS_CREDENTIALS_JSON,
    GOOGLE_SHEETS_SA_CREDENTIALS_JSON,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.schemas.breeze_buddy.knowledge_base import KbDocument
from app.services.knowledge_base.connectors.base import KBConnector
from app.services.knowledge_base.types import NormalizedContent, TableData

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_API = "https://www.googleapis.com/drive/v3/files"

_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")

MAX_SHEET_ROWS = 10_000
MAX_SHEET_COLUMNS = 50

# Token cache: (access_token, expiry_epoch). Minting is sync google-auth
# work run in a thread; cached until 5 minutes before expiry.
_token_cache: Optional[Tuple[str, float]] = None
_token_lock = asyncio.Lock()

_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    """Lazily create/reuse the module's shared aiohttp session for all
    Google API calls (connection reuse across poller probes and loads)."""
    global _session
    if _session is None or _session.closed:
        _session = create_aiohttp_session(
            timeout=aiohttp.ClientTimeout(total=60, connect=10)
        )
    return _session


def _credentials_info() -> Dict[str, Any]:
    """Parsed service-account JSON from env.

    Dedicated ``GOOGLE_SHEETS_SA_CREDENTIALS_JSON`` wins; falls back to the
    GCS service account (same project) — that SA then also needs the
    Sheets/Drive readonly scopes granted. Raises a curated ValueError when
    neither is set (surfaced as the 503 on the connect-sheet endpoint).
    """
    raw = GOOGLE_SHEETS_SA_CREDENTIALS_JSON or GCS_CREDENTIALS_JSON
    if not raw:
        raise ValueError(
            "Google Sheets sync is not configured: set "
            "GOOGLE_SHEETS_SA_CREDENTIALS_JSON (or GCS_CREDENTIALS_JSON)"
        )
    return json.loads(raw)


def get_service_account_email() -> str:
    """The SA email merchants must share their sheet with (Viewer)."""
    return str(_credentials_info().get("client_email", ""))


def parse_spreadsheet_id(url_or_id: str) -> str:
    """Extract the spreadsheet ID from a full Sheets URL or bare ID.

    Called by the add-sheet handler on whatever the merchant pasted into
    the loom connect dialog; raises a user-facing ValueError (400) when
    neither shape matches.
    """
    value = url_or_id.strip()
    match = _SPREADSHEET_URL_RE.search(value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", value):
        return value
    raise ValueError(
        "Could not parse a spreadsheet ID from the provided value; paste the "
        "full Google Sheets URL"
    )


def _mint_token_sync() -> Tuple[str, float]:
    """Mint a fresh OAuth access token from the SA credentials (sync).

    google-auth is a sync library, so this runs via ``asyncio.to_thread``
    from ``_get_access_token``. Returns (token, expiry_epoch); a missing
    expiry assumes the standard ~1h lifetime minus safety margin.
    """
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        _credentials_info(), scopes=_SCOPES
    )
    credentials.refresh(Request())
    expiry = credentials.expiry
    expiry_epoch = (
        expiry.replace(tzinfo=datetime.timezone.utc).timestamp()
        if expiry
        else datetime.datetime.now(datetime.timezone.utc).timestamp() + 3000
    )
    return str(credentials.token), expiry_epoch


async def _get_access_token() -> str:
    """Cached SA access token, re-minted when <5 minutes from expiry.

    Double-checked locking: the cheap check outside the lock serves the
    common case without contention; the re-check inside prevents a
    thundering herd of token mints when many probes race at expiry.
    """
    global _token_cache
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if _token_cache and _token_cache[1] - now > 300:
        return _token_cache[0]
    async with _token_lock:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        if _token_cache and _token_cache[1] - now > 300:
            return _token_cache[0]
        _token_cache = await asyncio.to_thread(_mint_token_sync)
        return _token_cache[0]


async def _api_get(url: str, params: Any = None) -> Dict[str, Any]:
    """Authenticated GET against the Sheets/Drive APIs with error mapping.

    Translates the statuses users actually hit into actionable messages:
    403 -> "share with <SA email>" (the #1 onboarding mistake), 404 -> bad
    URL, 429 -> rate limit (poller backs off by just failing the tick).
    ValueError = user-fixable, RuntimeError = transient/ops.
    """
    token = await _get_access_token()
    async with _get_session().get(
        url, params=params, headers={"Authorization": f"Bearer {token}"}
    ) as response:
        if response.status == 403:
            raise ValueError(
                "Access denied. Share the sheet with "
                f"{get_service_account_email()} (Viewer) and try again."
            )
        if response.status == 404:
            raise ValueError("Spreadsheet not found — check the URL.")
        if response.status == 429:
            raise RuntimeError("Google Sheets API rate limit hit (429); retry later")
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(
                f"Google API request failed ({response.status}): {body[:300]}"
            )
        return await response.json()


async def fetch_spreadsheet_meta(spreadsheet_id: str) -> Dict[str, Any]:
    """Validate access and return {title, sheet_titles} for the add-sheet flow."""
    data = await _api_get(
        f"{_SHEETS_API}/{spreadsheet_id}",
        params={"fields": "properties.title,sheets.properties.title"},
    )
    return {
        "title": data.get("properties", {}).get("title", "Untitled spreadsheet"),
        "sheet_titles": [
            s.get("properties", {}).get("title", "")
            for s in data.get("sheets", [])
            if s.get("properties", {}).get("title")
        ],
    }


class GoogleSheetsConnector(KBConnector):
    source_type = "google_sheet"

    async def load(self, document: KbDocument) -> NormalizedContent:
        """Fetch the sheet's current rows as TableData (one per tab).

        The connector half of ingestion for google_sheet documents: one
        ``values:batchGet`` for all configured ranges (or every tab when
        none were pinned at connect time). The content hash over the
        fetched values lets ingestion's row-level hash-diff re-embed only
        edited rows.
        """
        spreadsheet_id = str(document.source_ref.get("spreadsheet_id") or "")
        if not spreadsheet_id:
            raise ValueError(
                f"Document {document.id} has no spreadsheet_id in source_ref"
            )

        ranges: List[str] = list(document.source_ref.get("ranges") or [])
        if not ranges:
            meta = await fetch_spreadsheet_meta(spreadsheet_id)
            ranges = meta["sheet_titles"]
        if not ranges:
            raise ValueError("Spreadsheet has no sheets to read")

        data = await _api_get(
            f"{_SHEETS_API}/{spreadsheet_id}/values:batchGet",
            params=[("ranges", r) for r in ranges]
            + [("majorDimension", "ROWS"), ("valueRenderOption", "FORMATTED_VALUE")],
        )

        tables: List[TableData] = []
        for value_range in data.get("valueRanges", []):
            rows: List[List[str]] = [
                [str(cell) for cell in row[:MAX_SHEET_COLUMNS]]
                for row in value_range.get("values", [])
            ][: MAX_SHEET_ROWS + 1]
            if len(rows) < 2:
                continue  # header-only or empty tab
            # "'Sheet1'!A1:Z100" -> "Sheet1"
            range_name = value_range.get("range", "")
            sheet_name = range_name.split("!")[0].strip("'") or "sheet"
            tables.append(TableData(name=sheet_name, headers=rows[0], rows=rows[1:]))

        if not tables:
            raise ValueError(
                "No data rows found — the sheet needs a header row plus at "
                "least one data row"
            )

        content_hash = hashlib.sha256(
            json.dumps(
                [[t.name, t.headers, t.rows] for t in tables], ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        return NormalizedContent(tables=tables, content_hash=content_hash)

    async def detect_change(self, document: KbDocument) -> bool:
        """Cheap freshness probe: Drive modifiedTime vs the last sync time.

        Errors report "changed" so the poller falls through to a full
        re-read (which surfaces real errors on the document row).
        """
        spreadsheet_id = str(document.source_ref.get("spreadsheet_id") or "")
        if not spreadsheet_id or document.synced_at is None:
            return True
        try:
            data = await _api_get(
                f"{_DRIVE_API}/{spreadsheet_id}", params={"fields": "modifiedTime"}
            )
            modified_raw = data.get("modifiedTime")
            if not modified_raw:
                return True
            modified_at = datetime.datetime.fromisoformat(
                modified_raw.replace("Z", "+00:00")
            )
            synced_at = document.synced_at
            if synced_at.tzinfo is None:
                synced_at = synced_at.replace(tzinfo=datetime.timezone.utc)
            return modified_at > synced_at
        except Exception as e:
            logger.warning(
                f"Sheets change probe failed for document {document.id} "
                f"(assuming changed): {e}"
            )
            return True
