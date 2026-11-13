"""The DragonTTS cache proxy forwards real cache keys only.

The admin routes put ``{key}`` into the internal DragonTTS URL. A key such as
``clear?x=`` cut off the route's fixed ending and reached DragonTTS endpoints
the proxy does not expose (``POST /cache/clear`` deletes the whole cache).
"""

from __future__ import annotations

from typing import List

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.breeze_buddy import dragontts
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole

KEY = "ab" * 32  # a sha256 hex digest, as DragonTTS issues


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Every request the proxy sends to DragonTTS, as "METHOD /path?query"."""
    sent: List[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.raw_path.decode()}")
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(answer), **kwargs)

    async def url() -> str:
        return "http://dragontts.internal:8000"

    monkeypatch.setattr(dragontts, "DRAGONTTS_URL", url)
    monkeypatch.setattr(dragontts.httpx, "AsyncClient", client)
    return sent


@pytest.fixture
def admin() -> TestClient:
    app = FastAPI()
    app.include_router(dragontts.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: UserInfo(
        id="admin-1", username="admin", role=UserRole.ADMIN
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "method,route,sent",
    [
        ("GET", f"/admin/dragontts/cache/{KEY}", f"GET /cache/{KEY}"),
        ("DELETE", f"/admin/dragontts/cache/{KEY}", f"DELETE /cache/{KEY}"),
        (
            "POST",
            f"/admin/dragontts/cache/{KEY}/resynth",
            f"POST /cache/{KEY}/resynth",
        ),
        ("POST", f"/admin/dragontts/cache/{KEY}/audio", f"POST /cache/{KEY}/audio"),
    ],
)
def test_a_real_key_is_forwarded(admin, upstream, method, route, sent) -> None:
    response = admin.request(method, route)

    assert response.status_code == 200
    assert upstream == [sent]


@pytest.mark.parametrize(
    "method,route",
    [
        # "?" or "#" in the key drops the route's ending: DragonTTS would get
        # POST /cache/clear, /cache/delete-by-age, /cache/delete-by-text.
        ("POST", "/admin/dragontts/cache/clear%3Fx=/resynth"),
        ("POST", "/admin/dragontts/cache/clear%23/resynth"),
        ("POST", "/admin/dragontts/cache/delete-by-age%3Fx=/audio"),
        ("POST", "/admin/dragontts/cache/delete-by-text%3Fx=/audio"),
        # ".." walks out of /cache.
        ("GET", "/admin/dragontts/cache/%2e%2e"),
        ("DELETE", "/admin/dragontts/cache/%2e%2e"),
        ("POST", "/admin/dragontts/cache/%2e%2e/resynth"),
        # A key-like value that is not a sha256 hex digest.
        ("GET", "/admin/dragontts/cache/" + "AB" * 32),
        ("GET", "/admin/dragontts/cache/" + "ab" * 31),
        ("GET", "/admin/dragontts/cache/" + "ab" * 32 + "0"),
        ("DELETE", "/admin/dragontts/cache/clear"),
    ],
)
def test_anything_else_is_404_and_nothing_is_sent(
    admin, upstream, method, route
) -> None:
    response = admin.request(method, route)

    assert response.status_code == 404
    assert upstream == []


def test_by_text_keeps_its_own_route(admin, upstream) -> None:
    response = admin.get("/admin/dragontts/cache/by-text", params={"text": "hello"})

    assert response.status_code == 200
    assert len(upstream) == 1 and upstream[0].startswith("GET /cache/by-text?")
