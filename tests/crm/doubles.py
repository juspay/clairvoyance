"""Test doubles shared across the connectivity and outreach suites — one
implementation each, so a fake that grows a method grows it everywhere.

Rules the doubles keep: a fake accessor records writes BY FIELD NAME (the
strict-zip trick — an accessor whose signature grows a column turns the
assertions red instead of silently reading the wrong one); an HTTP stub
captures the exact request the code made and delegates to a canned handler.
"""

from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

# --- HTTP transports -----------------------------------------------------------


def stub_http(
    monkeypatch: pytest.MonkeyPatch, module: Any, handler: Callable
) -> List[httpx.Request]:
    """Point ``module.create_http_client`` at a canned responder; return the
    list every outgoing request is appended to, in order."""
    seen: List[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        module,
        "create_http_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(_capture)),
    )
    return seen


def stub_graph(
    monkeypatch: pytest.MonkeyPatch, handler: Callable
) -> List[httpx.Request]:
    """The shared Meta Graph transport, canned."""
    from app.crm.connectivity.providers.meta import graph as graph_module

    return stub_http(monkeypatch, graph_module, handler)


# --- accessors -----------------------------------------------------------------

#: db/accessors/installation.upsert_installation's positional signature after
#: ``txn``, named once so assertions read as facts about the row.
UPSERT_INSTALLATION_FIELDS = (
    "merchant_id",
    "connector_key",
    "external_account_id",
    "display_label",
    "credential_id",
    "status",
    "token_expires_at",
    "health_detail_json",
)


class FakeInstallationAccessor:
    """Stands in for db/accessors/installation.

    ``installation`` / ``existing_account``: what the door read answers
    (``get_installation_by_account``) — two names because the templates suite
    seeds "the door behind this template" and the onboarding suite seeds
    "what is already connected"; ``upsert_returns``: what the write answers.
    """

    def __init__(
        self,
        installation: Any = None,
        *,
        existing_account: Any = None,
        upsert_returns: Any = None,
    ) -> None:
        self.installation = installation
        self.existing_account = existing_account
        self.upsert_returns = upsert_returns
        self.written: Optional[Dict[str, Any]] = None

    async def get_installation(self, merchant_id, installation_id):
        return self.installation

    async def get_installation_by_account(self, merchant_id, key, account_ref):
        if self.existing_account is not None:
            return self.existing_account
        return self.installation

    async def upsert_installation(self, txn, *args):
        self.written = dict(zip(UPSERT_INSTALLATION_FIELDS, args, strict=True))
        return self.upsert_returns


def patch_accessors(monkeypatch: pytest.MonkeyPatch, module: Any, fake: Any) -> None:
    """Point every ``*_accessor`` name a logic module imports at one fake —
    the outreach suites seed one object that answers for all three tables."""
    names = [n for n in vars(module) if n.endswith("_accessor") or n == "accessor"]
    assert names, f"{module.__name__} imports no accessor to patch"
    for name in names:
        monkeypatch.setattr(module, name, fake)
