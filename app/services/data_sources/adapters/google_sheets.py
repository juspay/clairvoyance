"""Google Sheets data-source adapter."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import google.auth as google_auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.core.config.static import GOOGLE_CREDENTIALS_JSON
from app.services.data_sources.models import (
    Capabilities,
    DataSourceUnavailable,
    Discovery,
    DiscoveryDataset,
    RawData,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def extract_spreadsheet_id(spreadsheet_url: str) -> str:
    match = SPREADSHEET_ID_RE.search(spreadsheet_url or "")
    if match:
        return match.group(1)
    if spreadsheet_url and "/" not in spreadsheet_url:
        return spreadsheet_url
    raise DataSourceUnavailable("Invalid Google Sheets URL")


def _quote_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _column_label(column_number: int) -> str:
    """Convert a 1-based column number to an A1 notation column label."""
    if column_number < 1:
        return "A"

    label = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        label = chr(65 + remainder) + label
    return label


class GoogleSheetsAdapter:
    source_type = "google_sheet"
    capabilities = Capabilities(supports_prefetch=True)

    def __init__(self) -> None:
        self._cached_session: AuthorizedSession | None = None

    def _credentials(self) -> Any:
        if GOOGLE_CREDENTIALS_JSON:
            credentials_raw = GOOGLE_CREDENTIALS_JSON.strip()
            if not credentials_raw.startswith("{"):
                credentials_path = Path(credentials_raw).expanduser()
                if credentials_path.exists():
                    credentials_raw = credentials_path.read_text()
            return service_account.Credentials.from_service_account_info(
                json.loads(credentials_raw),
                scopes=[SHEETS_SCOPE],
            )

        try:
            credentials, _project_id = google_auth.default(scopes=[SHEETS_SCOPE])
            return credentials
        except DefaultCredentialsError as exc:
            raise DataSourceUnavailable(
                "Google credentials are not configured. Set GOOGLE_CREDENTIALS_JSON "
                "or configure Application Default Credentials."
            ) from exc

    def _session(self) -> AuthorizedSession:
        if self._cached_session:
            return self._cached_session

        self._cached_session = AuthorizedSession(self._credentials())
        return self._cached_session

    async def discover(self, config: Dict[str, Any]) -> Discovery:
        return await asyncio.to_thread(self._discover_sync, config)

    def _list_sheet_metadata_sync(
        self, session: AuthorizedSession, spreadsheet_id: str
    ) -> List[Dict[str, Any]]:
        """Return sheet tab metadata in sheet order."""
        response = session.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={
                "fields": "sheets.properties(title,index,gridProperties.columnCount)"
            },
            timeout=10,
        )
        response.raise_for_status()
        sheets = sorted(
            response.json().get("sheets", []),
            key=lambda sheet: sheet.get("properties", {}).get("index", 0),
        )
        metadata = []
        for sheet in sheets:
            properties = sheet.get("properties", {})
            title = properties.get("title")
            if not title:
                continue
            grid = properties.get("gridProperties", {})
            metadata.append(
                {
                    "title": title,
                    "column_count": max(int(grid.get("columnCount") or 1), 1),
                }
            )
        return metadata

    def _discover_sync(self, config: Dict[str, Any]) -> Discovery:
        spreadsheet_id = extract_spreadsheet_id(config.get("spreadsheet_url", ""))
        session = self._session()
        sheets = self._list_sheet_metadata_sync(session, spreadsheet_id)
        if not sheets:
            return Discovery(datasets=[])

        ranges = [
            f"{_quote_sheet_name(sheet['title'])}!A1:{_column_label(sheet['column_count'])}6"
            for sheet in sheets
        ]
        response = session.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet",
            params=[
                ("majorDimension", "ROWS"),
                *[("ranges", range_name) for range_name in ranges],
            ],
            timeout=10,
        )
        response.raise_for_status()

        value_ranges = response.json().get("valueRanges", [])
        if len(value_ranges) != len(sheets):
            raise DataSourceUnavailable(
                "Google Sheets discovery response did not match requested tabs"
            )
        datasets = []
        for sheet, value_range in zip(sheets, value_ranges, strict=True):
            raw = self._raw_from_values(value_range.get("values", []))
            datasets.append(
                DiscoveryDataset(
                    name=sheet["title"],
                    columns=raw.columns,
                    preview_rows=raw.rows[:5],
                )
            )
        return Discovery(datasets=datasets)

    async def fetch_dataset(
        self, config: Dict[str, Any], selector: Dict[str, Any]
    ) -> RawData:
        return await asyncio.to_thread(self._fetch_dataset_sync, config, selector)

    async def fetch_datasets(
        self, config: Dict[str, Any], selectors: List[Dict[str, Any]]
    ) -> List[RawData]:
        return await asyncio.to_thread(self._fetch_datasets_sync, config, selectors)

    def _range_for_selector(self, selector: Dict[str, Any]) -> str:
        sheet_name = selector.get("sheet_name")
        if not sheet_name:
            raise DataSourceUnavailable("selector.sheet_name is required")

        range_name = selector.get("range")
        max_rows = selector.get("max_rows")
        if range_name and max_rows:
            raise DataSourceUnavailable(
                "selector.range and selector.max_rows cannot be used together"
            )
        if range_name:
            return f"{_quote_sheet_name(sheet_name)}!{range_name}"
        if max_rows:
            return f"{_quote_sheet_name(sheet_name)}!1:{int(max_rows)}"
        return _quote_sheet_name(sheet_name)

    def _fetch_datasets_sync(
        self,
        config: Dict[str, Any],
        selectors: List[Dict[str, Any]],
    ) -> List[RawData]:
        spreadsheet_id = extract_spreadsheet_id(config.get("spreadsheet_url", ""))
        ranges = [self._range_for_selector(selector) for selector in selectors]
        if not ranges:
            return []

        response = self._session().get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet",
            params=[
                ("majorDimension", "ROWS"),
                *[("ranges", range_name) for range_name in ranges],
            ],
            timeout=10,
        )
        response.raise_for_status()
        value_ranges = response.json().get("valueRanges", [])
        if len(value_ranges) != len(selectors):
            raise DataSourceUnavailable(
                "Google Sheets batch response did not match requested tabs"
            )
        return [
            self._raw_from_values(value_range.get("values", []))
            for value_range in value_ranges
        ]

    def _fetch_dataset_sync(
        self,
        config: Dict[str, Any],
        selector: Dict[str, Any],
        session: AuthorizedSession | None = None,
    ) -> RawData:
        spreadsheet_id = extract_spreadsheet_id(config.get("spreadsheet_url", ""))
        session = session or self._session()
        a1_range = self._range_for_selector(selector)
        encoded_range = quote(a1_range, safe="")

        response = session.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}",
            params={"majorDimension": "ROWS"},
            timeout=10,
        )
        response.raise_for_status()
        return self._raw_from_values(response.json().get("values", []))

    def _raw_from_values(self, values: List[List[Any]]) -> RawData:
        if not values:
            return RawData(columns=[], rows=[])

        seen: Dict[str, int] = {}
        columns = []
        for index, value in enumerate(values[0]):
            base = str(value).strip() or f"column_{index + 1}"
            count = seen.get(base, 0)
            seen[base] = count + 1
            columns.append(base if count == 0 else f"{base}_{count + 1}")
        rows = []
        for raw_row in values[1:]:
            row = {
                column: raw_row[index] if index < len(raw_row) else ""
                for index, column in enumerate(columns)
            }
            rows.append(row)
        return RawData(columns=columns, rows=rows)
