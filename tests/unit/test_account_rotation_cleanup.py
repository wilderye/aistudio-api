from types import SimpleNamespace

import pytest

from aistudio_api.application import api_service_common


class FakeExclusive:
    def __init__(self, gate, current_shared):
        self.gate = gate
        self.current_shared = current_shared

    async def __aenter__(self):
        self.gate.events.append(("enter", self.current_shared))

    async def __aexit__(self, exc_type, exc, tb):
        self.gate.events.append(("exit", self.current_shared))


class FakeGate:
    def __init__(self):
        self.events = []

    def exclusive(self, *, current_shared=False):
        return FakeExclusive(self, current_shared)


class FakeRotator:
    async def get_next_account(self):
        return SimpleNamespace(id="acc-2")


class FakeAccountService:
    def __init__(self):
        self.calls = []
        self.active_account = SimpleNamespace(id="acc-1")

    async def activate_account(self, account_id, browser_session, snapshot_cache, busy_lock, *, keep_snapshot_cache):
        self.calls.append((account_id, browser_session, snapshot_cache, busy_lock, keep_snapshot_cache))
        return SimpleNamespace(id=account_id)

    def get_active_account(self):
        return self.active_account


def save_runtime_state():
    return {
        "rotator": api_service_common.runtime_state.rotator,
        "account_service": api_service_common.runtime_state.account_service,
        "client": api_service_common.runtime_state.client,
        "snapshot_cache": api_service_common.runtime_state.snapshot_cache,
        "busy_lock": api_service_common.runtime_state.busy_lock,
    }


def restore_runtime_state(saved):
    for key, value in saved.items():
        setattr(api_service_common.runtime_state, key, value)


@pytest.mark.asyncio
async def test_try_switch_account_uses_current_shared_exclusive_gate():
    gate = FakeGate()
    account_service = FakeAccountService()
    original = save_runtime_state()
    api_service_common.runtime_state.rotator = FakeRotator()
    api_service_common.runtime_state.account_service = account_service
    api_service_common.runtime_state.client = SimpleNamespace(_session="session")
    api_service_common.runtime_state.snapshot_cache = "snapshot-cache"
    api_service_common.runtime_state.busy_lock = gate
    try:
        result = await api_service_common.try_switch_account(current_request_holds_gate=True)
    finally:
        restore_runtime_state(original)

    assert result is True
    assert gate.events == [("enter", True), ("exit", True)]
    assert account_service.calls == [("acc-2", "session", "snapshot-cache", None, False)]


@pytest.mark.asyncio
async def test_ensure_active_account_switches_with_current_shared_gate_when_no_active_account():
    gate = FakeGate()
    account_service = FakeAccountService()
    account_service.active_account = None
    original = save_runtime_state()
    api_service_common.runtime_state.rotator = FakeRotator()
    api_service_common.runtime_state.account_service = account_service
    api_service_common.runtime_state.client = SimpleNamespace(_session="session")
    api_service_common.runtime_state.snapshot_cache = "snapshot-cache"
    api_service_common.runtime_state.busy_lock = gate
    try:
        await api_service_common.ensure_active_account(0)
    finally:
        restore_runtime_state(original)

    assert gate.events == [("enter", True), ("exit", True)]
    assert account_service.calls == [("acc-2", "session", "snapshot-cache", None, False)]


@pytest.mark.asyncio
async def test_ensure_active_account_does_not_switch_after_first_attempt():
    gate = FakeGate()
    account_service = FakeAccountService()
    account_service.active_account = None
    original = save_runtime_state()
    api_service_common.runtime_state.rotator = FakeRotator()
    api_service_common.runtime_state.account_service = account_service
    api_service_common.runtime_state.client = SimpleNamespace(_session="session")
    api_service_common.runtime_state.snapshot_cache = "snapshot-cache"
    api_service_common.runtime_state.busy_lock = gate
    try:
        await api_service_common.ensure_active_account(1)
    finally:
        restore_runtime_state(original)

    assert gate.events == []
    assert account_service.calls == []
