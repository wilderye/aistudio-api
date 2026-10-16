import pytest
from fastapi.testclient import TestClient

from aistudio_api.api.app import app
from aistudio_api.config import settings


def _preflight(origin, path="/v1/chat/completions", method="POST"):
    return TestClient(app).options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type, x-api-key",
        },
    )


@pytest.mark.parametrize("origin", [
    "http://localhost:8000",
    "https://localhost",
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
])
def test_local_web_plugin_preflight_allows_chat_headers(origin):
    response = _preflight(origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]
    allowed = response.headers["access-control-allow-headers"].lower()
    assert all(header in allowed for header in ("authorization", "content-type", "x-api-key"))


@pytest.mark.parametrize("origin", [
    "https://example.com",
    "http://localhost.example.com:8000",
    "null",
    "tauri://localhost",
])
def test_unconfigured_other_origins_are_not_allowed(origin):
    response = _preflight(origin)

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_preserves_api_auth_and_allows_reading_models(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"test-key"}))
    origin = "http://localhost:8000"
    client = TestClient(app)

    preflight = _preflight(origin, "/v1/models", "GET")
    assert preflight.status_code == 200

    denied = client.get("/v1/models", headers={"Origin": origin})
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == origin

    allowed = client.get(
        "/v1/models",
        headers={"Origin": origin, "Authorization": "Bearer test-key"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == origin
    assert any(model["id"] == "gemma-4-31b-it" for model in allowed.json()["data"])
