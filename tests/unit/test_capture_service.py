import json

import pytest

from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.capture import RequestCaptureService


class DummySession:
    async def capture_template(self, model: str):
        return {
            "url": "https://example.test/GenerateContent",
            "headers": {},
            "body": json.dumps(
                [
                    "models/template-old",
                    [[[[None, "old"]], "user"]],
                    None,
                    [None] * 27,
                    "!old-snapshot",
                    None,
                    None,
                ]
            ),
        }

    async def generate_snapshot(self, contents):
        return "!new-snapshot"


@pytest.mark.asyncio
async def test_capture_uses_requested_model_over_template_model():
    service = RequestCaptureService(DummySession(), SnapshotCache())

    captured = await service.capture(
        prompt="hello",
        model="models/gemini-3.5-flash",
        force_refresh=True,
    )

    assert captured is not None
    assert json.loads(captured.body)[0] == "models/gemini-3.5-flash"
