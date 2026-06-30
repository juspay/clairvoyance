"""Google Sheets data source connector.

The ONLY place that knows what a "tab" or "spreadsheet_url" is. Implements the
``DataSourceConnector`` protocol and self-registers under ``google_sheet`` so
the load builtin / prefetch reach it purely by ``type``.

``key`` is interpreted as the sheet tab name (falling back to the configured
``sheet_name`` when no key is supplied). The cache key matches the Phase-1
inline format (``datasource:{sheet_id}:{tab}:{cols_digest}:{format}``) so eager
and on-demand paths share cached slices.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.services.data_sources import DATA_SOURCE_UNAVAILABLE, register_connector
from app.services.google.sheets import (
    extract_spreadsheet_id,
    fetch_formatted,
    list_tabs,
)


class GoogleSheetConnector:
    """Fetch a single tab of a Google Sheet, addressed by ``key`` (tab name)."""

    async def fetch(self, config: Dict[str, Any], key: Optional[str]) -> str:
        spreadsheet_id = extract_spreadsheet_id(config.get("spreadsheet_url") or "")
        if not spreadsheet_id:
            logger.warning(
                f"GoogleSheetConnector: malformed spreadsheet_url: "
                f"{config.get('spreadsheet_url')}"
            )
            return DATA_SOURCE_UNAVAILABLE

        # key (the requested tab) wins over the static config sheet_name.
        sheet_name = key or config.get("sheet_name")
        return await fetch_formatted(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            columns=config.get("columns"),
            format=config.get("format", "markdown_table"),
        )

    async def list_keys(self, config: Dict[str, Any]) -> List[str]:
        spreadsheet_id = extract_spreadsheet_id(config.get("spreadsheet_url") or "")
        if not spreadsheet_id:
            return []
        return await list_tabs(spreadsheet_id)

    def cache_key(self, config: Dict[str, Any], key: Optional[str]) -> str:
        spreadsheet_id = (
            extract_spreadsheet_id(config.get("spreadsheet_url") or "") or "invalid_url"
        )
        sheet = key or config.get("sheet_name") or "_first_"
        cols = json.dumps(sorted(config.get("columns") or []))
        cols_digest = hashlib.md5(cols.encode()).hexdigest()[:8]
        fmt = config.get("format", "markdown_table")
        return f"datasource:{spreadsheet_id}:{sheet}:{cols_digest}:{fmt}"


register_connector("google_sheet", GoogleSheetConnector())
