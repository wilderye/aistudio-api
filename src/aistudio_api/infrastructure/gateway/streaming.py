"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError, classify_error
from aistudio_api.domain.models import parse_chunk_usage, parse_response_chunk
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.request_rewriter import modify_body
from aistudio_api.infrastructure.gateway.response_diagnostics import (
    log_stream_candidate_diag,
    log_stream_done_diag,
)
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

logger = logging.getLogger("aistudio")


def _dump_stream_exchange(
    *,
    model: str,
    url: str,
    modified_body: str,
    status_code: int,
    raw_response: str,
) -> None:
    if not settings.dump_raw_response:
        return

    out_dir = Path(settings.dump_raw_response_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_")
    timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "stream_generate_content",
        "model": model,
        "url": url,
        "status_code": status_code,
        "modified_body": json.loads(modified_body),
        "raw_response": raw_response,
    }
    path = out_dir / f"aistudio_stream_generate_content_{safe_model}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("已落盘流式原始请求/响应: %s", path)


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: BrowserSession | None = None):
        self._session = session

    async def stream_chat(
        self,
        *,
        captured: CapturedRequest | None,
        model: str,
        system_instruction: str | None,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        safety_off: bool = False,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not captured:
            raise ValueError("captured request is required")
        if self._session is None:
            raise RuntimeError("browser session is required for streaming xhr replay")

        modified_body = modify_body(
            captured.body,
            model=model,
            contents=contents,
            system_instruction=system_instruction,
            system_instruction_content=system_instruction_content,
            tools=tools,
            safety_settings=safety_settings,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
            safety_off=safety_off,
        )

        parser = IncrementalJSONStreamParser()
        latest_usage: dict | None = None
        raw_parts: list[str] = []
        status_code = 0
        parsed_chunks = 0
        body_chars = 0
        thinking_chars = 0
        tool_call_chunks = 0
        final_finish_reason: int | None = None
        final_finish_message = ""
        final_safety_ratings: list | None = None

        async for event_type, payload in self._session.send_streaming_request(
            body=modified_body,
            timeout_ms=settings.timeout_stream * 1000,
        ):
            if event_type == "status" and payload and not status_code:
                status_code = int(payload)
            elif event_type == "chunk" and payload:
                text_payload = payload.decode("utf-8", errors="replace")
                raw_parts.append(text_payload)
                for parsed_chunk in parser.feed(text_payload):
                    usage = parse_chunk_usage(parsed_chunk)
                    if usage:
                        latest_usage = usage
                    candidate = parse_response_chunk(parsed_chunk)
                    parsed_chunks += 1
                    body_chars += len(candidate.text)
                    thinking_chars += len(candidate.thinking)
                    if candidate.function_calls:
                        tool_call_chunks += 1
                    if candidate.finish_reason is not None:
                        final_finish_reason = candidate.finish_reason
                    if candidate.finish_message:
                        final_finish_message = candidate.finish_message
                    if candidate.safety_ratings:
                        final_safety_ratings = candidate.safety_ratings
                    diag_event_types = []
                    if candidate.thinking:
                        diag_event_types.append("thinking")
                        yield ("thinking", candidate.thinking)
                    if candidate.reasoning_images:
                        diag_event_types.append("reasoning_images")
                        yield ("reasoning_images", candidate.reasoning_images)
                    if candidate.function_calls:
                        diag_event_types.append("tool_calls")
                        yield ("tool_calls", candidate.function_calls)
                    if candidate.images:
                        diag_event_types.append("images")
                        yield ("images", candidate.images)
                    if candidate.text:
                        diag_event_types.append("body")
                        yield ("body", candidate.text)
                    if candidate.thought_signature:
                        diag_event_types.append("thought_signature")
                        yield ("thought_signature", candidate.thought_signature)
                    log_stream_candidate_diag(
                        model=model,
                        chunk_index=parsed_chunks,
                        event_types=diag_event_types,
                        candidate=candidate,
                    )

        raw_response = "".join(raw_parts)
        _dump_stream_exchange(
            model=model,
            url=captured.url,
            modified_body=modified_body,
            status_code=status_code,
            raw_response=raw_response,
        )
        log_stream_done_diag(
            model=model,
            status_code=status_code,
            chunks=parsed_chunks,
            body_chars=body_chars,
            thinking_chars=thinking_chars,
            tool_call_chunks=tool_call_chunks,
            final_finish_reason=final_finish_reason,
            final_finish_message=final_finish_message,
            final_safety_ratings=final_safety_ratings,
        )
        if status_code != 200:
            detail = _summarize_error_body(raw_response)
            if status_code in (401, 403, 429):
                raise classify_error(status_code, raw_response)
            if detail:
                raise RequestError(status_code, detail)
            raise RequestError(status_code, "")

        yield ("usage", latest_usage)
        yield ("done", None)
