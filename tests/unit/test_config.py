from aistudio_api.config import _load_cors_origins


def test_load_cors_origins_splits_trims_strips_and_deduplicates(monkeypatch):
    monkeypatch.setenv(
        "AISTUDIO_CORS_ORIGINS",
        " https://example.com/,\nhttps://app.example.com, https://example.com ",
    )

    assert _load_cors_origins() == ("https://example.com", "https://app.example.com")
