import logging

from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway import response_diagnostics as diag


def test_stream_candidate_diag_is_silent_when_disabled(monkeypatch, caplog):
    monkeypatch.setattr(settings, "response_diag", False)
    candidate = Candidate(text="secret body", thinking="secret thinking", finish_reason=2)

    with caplog.at_level(logging.INFO, logger="aistudio"):
        diag.log_stream_candidate_diag(
            model="gemma-4-31b-it",
            chunk_index=1,
            event_types=["body"],
            candidate=candidate,
        )

    assert "[diag]" not in caplog.text


def test_stream_candidate_diag_logs_metadata_without_content(monkeypatch, caplog):
    monkeypatch.setattr(settings, "response_diag", True)
    candidate = Candidate(
        text="secret body",
        thinking="secret thinking",
        finish_reason=2,
        finish_message="blocked by safety",
        safety_ratings=[
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "probability": "HIGH",
                "blocked": True,
                "secret_quote": "do not log this",
            }
        ],
    )

    with caplog.at_level(logging.INFO, logger="aistudio"):
        diag.log_stream_candidate_diag(
            model="gemma-4-31b-it",
            chunk_index=3,
            event_types=["body"],
            candidate=candidate,
        )

    assert "[diag] stream chunk" in caplog.text
    assert "finish_reason=2" in caplog.text
    assert "body_chars=11" in caplog.text
    assert "thinking_chars=15" in caplog.text
    assert "HARM_CATEGORY_DANGEROUS_CONTENT" in caplog.text
    assert "secret body" not in caplog.text
    assert "secret thinking" not in caplog.text
    assert "do not log this" not in caplog.text


def test_nonstream_diag_logs_finish_and_safety_without_content(monkeypatch, caplog):
    monkeypatch.setattr(settings, "response_diag", True)
    output = ModelOutput(
        candidates=[
            Candidate(
                text="visible answer",
                thinking="visible chain",
                finish_reason=3,
                finish_message="finished",
                safety_ratings=[[None, None, 9, 4, "extra", "trimmed"]],
            )
        ],
        response_id="resp_test",
    )

    with caplog.at_level(logging.INFO, logger="aistudio"):
        diag.log_nonstream_response_diag(model="gemma-4-31b-it", status_code=200, output=output)

    assert "[diag] nonstream done" in caplog.text
    assert "finish_reason=3" in caplog.text
    assert "body_chars=14" in caplog.text
    assert "thinking_chars=13" in caplog.text
    assert "resp_test" in caplog.text
    assert "visible answer" not in caplog.text
    assert "visible chain" not in caplog.text
