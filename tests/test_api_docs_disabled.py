"""Routes turned off for security are not served.

/openapi.json, /docs and /redoc (security review, 2026-10-24), and /uap/* by default.
"""

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


def test_schema_and_docs_routes_are_gone() -> None:
    from app.main import app

    client = TestClient(app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path
    assert client.get("/").status_code == 200


def test_uap_routes_are_off_by_default() -> None:
    from app.main import app

    paths = [r.path for r in app.routes if isinstance(r, APIRoute)]
    assert paths, "the walk found no routes"
    assert [p for p in paths if "/uap/" in p] == []
