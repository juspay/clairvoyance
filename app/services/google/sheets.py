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
import json
import re
from typing import List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config.static import GOOGLE_CREDENTIALS_JSON
from app.core.logger import logger

# Read-only scope — we never write to sheets
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Regex to extract spreadsheet ID from any Google Sheets URL format
_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)")


def extract_spreadsheet_id(url: str) -> Optional[str]:
    """Extract spreadsheet ID from a Google Sheets URL."""
    match = _SPREADSHEET_ID_RE.search(url)
    return match.group(1) if match else None


def _get_sheets_service():
    """Build authenticated Google Sheets API service using the platform SA."""
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
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return service
    except Exception as e:
        logger.error(f"Failed to build Google Sheets service: {e}")
        return None


async def list_tabs(spreadsheet_id: str) -> List[str]:
    """List all tab (sheet) names in a spreadsheet."""

    def _fetch():
        service = _get_sheets_service()
        if not service:
            return []
        try:
            result = (
                service.spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
                .execute()
            )
            return [sheet["properties"]["title"] for sheet in result.get("sheets", [])]
        except HttpError as e:
            logger.error(
                f"Google Sheets API error listing tabs for {spreadsheet_id}: {e}"
            )
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing tabs for {spreadsheet_id}: {e}")
            return []

    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


async def get_column_headers(
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
) -> List[str]:
    """Get column headers (first row) of a sheet tab."""

    def _fetch():
        service = _get_sheets_service()
        if not service:
            return []
        try:
            tab = sheet_name
            if not tab:
                meta = (
                    service.spreadsheets()
                    .get(
                        spreadsheetId=spreadsheet_id,
                        fields="sheets.properties.title",
                    )
                    .execute()
                )
                sheets = meta.get("sheets", [])
                if not sheets:
                    return []
                tab = sheets[0]["properties"]["title"]

            range_str = f"'{tab}'!1:1"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=range_str)
                .execute()
            )
            rows = result.get("values", [])
            return rows[0] if rows else []
        except HttpError as e:
            logger.error(
                f"Google Sheets API error fetching headers "
                f"for {spreadsheet_id}/{sheet_name}: {e}"
            )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error fetching headers "
                f"for {spreadsheet_id}/{sheet_name}: {e}"
            )
            return []

    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


async def fetch_sheet_data(
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
    max_rows: int = 500,
) -> List[dict]:
    """Fetch sheet data as a list of row dicts {column_name: value}."""

    def _fetch():
        service = _get_sheets_service()
        if not service:
            return []
        try:
            tab = sheet_name
            if not tab:
                meta = (
                    service.spreadsheets()
                    .get(
                        spreadsheetId=spreadsheet_id,
                        fields="sheets.properties.title",
                    )
                    .execute()
                )
                sheets = meta.get("sheets", [])
                if not sheets:
                    return []
                tab = sheets[0]["properties"]["title"]

            range_str = f"'{tab}'!A1:ZZ{max_rows + 1}"
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=range_str)
                .execute()
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
                col_set = set(columns)
                records = [
                    {col: r.get(col, "") for col in columns if col in col_set}
                    for r in records
                ]

            return records
        except HttpError as e:
            logger.error(
                f"Google Sheets API error fetching data "
                f"for {spreadsheet_id}/{sheet_name}: {e}"
            )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error fetching data "
                f"for {spreadsheet_id}/{sheet_name}: {e}"
            )
            return []

    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


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
    lines = [",".join(headers)]
    for row in rows:
        cells = [str(row.get(h, "")).replace(",", ";") for h in headers]
        lines.append(",".join(cells))
    return "\n".join(lines)


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

    Returns "[No data available]" on any error or empty sheet.
    """
    rows = await fetch_sheet_data(spreadsheet_id, sheet_name, columns, max_rows)
    if not rows:
        logger.warning(
            f"No data fetched from spreadsheet={spreadsheet_id}, sheet={sheet_name}"
        )
        return "[No data available]"

    headers = list(rows[0].keys()) if rows else []

    if format == "csv":
        return _rows_to_csv(headers, rows)
    elif format == "json":
        return _rows_to_json(rows)
    else:
        return _rows_to_markdown_table(headers, rows)
