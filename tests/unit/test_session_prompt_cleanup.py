from aistudio_api.infrastructure.gateway.session import (
    BrowserSession,
    DELETE_PROMPT_URL,
    extract_prompt_resource_from_url,
    headers_from_captured_request,
)


def test_extract_prompt_resource_from_saved_prompt_url():
    assert (
        extract_prompt_resource_from_url(
            "https://aistudio.google.com/prompts/1abcDEF_123?save=true"
        )
        == "prompts/1abcDEF_123"
    )


def test_extract_prompt_resource_from_plain_prompt_url():
    assert (
        extract_prompt_resource_from_url("https://aistudio.google.com/prompts/1abcDEF_123")
        == "prompts/1abcDEF_123"
    )


def test_extract_prompt_resource_returns_none_for_new_chat():
    assert (
        extract_prompt_resource_from_url(
            "https://aistudio.google.com/prompts/new_chat?model=gemma-4-31b-it"
        )
        is None
    )


def test_delete_prompt_url_is_verified_rpc():
    assert DELETE_PROMPT_URL.endswith("MakerSuiteService/DeletePrompt")


def test_headers_from_captured_request_filters_browser_managed_headers():
    headers = headers_from_captured_request(
        {
            "headers": {
                "authorization": "SAPISIDHASH abc",
                "content-length": "123",
                "host": "alkalimakersuite-pa.clients6.google.com",
                "x-goog-api-key": "key",
            }
        }
    )

    assert headers == {
        "authorization": "SAPISIDHASH abc",
        "x-goog-api-key": "key",
    }


class ExplodingPage:
    def is_closed(self):
        return False

    def evaluate(self, script, args):
        raise RuntimeError("page evaluate exploded")


def test_delete_prompt_returns_error_when_page_evaluate_raises():
    session = BrowserSession.__new__(BrowserSession)

    result = session._delete_prompt_sync(
        "prompts/abc123",
        page=ExplodingPage(),
        headers={"authorization": "SAPISIDHASH abc"},
    )

    assert result["attempted"] is True
    assert result["ok"] is False
    assert result["error"] == "DeletePrompt exception: page evaluate exploded"


class Closeable:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.closed = False

    def close(self):
        self.closed = True
        self.events.append(("close", self.name))


class PromptPage:
    def __init__(self, url):
        self.url = url

    def is_closed(self):
        return False


def test_switch_auth_cleanup_deletes_old_prompt_after_new_session_is_ready():
    events = []
    old_ctx = Closeable("old-ctx", events)
    old_page = PromptPage("https://aistudio.google.com/prompts/old123")
    session = BrowserSession.__new__(BrowserSession)
    session._ctx = old_ctx
    session._browser = None
    session._cf = None
    session._playwright = None
    session._hook_page = old_page
    session._templates = {"model": {"headers": {"authorization": "SAPISIDHASH old", "host": "ignored"}}}
    session._bootstrap_template = None
    session._snap_key = "old-snap"
    session._auth_file = "old-auth.json"
    session._profile_dir = "old-profile"

    def ensure_browser():
        events.append(("ensure-browser", session._auth_file))
        session._ctx = Closeable("new-ctx", events)
        session._hook_page = PromptPage("https://aistudio.google.com/prompts/new456")
        return session._ctx

    def ensure_botguard():
        events.append("ensure-botguard")
        return session._hook_page

    def delete_prompt(prompt_resource, *, page=None, headers=None):
        events.append(("delete", prompt_resource, page, headers))
        return {"attempted": True, "ok": True, "error": None}

    session._ensure_browser_sync = ensure_browser
    session._ensure_botguard_service_sync = ensure_botguard
    session._delete_prompt_sync = delete_prompt

    result = session._switch_auth_sync("new-auth.json", cleanup_old=True)

    assert result == {
        "ok": True,
        "old_prompt": "prompts/old123",
        "cleanup_attempted": True,
        "cleanup_ok": True,
        "cleanup_error": None,
    }
    assert events == [
        ("ensure-browser", "new-auth.json"),
        "ensure-botguard",
        ("delete", "prompts/old123", old_page, {"authorization": "SAPISIDHASH old"}),
        ("close", "old-ctx"),
    ]
