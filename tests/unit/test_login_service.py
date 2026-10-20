import asyncio
import sys
import types

import pytest

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
playwright_module = sys.modules.setdefault("playwright", types.ModuleType("playwright"))
async_api_module = sys.modules.setdefault("playwright.async_api", types.ModuleType("playwright.async_api"))
setattr(playwright_module, "async_api", async_api_module)
setattr(async_api_module, "async_playwright", lambda: None)

from aistudio_api.infrastructure.account import login_service as login_module
from aistudio_api.infrastructure.account.login_service import LoginService, LoginSession, LoginStatus


class FakePage:
    def __init__(self, *, close_on_goto: bool = False, navigation: str | None = None, main_navigation: bool = False, app_ready: bool = True) -> None:
        self.url = ""
        self.main_frame = types.SimpleNamespace(url="")
        self._handlers: dict[str, list] = {}
        self._close_on_goto = close_on_goto
        self._navigation = navigation
        self._main_navigation = main_navigation
        self._app_ready = app_ready

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = url
        self.main_frame.url = url
        if self._navigation and "ServiceLogin" in url:
            frame = self.main_frame if self._main_navigation else types.SimpleNamespace(url=self._navigation)
            if self._main_navigation:
                self.url = frame.url = self._navigation
            await self.emit("framenavigated", frame)
        if self._close_on_goto:
            await self.emit("close")

    async def evaluate(self, script: str):
        return "login-test@example.com"

    async def wait_for_selector(self, selector: str, *, state: str, timeout: int):
        if not self._app_ready:
            raise TimeoutError("app not ready")

    def is_closed(self) -> bool:
        return False

    async def emit(self, event: str, *args) -> None:
        for callback in self._handlers.get(event, []):
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result


class FakeContext:
    def __init__(self, page: FakePage, *, authenticated: bool = False) -> None:
        self._page = page
        self._handlers: dict[str, list] = {}
        self._cookies = [{"name": "SID", "value": "test-session", "domain": ".google.com", "path": "/"}] if authenticated else []

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def new_page(self) -> FakePage:
        return self._page

    async def storage_state(self) -> dict:
        return {"cookies": self._cookies, "origins": []}

    async def cookies(self, *args) -> list[dict]:
        return self._cookies


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self._handlers: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def new_context(self, **kwargs) -> FakeContext:
        return self._context

    async def close(self) -> None:
        await self.emit("disconnected")

    async def emit(self, event: str, *args) -> None:
        for callback in self._handlers.get(event, []):
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result


class FakeManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    async def start(self) -> None:
        return None

    async def launch_browser(self, playwright) -> FakeBrowser:
        return self._browser

    async def stop(self) -> None:
        return None


class FakePlaywright:
    async def stop(self) -> None:
        return None


class FakeAsyncPlaywrightStarter:
    async def start(self) -> FakePlaywright:
        return FakePlaywright()


class FakeAccountStore:
    def __init__(self) -> None:
        self.saved = False

    def save_account(self, **kwargs):
        self.saved = True
        raise AssertionError("closed login flow should not save account")


def test_login_session_fails_immediately_when_browser_window_is_closed(monkeypatch):
    page = FakePage(close_on_goto=True)
    context = FakeContext(page)
    browser = FakeBrowser(context)
    manager = FakeManager(browser)
    store = FakeAccountStore()

    monkeypatch.setattr(login_module, "CamoufoxManager", lambda port, headless: manager)
    monkeypatch.setattr(login_module, "describe_browser_backend", lambda: "camoufox")
    monkeypatch.setattr(login_module, "build_browser_context_options", lambda headless=None: {})

    async def fake_maximize_page_window(page, *, headless):
        return None

    async def fake_terminal_login_loop(self, session_id, page, login_done, *, headless):
        await asyncio.sleep(3600)

    monkeypatch.setattr(login_module, "async_maximize_page_window", fake_maximize_page_window)
    monkeypatch.setattr(LoginService, "_terminal_login_loop", fake_terminal_login_loop)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakeAsyncPlaywrightStarter())

    service = LoginService()
    session = LoginSession(session_id="login_test")
    service._sessions[session.session_id] = session

    asyncio.run(
        asyncio.wait_for(
            service._login_worker(
                session.session_id,
                store,
                None,
                headless=False,
                ui_locale=None,
            ),
            timeout=1,
        )
    )

    assert session.status == LoginStatus.FAILED
    assert session.error == "登录窗口已关闭"
    assert store.saved is False


class RecordingAccountStore:
    def __init__(self):
        self.saved_states = []

    def save_account(self, **kwargs):
        self.saved_states.append(kwargs)
        return types.SimpleNamespace(id="acc_test")


@pytest.mark.parametrize("navigation,main_navigation,app_ready,authenticated,success", [
    ("https://www.google.com/recaptcha/api2/anchor", False, True, False, False),
    ("https://www.google.com/recaptcha/api2/bframe", False, True, True, False),
    ("https://aistudio.google.com/app/prompts/new_chat", False, True, True, False),
    ("https://myaccount.google.com/", True, True, True, False),
    ("https://aistudio.google.com.evil.example/", True, True, True, False),
    ("https://aistudio.google.com/app/prompts/new_chat", True, False, True, False),
    ("https://aistudio.google.com/app/prompts/new_chat", True, True, False, False),
    ("https://aistudio.google.com/app/prompts/new_chat", True, True, True, True),
])
def test_login_only_saves_authenticated_main_aistudio_page(monkeypatch, navigation, main_navigation, app_ready, authenticated, success):
    page = FakePage(close_on_goto=not success, navigation=navigation, main_navigation=main_navigation, app_ready=app_ready)
    context = FakeContext(page, authenticated=authenticated)
    browser = FakeBrowser(context)
    store = RecordingAccountStore()
    monkeypatch.setattr(login_module, "CamoufoxManager", lambda port, headless: FakeManager(browser))
    monkeypatch.setattr(login_module, "build_browser_context_options", lambda headless=None: {})

    async def no_maximize(*args, **kwargs):
        pass

    async def no_terminal(*args, **kwargs):
        pass

    monkeypatch.setattr(login_module, "async_maximize_page_window", no_maximize)
    monkeypatch.setattr(LoginService, "_terminal_login_loop", no_terminal)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakeAsyncPlaywrightStarter())
    service = LoginService()
    session = LoginSession(session_id="login_navigation_test")
    service._sessions[session.session_id] = session
    asyncio.run(asyncio.wait_for(service._login_worker(session.session_id, store, None, headless=False, ui_locale=None), timeout=1))

    assert bool(store.saved_states) is success
    assert session.status == (LoginStatus.COMPLETED if success else LoginStatus.FAILED)
    assert session.account_id == ("acc_test" if success else None)
