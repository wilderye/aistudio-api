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
