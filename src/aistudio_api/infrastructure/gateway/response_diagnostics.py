"""Small response diagnostics for investigating AI Studio truncation."""

from __future__ import annotations

import logging
from typing import Any

from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput

logger = logging.getLogger("aistudio")


def response_diag_enabled() -> bool:
    return bool(getattr(settings, "response_diag", False))


def _trim(value: Any, limit: int = 160) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _summarize_safety_ratings(safety_ratings: Any) -> list[Any]:
    if not isinstance(safety_ratings, list):
        return []

    summary: list[Any] = []
    for rating in safety_ratings[:8]:
        if isinstance(rating, dict):
            safe_item = {}
            for key in ("category", "probability", "severity", "blocked"):
                if key in rating:
                    safe_item[key] = rating[key]
            summary.append(safe_item or {"keys": sorted(str(key) for key in rating.keys())[:8]})
        elif isinstance(rating, list):
            summary.append(rating[:6])
        else:
            summary.append(_trim(rating, 80))
    return summary


def log_stream_candidate_diag(
    *,
    model: str,
    chunk_index: int,
    event_types: list[str],
    candidate: Candidate,
) -> None:
    if not response_diag_enabled():
        return

    logger.info(
        "[diag] stream chunk: model=%s chunk=%d type=%s finish_reason=%s finish_message=%r "
        "safety=%s body_chars=%d thinking_chars=%d images=%d reasoning_images=%d tool_calls=%d",
        model,
        chunk_index,
        ",".join(event_types) if event_types else "metadata",
        candidate.finish_reason,
        _trim(candidate.finish_message),
        _summarize_safety_ratings(candidate.safety_ratings),
        len(candidate.text),
        len(candidate.thinking),
        len(candidate.images),
        len(candidate.reasoning_images),
        len(candidate.function_calls),
    )


def log_stream_done_diag(
    *,
    model: str,
    status_code: int,
    chunks: int,
    body_chars: int,
    thinking_chars: int,
    tool_call_chunks: int,
    final_finish_reason: int | None,
    final_finish_message: str,
    final_safety_ratings: Any,
) -> None:
    if not response_diag_enabled():
        return

    logger.info(
        "[diag] stream done: model=%s status=%s chunks=%d body_chars=%d thinking_chars=%d "
        "tool_call_chunks=%d final_finish_reason=%s final_finish_message=%r final_safety=%s",
        model,
        status_code,
        chunks,
        body_chars,
        thinking_chars,
        tool_call_chunks,
        final_finish_reason,
        _trim(final_finish_message),
        _summarize_safety_ratings(final_safety_ratings),
    )


def log_nonstream_response_diag(*, model: str, status_code: int, output: ModelOutput) -> None:
    if not response_diag_enabled():
        return

    candidate = output.candidates[0] if output.candidates else Candidate()
    logger.info(
        "[diag] nonstream done: model=%s status=%s body_chars=%d thinking_chars=%d "
        "finish_reason=%s finish_message=%r safety=%s response_id=%s",
        model,
        status_code,
        len(output.text),
        len(output.thinking),
        candidate.finish_reason,
        _trim(candidate.finish_message),
        _summarize_safety_ratings(candidate.safety_ratings),
        output.response_id,
    )
