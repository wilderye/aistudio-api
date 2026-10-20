import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistudio_api.api.dependencies import get_account_service, get_runtime_state
from aistudio_api.api.routes_accounts import router
from aistudio_api.api.routes_system import protected_router
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account import account_store as store_module
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.session import BrowserSession


class FakeBrowserSession:
    def __init__(self, auth_file, failures=()):
        self.auth_file = str(auth_file) if auth_file else None
        self.ready = self.auth_file is not None
        self.failures = set(failures)
        self.switches = []
        self.operation_lock = asyncio.Lock()

    async def switch_auth(self, auth_file):
        self.switches.append(auth_file)
        self.auth_file = auth_file
        self.ready = False

    async def ensure_context(self):
        account_id = Path(self.auth_file).parent.name if self.auth_file else None
        if account_id in self.failures:
            raise RuntimeError("caller does not have permission")
        self.ready = True


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_resolve_legacy_auth_file", lambda: None)
    store = AccountStore(tmp_path / "accounts")
    for account_id in ("old", "new"):
        store.save_account(account_id, None, {"cookies": []}, account_id=account_id)
    store.set_active_account("old")
    service = AccountService(store, None)
    browser = FakeBrowserSession(store.get_auth_path("old"))
    cache = SimpleNamespace(clear=Mock())
    return store, service, browser, cache


def test_activation_commits_only_after_the_browser_is_ready(accounts):
    store, service, browser, cache = accounts
    original_ensure = browser.ensure_context

    async def ensure():
        assert store.get_active_account().id == "old"
        await original_ensure()

    browser.ensure_context = ensure
    result = asyncio.run(service.activate_account("new", browser, cache))

    assert result.id == store.get_active_account().id == "new"
    assert browser.auth_file == str(store.get_auth_path("new"))
    assert browser.ready
    cache.clear.assert_called_once()


@pytest.mark.parametrize("keep_cache", [False, True])
def test_failed_activation_restores_the_previous_account(accounts, keep_cache):
    store, service, browser, cache = accounts
    browser.failures.add("new")

    with pytest.raises(RuntimeError, match="已恢复原账号"):
        asyncio.run(service.activate_account("new", browser, cache, keep_snapshot_cache=keep_cache))

    assert store.get_active_account().id == "old"
    assert browser.auth_file == str(store.get_auth_path("old"))
    assert browser.ready
    cache.clear.assert_called_once()


def test_failed_restore_clears_the_active_account_and_failed_browser(accounts):
    store, service, browser, cache = accounts
    browser.failures.update(("old", "new"))

    with pytest.raises(RuntimeError, match="没有激活账号"):
        asyncio.run(service.activate_account("new", browser, cache))

    assert store.get_active_account() is None
    assert browser.auth_file is None
    assert not browser.ready
    assert AccountStore(store._accounts_dir).get_active_account() is None


def test_first_saved_account_is_not_reported_as_active(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_resolve_legacy_auth_file", lambda: None)
    store = AccountStore(tmp_path / "accounts")
    account = store.save_account("new", None, {"cookies": []}, account_id="new")
    browser = FakeBrowserSession(None, failures=("new",))

    assert store.get_active_account() is None
    with pytest.raises(RuntimeError, match="没有激活账号"):
        asyncio.run(AccountService(store, None).activate_account(account.id, browser, None))
    assert store.get_active_account() is None
    assert browser.auth_file is None


def test_active_record_write_failure_restores_the_browser(accounts, monkeypatch):
    store, service, browser, cache = accounts
    registry_path = store._registry_path
    original_write = Path.write_text

    def fail_registry_write(path, *args, **kwargs):
        if path == registry_path:
            raise OSError("disk unavailable")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_registry_write)
    with pytest.raises(RuntimeError, match="已恢复原账号"):
        asyncio.run(service.activate_account("new", browser, cache))
    assert store.get_active_account().id == "old"
    assert browser.auth_file == str(store.get_auth_path("old"))
    assert browser.ready


def test_cancelled_activation_restores_before_releasing_the_lock(accounts):
    store, service, browser, cache = accounts
    original_ensure = browser.ensure_context

    async def ensure():
        if Path(browser.auth_file).parent.name == "new":
            raise asyncio.CancelledError()
        await original_ensure()

    browser.ensure_context = ensure
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.activate_account("new", browser, cache))
    assert store.get_active_account().id == "old"
    assert browser.ready
    assert browser.auth_file == str(store.get_auth_path("old"))


