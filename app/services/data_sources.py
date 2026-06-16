"""Data source connector seam.

A data source is fetched through a pluggable *connector* selected by its
``type`` (``google_sheet`` today; ``google_doc`` / ``http`` / ``db`` later).
Dispatch-by-type mirrors the ``BUILTIN_HANDLERS`` registry and the global
function ``type`` switch already used elsewhere in the codebase.

Adding a new source = implement the ``DataSourceConnector`` protocol in its own
module and call ``register_connector(...)`` once. Nothing above the connector
(the load builtin, prefetch, caching, injection, template contract) changes.

This module stays dependency-light on purpose — it must NOT import connector
implementations (e.g. ``app.services.google.sheets``), because those import
``DATA_SOURCE_UNAVAILABLE`` from here. Connectors self-register on import.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

DATA_SOURCE_UNAVAILABLE = "[Data unavailable]"

# Cap a cached slice's size so a pathologically large sheet can't flood Redis
# and evict live-call state (locks, greeting audio, semaphore tokens).
MAX_DATA_SOURCE_CACHE_BYTES = 256 * 1024  # 256 KiB


def within_cache_limit(content: str) -> bool:
    """True if ``content`` is small enough to cache in Redis safely."""
    return len(content.encode("utf-8")) <= MAX_DATA_SOURCE_CACHE_BYTES


@runtime_checkable
class DataSourceConnector(Protocol):
    """Source-agnostic interface every connector implements.

    ``config`` is the connector-specific settings dict (opaque to callers).
    ``key`` selects the slice to load (a sheet tab, a doc heading, an http
    path param, ...). Connectors interpret ``key`` and own their cache key so
    the layers above never learn what a "sheet" is.
    """

    async def fetch(self, config: Dict[str, Any], key: Optional[str]) -> str: ...

    async def list_keys(self, config: Dict[str, Any]) -> List[str]: ...

    def cache_key(self, config: Dict[str, Any], key: Optional[str]) -> str: ...


# Registry of source type -> connector instance. One entry per source type.
_CONNECTORS: Dict[str, DataSourceConnector] = {}
_bootstrapped = False


def register_connector(type_: str, connector: DataSourceConnector) -> None:
    """Register a connector under its source ``type`` (idempotent overwrite)."""
    _CONNECTORS[type_] = connector


def _bootstrap_builtin_connectors() -> None:
    """Import built-in connector modules so they self-register.

    Deferred to call time (not module import) so the connector → sheets →
    data_sources import chain never forms a cycle: by the time this runs,
    this module is fully initialized. Add new built-in connectors here.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    # Flip the flag only AFTER a successful import, so a transient import
    # failure can be retried on the next call instead of being latched off.
    import app.services.google.sheets_connector  # noqa: F401

    _bootstrapped = True


def get_connector(type_: str) -> Optional[DataSourceConnector]:
    """Return the connector for ``type_`` or ``None`` if none is registered."""
    _bootstrap_builtin_connectors()
    return _CONNECTORS.get(type_)
