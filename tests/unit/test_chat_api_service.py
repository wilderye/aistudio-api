import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aistudio_api.api.app import app
from aistudio_api.api.schemas import ChatRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service import handle_chat
from aistudio_api.config import settings


class _CaptureChatClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        class _Output:
            text = "ok"
            thinking = "思考"
            usage = {}
            function_calls = []

        return _Output()

    async def stream_generate_content(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        yield "thinking", "思考"
        yield "body", "ok"
        yield "usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


@pytest.mark.parametrize("stream", [False, True])
def test_openai_http_response_exposes_reasoning_for_sillytavern(monkeypatch, stream):
    from aistudio_api.application import api_service_openai

    async def _noop(*args, **kwargs):
        return None

    gateway = _CaptureChatClient()
    monkeypatch.setattr(settings, "api_keys", frozenset())
    monkeypatch.setattr(runtime_state, "client", gateway)
    monkeypatch.setattr(runtime_state, "busy_lock", asyncio.Semaphore(1))
    monkeypatch.setattr(api_service_openai, "ensure_active_account", _noop)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Origin": "http://localhost:8000"},
        json={"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "你好"}], "stream": stream},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8000"
    assert gateway.calls[0]["kwargs"]["contents"][0].parts[0].text == "你好"
    assert gateway.calls[0]["kwargs"]["tools"] is None
    if stream:
        assert "text/event-stream" in response.headers["content-type"]
        events = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(event) for event in events[:-1]]
        deltas = [choice["delta"] for chunk in chunks for choice in chunk["choices"]]
        assert "".join(delta.get("reasoning_content", "") for delta in deltas) == "思考"
        assert "".join(delta.get("content", "") for delta in deltas) == "ok"
        assert all("thinking" not in delta for delta in deltas)
    else:
        message = response.json()["choices"][0]["message"]
        assert message["reasoning_content"] == "思考"
        assert message["content"] == "ok"
        assert "thinking" not in message


def test_handle_chat_empty_tools_disables_model_defaults(monkeypatch):
    from aistudio_api.application import api_service_openai

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(api_service_openai, "require_busy_lock", lambda: asyncio.Semaphore(1))
    monkeypatch.setattr(api_service_openai, "ensure_active_account", _noop)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    client = _CaptureChatClient()
    req = ChatRequest(
        model="gemma-4-31b-it",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
    )

    asyncio.run(handle_chat(req, client))

    assert len(client.calls) == 1
    assert client.calls[0]["kwargs"]["tools"] == []


def test_normalize_chat_request_keeps_upstream_tool_result_text():
    from aistudio_api.application.chat_service import normalize_chat_request
    from aistudio_api.api.schemas import Message

    messages = [
        Message(role="user", content="What is the weather like in Beijing?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"location": "Beijing"}'},
                }
            ],
        ),
        Message(
            role="tool",
            tool_call_id="call_123",
            content='{"temperature": 24, "condition": "sunny"}',
        ),
    ]

    res = normalize_chat_request(messages, "gemini-3.5-flash")
    contents = res["contents"]

    assert len(contents) == 2

    # Check User Message
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "What is the weather like in Beijing?"

    # Current upstream represents OpenAI tool results as named text markers.
    assert contents[1].role == "user"
    assert contents[1].parts[0].text == (
        '<tool_result name="get_weather">\n'
        '{"temperature": 24, "condition": "sunny"}\n'
        '</tool_result>'
    )
    assert contents[1].parts[0].function_response is None

