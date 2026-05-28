from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aistudio_api.api.routes_system import reset_session


class FakeExclusive:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("exclusive-enter")

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("exclusive-exit")


class FakeGate:
    def __init__(self):
        self.events = []

    def exclusive(self):
        return FakeExclusive(self.events)


class FakeClient:
    async def reset_session(self, *, cleanup_old: bool):
        return {"ok": True, "cleanup_old": cleanup_old}


@pytest.mark.asyncio
async def test_reset_session_route_uses_exclusive_gate():
    gate = FakeGate()
    runtime_state = SimpleNamespace(client=FakeClient(), busy_lock=gate)

    result = await reset_session(runtime_state=runtime_state)

    assert result == {"ok": True, "cleanup_old": True}
    assert gate.events == ["exclusive-enter", "exclusive-exit"]


@pytest.mark.asyncio
async def test_reset_session_route_requires_client_and_gate():
    runtime_state = SimpleNamespace(client=None, busy_lock=None)

    with pytest.raises(HTTPException) as exc_info:
        await reset_session(runtime_state=runtime_state)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "service not ready"
