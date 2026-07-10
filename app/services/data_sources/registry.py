"""Adapter registry for data sources."""

from typing import Dict

from app.services.data_sources.models import DataSourceAdapter

_ADAPTERS: Dict[str, DataSourceAdapter] = {}
_BOOTSTRAPPED = False


def register_adapter(source_type: str, adapter: DataSourceAdapter) -> None:
    _ADAPTERS[source_type] = adapter


def _bootstrap_adapters() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    from app.services.data_sources.adapters.google_sheets import GoogleSheetsAdapter

    register_adapter("google_sheet", GoogleSheetsAdapter())
    _BOOTSTRAPPED = True


def get_adapter(source_type: str) -> DataSourceAdapter:
    _bootstrap_adapters()
    if source_type not in _ADAPTERS:
        raise KeyError(f"No data-source adapter registered for {source_type}")
    return _ADAPTERS[source_type]
