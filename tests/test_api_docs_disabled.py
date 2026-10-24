"""/openapi.json, /docs and /redoc are not served (security review, 2026-10-24)."""

from fastapi.testclient import TestClient


def test_schema_and_docs_routes_are_gone() -> None:
    from app.main import app

    client = TestClient(app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path
    assert client.get("/").status_code == 200
