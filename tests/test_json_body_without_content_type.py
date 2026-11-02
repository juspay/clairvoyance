from fastapi.testclient import TestClient

from app.main import app


def test_a_json_body_with_no_content_type_is_still_parsed():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/agent/voice/breeze-buddy/login",
        content=b'{"username": "a"}',
    )

    assert "content-type" not in response.request.headers
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
