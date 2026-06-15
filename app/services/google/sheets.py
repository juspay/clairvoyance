"""
Google Sheets Service

Fetches data from Google Sheets using the platform's shared GCP service account.
Merchants share their sheet with the platform SA email (Viewer access).
The SA credentials come from GOOGLE_CREDENTIALS_JSON env var.

Supported output formats:
  - markdown_table : LLM-readable, token-efficient
  - csv            : compact, parseable
  - json           : structured, for programmatic use
"""

import asyncio
import csv
import io
import json
import re
from typing import List, Optional
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.core.config.static import GOOGLE_CREDENTIALS_JSON
from app.core.logger import logger
from app.services.data_sources import DATA_SOURCE_UNAVAILABLE

# Read-only scope — we never write to sheets
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Regex to extract spreadsheet ID from any Google Sheets URL format
_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)")


def extract_spreadsheet_id(url: str) -> Optional[str]:
    """Extract spreadsheet ID from a Google Sheets URL."""
    match = _SPREADSHEET_ID_RE.search(url)
    return match.group(1) if match else None


def _get_sheets_session():
    """Build an authenticated HTTP session for the Google Sheets API."""
    if not GOOGLE_CREDENTIALS_JSON:
        logger.error(
            "GOOGLE_CREDENTIALS_JSON env var is not set — cannot access Google Sheets"
        )
        return None

    try:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
        return None

    try:
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict, scopes=_SCOPES
        )
        return AuthorizedSession(credentials)
    except Exception as e:
        logger.error(f"Failed to build Google Sheets session: {e}")
        return None


def _get_json(session: AuthorizedSession, url: str, params: Optional[dict] = None):
    response = session.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


async def list_tabs(spreadsheet_id: str) -> List[str]:
    """List all tab (sheet) names in a spreadsheet."""

    def _fetch():
        session = _get_sheets_session()
        if not session:
            return []
        try:
            result = _get_json(
                session,
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                params={"fields": "sheets.properties.title"},
            )
            return [sheet["properties"]["title"] for sheet in result.get("sheets", [])]
        except Exception as e:
            logger.error(f"Error listing tabs for {spreadsheet_id}: {e}")
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _fetch)


async def get_column_headers(
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
) -> List[str]:
    """Get column headers (first row) of a sheet tab."""

    def _fetch():
        session = _get_sheets_session()
        if not session:
            return []
        try:
            tab = sheet_name
            if not tab:
                meta = _get_json(
                    session,
                    f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                    params={"fields": "sheets.properties.title"},
                )
                sheets = meta.get("sheets", [])
                if not sheets:
                    return []
                tab = sheets[0]["properties"]["title"]

            range_str = f"'{tab}'!1:1"
            result = _get_json(
                session,
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{spreadsheet_id}/values/{quote(range_str, safe='')}",
            )
            rows = result.get("values", [])
            return rows[0] if rows else []
        except Exception as e:
            logger.error(
                f"Error fetching headers for {spreadsheet_id}/{sheet_name}: {e}"
            )
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _fetch)


async def fetch_sheet_data(
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
    max_rows: int = 500,
) -> List[dict]:
    """Fetch sheet data as a list of row dicts {column_name: value}."""

    def _fetch():
        session = _get_sheets_session()
        if not session:
            return []
        try:
            tab = sheet_name
            if not tab:
                meta = _get_json(
                    session,
                    f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                    params={"fields": "sheets.properties.title"},
                )
                sheets = meta.get("sheets", [])
                if not sheets:
                    return []
                tab = sheets[0]["properties"]["title"]

            range_str = f"'{tab}'!A1:ZZ{max_rows + 1}"
            result = _get_json(
                session,
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{spreadsheet_id}/values/{quote(range_str, safe='')}",
            )
            rows = result.get("values", [])
            if not rows:
                return []

            headers = rows[0]
            data_rows = rows[1:]

            records = []
            for row in data_rows:
                padded = row + [""] * (len(headers) - len(row))
                record = {headers[i]: padded[i] for i in range(len(headers))}
                records.append(record)

            if columns:
                records = [{col: r.get(col, "") for col in columns} for r in records]

            return records
        except Exception as e:
            logger.error(f"Error fetching data for {spreadsheet_id}/{sheet_name}: {e}")
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _fetch)


def _rows_to_markdown_table(headers: List[str], rows: List[dict]) -> str:
    if not headers or not rows:
        return "(no data)"
    header_line = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    data_lines = []
    for row in rows:
        cells = [str(row.get(h, "")) for h in headers]
        data_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_line, separator] + data_lines)


def _rows_to_csv(headers: List[str], rows: List[dict]) -> str:
    if not headers or not rows:
        return "(no data)"
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([str(row.get(h, "")) for h in headers])
    return output.getvalue()


def _rows_to_json(rows: List[dict]) -> str:
    if not rows:
        return "[]"
    return json.dumps(rows, ensure_ascii=False)


async def fetch_formatted(
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
    format: str = "markdown_table",
    max_rows: int = 500,
) -> str:
    """
    Fetch sheet data and return as a formatted string for LLM injection.

    Returns DATA_SOURCE_UNAVAILABLE on any error or empty sheet.
    """
    rows = await fetch_sheet_data(spreadsheet_id, sheet_name, columns, max_rows)
    if not rows:
        logger.warning(
            f"No data fetched from spreadsheet={spreadsheet_id}, sheet={sheet_name}"
        )
        return DATA_SOURCE_UNAVAILABLE

    headers = list(rows[0].keys()) if rows else []

    if format == "csv":
        return _rows_to_csv(headers, rows)
    elif format == "json":
        return _rows_to_json(rows)
    else:
        return _rows_to_markdown_table(headers, rows)
