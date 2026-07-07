"""Normalize raw adapter output into runtime text datasets."""

import csv
import io
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.types import DatasetUse
from app.services.data_sources.models import RawData


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value)


def _to_markdown(columns: list[str], rows: list[Dict[str, Any]]) -> str:
    if not columns:
        return ""
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = [
        "| " + " | ".join(_cell_text(row.get(column, "")) for column in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, divider, *body])


def _to_csv(columns: list[str], rows: list[Dict[str, Any]]) -> str:
    if not columns:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def normalize(raw: RawData, dataset: DatasetUse) -> Dict[str, Any]:
    """Normalize one selected tab into rendered text content."""
    content = raw.text
    if not content:
        content = (
            _to_csv(raw.columns, raw.rows)
            if dataset.format == "csv"
            else _to_markdown(raw.columns, raw.rows)
        )

    normalized: Dict[str, Any] = {
        "format": dataset.format,
        "content": content,
    }
    if dataset.variable_name:
        normalized["variable_name"] = dataset.variable_name
    return normalized
