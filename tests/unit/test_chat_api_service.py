import asyncio

from aistudio_api.api.schemas import ChatRequest
from aistudio_api.application.api_service import handle_chat


class _CaptureChatClient:
    def __init__(self):
        self.calls = []

    async def generate_content(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        class _Output:
            text = "ok"
            thinking = ""
            usage = {}
            function_calls = []

        return _Output()


class _CaptureStreamClient:
    def __init__(self):
        self.calls = []

    async def stream_generate_content(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        yield "body", "ok"


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


def test_handle_chat_defaults_safety_off_for_openai_compat(monkeypatch):
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
    )

    asyncio.run(handle_chat(req, client))

    assert client.calls[0]["kwargs"]["safety_off"] is True


def test_handle_chat_allows_explicit_safety_off_false(monkeypatch):
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
        safety_off=False,
    )

    asyncio.run(handle_chat(req, client))

    assert client.calls[0]["kwargs"]["safety_off"] is False


def test_handle_chat_stream_defaults_safety_off_for_openai_compat(monkeypatch):
    from aistudio_api.api.state import runtime_state
    from aistudio_api.application import api_service_openai

    async def _noop(*args, **kwargs):
        return None

    original_busy_lock = runtime_state.busy_lock
    runtime_state.busy_lock = asyncio.Semaphore(1)
    monkeypatch.setattr(api_service_openai, "require_busy_lock", lambda: asyncio.Semaphore(1))
    monkeypatch.setattr(api_service_openai, "ensure_active_account", _noop)
    monkeypatch.setattr(api_service_openai, "record_rotator_event", lambda *args, **kwargs: None)

    client = _CaptureStreamClient()
    req = ChatRequest(
        model="gemma-4-31b-it",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    async def _run():
        response = await handle_chat(req, client)
        async for _ in response.body_iterator:
            pass

    try:
        asyncio.run(_run())
    finally:
        runtime_state.busy_lock = original_busy_lock

    assert client.calls[0]["kwargs"]["safety_off"] is True