@pytest.mark.parametrize("restore_fails", [False, True])
@pytest.mark.parametrize("endpoint", ["/accounts/new/activate", "/rotation/next"])
def test_activation_api_returns_failure_and_consistent_active_status(accounts, restore_fails, endpoint):
    store, service, browser, cache = accounts
    browser.failures = {"new", "old"} if restore_fails else {"new"}
    async def next_account():
        return store.get_account("new")

    state = SimpleNamespace(
        client=SimpleNamespace(_session=browser), snapshot_cache=cache, busy_lock=asyncio.Lock(),
        account_service=service, rotator=SimpleNamespace(get_next_account=next_account),
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(protected_router)
    app.dependency_overrides[get_account_service] = lambda: service
    app.dependency_overrides[get_runtime_state] = lambda: state

    with TestClient(app) as client:
        response = client.post(endpoint)
        assert response.status_code == 503
        assert "detail" in response.json()
        active = client.get("/accounts/active")
        if restore_fails:
            assert active.status_code == 404
            assert "没有激活账号" in response.json()["detail"]
        else:
            assert active.status_code == 200
            assert active.json()["id"] == "old"
            assert "已恢复原账号" in response.json()["detail"]


def test_failed_browser_initialization_does_not_leave_a_reusable_context(monkeypatch):
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "browser_engine", "chromium")
    monkeypatch.setattr(settings, "auth_file", "test-auth.json")
    session = BrowserSession(9222)
    failed_context = SimpleNamespace(close=Mock())

    def fail_launch(_started):
        session._ctx = failed_context
        session._hook_page = SimpleNamespace(is_closed=lambda: False)
        raise RuntimeError("page timed out")

    monkeypatch.setattr(session, "_ensure_browser_chromium_sync", fail_launch)
    try:
        with pytest.raises(RuntimeError, match="page timed out"):
            asyncio.run(session.ensure_context())
        assert session._ctx is None
        assert session._hook_page is None
        failed_context.close.assert_called_once()
    finally:
        session._close_sync()
        session._executor.shutdown()


def test_active_status_waits_for_failed_switch_to_finish_restoring(accounts):
    store, service, browser, cache = accounts

    async def exercise():
        restoring = asyncio.Event()
        finish_restore = asyncio.Event()
        original_ensure = browser.ensure_context

        async def ensure():
            if Path(browser.auth_file).parent.name == "new":
                raise RuntimeError("caller does not have permission")
            restoring.set()
            await finish_restore.wait()
            await original_ensure()

        browser.ensure_context = ensure
        activation = asyncio.create_task(service.activate_account("new", browser, cache))
        await restoring.wait()
        status = asyncio.create_task(service.get_active_account_status())
        await asyncio.sleep(0)
        assert not status.done()
        finish_restore.set()
        with pytest.raises(RuntimeError, match="已恢复原账号"):
            await activation
        assert (await status).id == "old"
        assert browser.ready

    asyncio.run(asyncio.wait_for(exercise(), timeout=2))


@pytest.mark.parametrize("request_kind", ["text", "stream", "image"])
def test_switch_waits_until_the_inflight_browser_request_finishes(accounts, monkeypatch, request_kind):
    from aistudio_api.infrastructure.gateway import client as client_module

    store, service, browser, cache = accounts
    client = object.__new__(client_module.AIStudioClient)
    client._session = browser
    client._dump_raw_exchange = lambda **kwargs: None
    monkeypatch.setattr(client_module, "modify_body", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(client_module, "parse_text_output", lambda _: SimpleNamespace(model=""))
    monkeypatch.setattr(client_module, "parse_image_output", lambda _: SimpleNamespace(model="", images=[]))

    async def exercise():
        generating = asyncio.Event()
        finish_generation = asyncio.Event()

        async def capture(*args, **kwargs):
            original_auth = browser.auth_file
            generating.set()
            await finish_generation.wait()
            assert browser.auth_file == original_auth
            return SimpleNamespace(body="[]")

        async def replay(*args, **kwargs):
            return 200, b"[]"

        async def stream(**kwargs):
            yield "text", "OK"

        client.capture_request = capture
        client._replay_service = SimpleNamespace(replay=replay)
        client._streaming_gateway = SimpleNamespace(stream_chat=stream)

        async def request():
            if request_kind == "stream":
                return [event async for event in client.stream_generate_content(capture_prompt="hello")]
            if request_kind == "image":
                return await client.generate_image("hello")
            return await client.generate_content(capture_prompt="hello")

        generation = asyncio.create_task(request())
        await generating.wait()
        activation = asyncio.create_task(service.activate_account("new", browser, cache))
        await asyncio.sleep(0)
        assert browser.switches == []
        assert not activation.done()
        finish_generation.set()
        await generation
        await activation
        assert browser.auth_file == str(store.get_auth_path("new"))
        assert store.get_active_account().id == "new"

    asyncio.run(asyncio.wait_for(exercise(), timeout=2))


def test_capture_does_not_reuse_another_browser_pages_request_headers():
    from aistudio_api.infrastructure.gateway.capture import RequestCaptureService

    async def exercise():
        current_account = "old"

        async def template(_model):
            return {"url": "https://example.invalid/generate", "headers": {"account": current_account}, "body": '["model"]'}

        capture = RequestCaptureService(SimpleNamespace(capture_template=template), None)
        assert (await capture._ensure_template("model")).headers["account"] == "old"
        current_account = "new"
        assert (await capture._ensure_template("model")).headers["account"] == "new"

    asyncio.run(exercise())


def test_automatic_rotation_failure_preserves_the_previous_account(accounts, monkeypatch):
    from aistudio_api.application import api_service_common

    store, service, browser, cache = accounts
    browser.failures.add("new")

    async def next_account():
        return store.get_account("new")

    monkeypatch.setattr(api_service_common, "runtime_state", SimpleNamespace(
        client=SimpleNamespace(_session=browser), account_service=service,
        snapshot_cache=cache, rotator=SimpleNamespace(get_next_account=next_account),
    ))
    assert asyncio.run(api_service_common.try_switch_account()) is False
    assert store.get_active_account().id == "old"
    assert browser.auth_file == str(store.get_auth_path("old"))
    assert browser.ready


def test_deleting_an_account_does_not_mark_another_account_as_activated(accounts):
    store, service, browser, cache = accounts
    assert store.delete_account("old")
    assert store.get_active_account() is None
    assert store.get_account("new") is not None
