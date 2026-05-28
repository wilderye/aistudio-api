import asyncio

import pytest

from aistudio_api.api.state import RequestGate


@pytest.mark.asyncio
async def test_exclusive_waits_for_shared_request_to_finish():
    gate = RequestGate(max_concurrency=2)
    events = []

    async def shared():
        async with gate:
            events.append("shared-start")
            await asyncio.sleep(0.05)
            events.append("shared-end")

    async def exclusive():
        await asyncio.sleep(0.01)
        async with gate.exclusive():
            events.append("exclusive")

    await asyncio.gather(shared(), exclusive())

    assert events == ["shared-start", "shared-end", "exclusive"]


@pytest.mark.asyncio
async def test_exclusive_current_waits_for_other_shared_requests():
    gate = RequestGate(max_concurrency=2)
    events = []

    async def other_shared():
        async with gate:
            events.append("other-start")
            await asyncio.sleep(0.05)
            events.append("other-end")

    async def current_shared_then_upgrade():
        async with gate:
            events.append("current-start")
            async with gate.exclusive(current_shared=True):
                events.append("exclusive-current")
            events.append("current-end")

    await asyncio.gather(other_shared(), current_shared_then_upgrade())

    assert events == ["other-start", "current-start", "other-end", "exclusive-current", "current-end"]


@pytest.mark.asyncio
async def test_new_shared_waits_while_exclusive_is_pending():
    gate = RequestGate(max_concurrency=2)
    events = []

    async def long_shared():
        async with gate:
            events.append("long-start")
            await asyncio.sleep(0.05)
            events.append("long-end")

    async def exclusive():
        await asyncio.sleep(0.01)
        async with gate.exclusive():
            events.append("exclusive-start")
            await asyncio.sleep(0.05)
            events.append("exclusive-end")

    async def late_shared():
        await asyncio.sleep(0.02)
        async with gate:
            events.append("late-shared")

    await asyncio.gather(long_shared(), exclusive(), late_shared())

    assert events == ["long-start", "long-end", "exclusive-start", "exclusive-end", "late-shared"]


@pytest.mark.asyncio
async def test_two_current_shared_upgrades_do_not_deadlock():
    gate = RequestGate(max_concurrency=2)
    events = []
    shared_count = 0
    shared_count_lock = asyncio.Lock()
    both_shared = asyncio.Event()

    async def upgrader(name: str):
        nonlocal shared_count
        async with gate:
            events.append(f"{name}-shared")
            async with shared_count_lock:
                shared_count += 1
                if shared_count == 2:
                    both_shared.set()
            await both_shared.wait()
            async with gate.exclusive(current_shared=True):
                events.append(f"{name}-exclusive")

    await asyncio.wait_for(asyncio.gather(upgrader("a"), upgrader("b")), timeout=1)

    assert sorted(events[:2]) == ["a-shared", "b-shared"]
    assert sorted(events[2:]) == ["a-exclusive", "b-exclusive"]


@pytest.mark.asyncio
async def test_current_shared_upgrade_requires_shared_holder():
    gate = RequestGate(max_concurrency=1)

    with pytest.raises(RuntimeError, match="current_shared=True requires"):
        async with gate.exclusive(current_shared=True):
            pass
