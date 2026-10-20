import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error, TimeoutError as PlaywrightTimeoutError

from aistudio_api.domain.errors import AuthError
from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session import (
    AI_STUDIO_URL,
    AI_STUDIO_URL_FALLBACK,
    BrowserSession,
)


class FakePage:
    def __init__(self, *, navigation_error=None, has_dms=True, has_input=True):
        self.url = "about:blank"
        self.navigation_error = navigation_error
        self.has_dms = has_dms
        self.has_input = has_input
        self.elapsed = 0.0
        self.closed = False
        self.goto_calls = []
        self.evaluation_errors = []

    def goto(self, url, *, wait_until, timeout):
        self.goto_calls.append((url, wait_until))
        self.url = url
        if self.navigation_error:
            self.navigation_error(self, url, wait_until)

    def evaluate(self, script):
        if self.evaluation_errors:
            raise self.evaluation_errors.pop(0)
        if script == "mw:!!window.default_MakerSuite":
            return self.has_dms
        return {"has_dms": self.has_dms, "has_textarea": self.has_input}

    def query_selector(self, selector):
        return SimpleNamespace(is_visible=lambda: True) if self.has_input else None

    def wait_for_timeout(self, milliseconds):
        self.elapsed += milliseconds / 1000

    def is_closed(self):
        return self.closed

    def title(self):
        return "Google AI Studio"


@pytest.fixture
def session():
    session = object.__new__(BrowserSession)
    session._verify_account_identity_sync = Mock()
    session._save_cookies_sync = Mock()
    return session


def test_busy_network_does_not_block_a_ready_chat_page(session):
    def never_idle(page, url, wait_until):
        if wait_until == "networkidle":
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded")

    page = FakePage(navigation_error=never_idle)
    session._goto_aistudio_sync(page)

    assert page.goto_calls == [(AI_STUDIO_URL, "domcontentloaded")]
    session._verify_account_identity_sync.assert_called_once_with(page)
    session._save_cookies_sync.assert_called_once()


@pytest.mark.parametrize("error", [
    Error('Navigation is interrupted by another navigation to "https://aistudio.google.com/prompts/new_chat"'),
    PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded"),
])
def test_navigation_interruption_or_timeout_checks_the_final_page_before_fallback(session, error):
    def interrupted(page, url, wait_until):
        page.url = AI_STUDIO_URL
        raise error

    page = FakePage(navigation_error=interrupted)
    session._goto_aistudio_sync(page)

    assert len(page.goto_calls) == 1
    assert page.url == AI_STUDIO_URL
    session._save_cookies_sync.assert_called_once()


def test_legacy_fallback_redirecting_to_the_current_page_is_accepted(session):
    def navigate(page, url, wait_until):
        if url == AI_STUDIO_URL:
            raise Error("net::ERR_CONNECTION_RESET")
        page.url = AI_STUDIO_URL
        raise Error("Navigation is interrupted by another navigation")

    page = FakePage(navigation_error=navigate)
    session._goto_aistudio_sync(page)

    assert [url for url, _ in page.goto_calls] == [AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK]
    session._save_cookies_sync.assert_called_once()


@pytest.mark.parametrize("has_dms,has_input", [(False, False), (True, False), (False, True)])
def test_incomplete_page_is_not_saved_or_reported_as_ready(session, monkeypatch, has_dms, has_input):
    page = FakePage(has_dms=has_dms, has_input=has_input)
    monkeypatch.setattr(session_module.time, "monotonic", lambda: page.elapsed)

    with pytest.raises(PlaywrightTimeoutError, match="页面未准备完成"):
        session._goto_aistudio_sync(page)
    session._verify_account_identity_sync.assert_not_called()
    session._save_cookies_sync.assert_not_called()


def test_google_login_redirect_fails_immediately_without_trying_another_chat_url(session):
    def login_redirect(page, url, wait_until):
        page.url = "https://accounts.google.com/v3/signin/identifier"

    page = FakePage(navigation_error=login_redirect)
    with pytest.raises(AuthError, match="重新登录"):
        session._goto_aistudio_sync(page)
    assert len(page.goto_calls) == 1
    session._save_cookies_sync.assert_not_called()


def test_a_new_execution_context_during_navigation_is_rechecked(session):
    page = FakePage()
    page.evaluation_errors = [Error("Execution context was destroyed, most likely because of a navigation")]

    session._goto_aistudio_sync(page)
    assert len(page.goto_calls) == 1
    session._save_cookies_sync.assert_called_once()


def test_closed_page_does_not_trigger_another_navigation(session):
    def close(page, url, wait_until):
        page.closed = True
        raise Error("Target page, context or browser has been closed")

    page = FakePage(navigation_error=close)
    with pytest.raises(Error, match="has been closed"):
        session._goto_aistudio_sync(page)
    assert len(page.goto_calls) == 1


@pytest.mark.parametrize("fresh_profile", [False, True])
def test_chromium_opens_the_chat_page_once_after_selecting_auth(session, tmp_path, monkeypatch, fresh_profile):
    profile = tmp_path / "profile"
    profile.mkdir()
    if not fresh_profile:
        (profile / "Preferences").write_text("{}")
    auth_file = tmp_path / "auth.json"
    cookies = [{"name": "SID", "value": "test-cookie", "domain": ".google.com", "path": "/"}]
    auth_file.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
    session._profile_dir = str(profile)
    session._auth_file = str(auth_file)
    page = FakePage()
    context = SimpleNamespace(pages=[page], add_cookies=Mock())
    monkeypatch.setattr(session_module, "sync_launch_persistent_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(session_module, "sync_maximize_page_window", lambda *args, **kwargs: None)
    session._bootstrap_google_session_sync = Mock()
    session._goto_aistudio_sync = Mock(side_effect=RuntimeError("page not ready"))
    session._install_hooks_sync = Mock()

    with pytest.raises(RuntimeError, match="page not ready"):
        session._ensure_browser_chromium_sync(0)
    session._goto_aistudio_sync.assert_called_once_with(page)
    session._install_hooks_sync.assert_not_called()
    if fresh_profile:
        context.add_cookies.assert_called_once_with(cookies)
        session._bootstrap_google_session_sync.assert_called_once_with(page)
    else:
        context.add_cookies.assert_not_called()
