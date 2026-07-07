from fastapi.testclient import TestClient

from aistudio_api.api.app import app


def _preflight(origin: str):
    client = TestClient(app)
    return client.options(
        "/v1/models",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_localhost_plugin_preflight_allows_authorization_header():
    response = _preflight("http://localhost:8000")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8000"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_ipv4_and_ipv6_loopback_origins_are_allowed():
    ipv4_response = _preflight("http://127.0.0.1:8000")
    ipv6_response = _preflight("http://[::1]:8000")

    assert ipv4_response.status_code == 200
    assert ipv4_response.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert ipv6_response.status_code == 200
    assert ipv6_response.headers["access-control-allow-origin"] == "http://[::1]:8000"


def test_local_desktop_webview_origins_are_allowed():
    origins = [
        "null",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        "app://localhost",
        "electron://localhost",
        "capacitor://localhost",
        "ionic://localhost",
    ]

    for origin in origins:
        response = _preflight(origin)

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_non_local_origin_is_not_allowed_by_default():
    response = _preflight("https://example.com")

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
