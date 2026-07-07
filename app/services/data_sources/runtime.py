"""Runtime bundle fetch/cache helpers for template data sources."""

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, Optional, cast

from app.ai.voice.agents.breeze_buddy.template.types import TemplateDataSourceRef
from app.core.config.static import DATA_SOURCE_BUNDLE_TTL_SECONDS
from app.core.logger import logger
from app.database.accessor.breeze_buddy.data_source import get_data_source_by_id
from app.schemas.breeze_buddy.data_source import DataSource
from app.services.data_sources.cache import (
    build_bundle,
    build_ref_signature,
    get_cached_bundle,
    set_cached_bundle,
)
from app.services.data_sources.normalizers import normalize
from app.services.data_sources.registry import get_adapter

DEFAULT_DATA_SOURCE_FETCH_TIMEOUT_SECONDS = 3.0


def _source_fingerprint(data_source: DataSource) -> str:
    updated_at = data_source.updated_at.isoformat() if data_source.updated_at else ""
    payload = json.dumps(
        {
            "id": data_source.id,
            "is_active": data_source.is_active,
            "updated_at": updated_at,
            "config": data_source.config,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _selector_dict(selector: Any) -> Dict[str, Any]:
    if hasattr(selector, "model_dump"):
        return selector.model_dump(mode="json", exclude_none=True)
    return dict(selector)


async def fetch_bundle_for_ref(
    ref: TemplateDataSourceRef, data_source: Optional[DataSource] = None
) -> Optional[Dict[str, Any]]:
    """Fetch and normalize all configured datasets for one template ref."""
    data_source = data_source or await get_data_source_by_id(ref.data_source_id)
    if not data_source or not data_source.is_active:
        logger.warning(f"Data source {ref.data_source_id} missing or inactive")
        return None

    adapter = get_adapter(data_source.source_type.value)
    datasets: Dict[str, Any] = {}
    fetch_datasets = getattr(adapter, "fetch_datasets", None)

    if callable(fetch_datasets):
        try:
            selectors = [_selector_dict(dataset.selector) for dataset in ref.datasets]
            batch_fetch = cast(
                Callable[[Dict[str, Any], list[Dict[str, Any]]], Awaitable[list[Any]]],
                fetch_datasets,
            )
            raw_datasets = await batch_fetch(data_source.config, selectors)
            if len(raw_datasets) != len(ref.datasets):
                raise ValueError(
                    "batch fetch returned "
                    f"{len(raw_datasets)} datasets for {len(ref.datasets)} selectors"
                )
            for dataset, raw in zip(ref.datasets, raw_datasets, strict=True):
                datasets[dataset.target] = normalize(raw, dataset)
            return build_bundle(
                source_name=ref.name,
                data_source_id=data_source.id,
                datasets=datasets,
            )
        except Exception as exc:
            logger.warning(
                f"Batch fetch failed for data source '{ref.name}'; falling back: {exc}"
            )

    for dataset in ref.datasets:
        # Isolate per-tab failures so one bad tab does not remove the whole
        # source from the call context.
        try:
            raw = await adapter.fetch_dataset(
                data_source.config, _selector_dict(dataset.selector)
            )
            datasets[dataset.target] = normalize(raw, dataset)
        except Exception as exc:
            logger.warning(f"Skipping dataset '{ref.name}.{dataset.target}': {exc}")

    return build_bundle(
        source_name=ref.name,
        data_source_id=data_source.id,
        datasets=datasets,
    )


async def get_or_fetch_bundle(
    template_id: str,
    ref: TemplateDataSourceRef,
    *,
    timeout_seconds: Optional[float] = DEFAULT_DATA_SOURCE_FETCH_TIMEOUT_SECONDS,
    write_cache: bool = True,
) -> Optional[Dict[str, Any]]:
    """Read a data-source bundle from Redis, falling back to a bounded fetch."""
    data_source = await get_data_source_by_id(ref.data_source_id)
    if not data_source or not data_source.is_active:
        logger.warning(f"Data source {ref.data_source_id} missing or inactive")
        return None

    ref_signature = build_ref_signature(ref, _source_fingerprint(data_source))
    cached = await get_cached_bundle(template_id, ref.name, ref_signature)
    if cached:
        return cached

    try:
        fetch = fetch_bundle_for_ref(ref, data_source=data_source)
        bundle = await asyncio.wait_for(fetch, timeout=timeout_seconds)
    except Exception as exc:
        logger.warning(f"Failed to fetch data source '{ref.name}': {exc}")
        return None

    if bundle and write_cache:
        await set_cached_bundle(
            template_id=template_id,
            ref_name=ref.name,
            bundle=bundle,
            ttl_seconds=DATA_SOURCE_BUNDLE_TTL_SECONDS,
            ref_signature=ref_signature,
        )
    return bundle
