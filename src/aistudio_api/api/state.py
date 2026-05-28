"""Shared API runtime state."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aistudio_api.infrastructure.gateway.client import AIStudioClient


class RequestGate:
    """Shared request slots plus an exclusive maintenance mode."""

    def __init__(self, max_concurrency: int):
        self._max_concurrency = max(1, int(max_concurrency))
        self._active_shared = 0
        self._holders: dict[asyncio.Task, int] = defaultdict(int)
        self._suspended_holders: dict[asyncio.Task, int] = defaultdict(int)
        self._exclusive_active = False
        self._exclusive_waiting = 0
        self._condition = asyncio.Condition()

    def locked(self) -> bool:
        return (
            self._exclusive_active
            or self._exclusive_waiting > 0
            or self._active_shared >= self._max_concurrency
        )

    async def __aenter__(self):
        async with self._condition:
            while (
                self._exclusive_active
                or self._exclusive_waiting > 0
                or self._active_shared >= self._max_concurrency
            ):
                await self._condition.wait()
            self._active_shared += 1
            self._holders[asyncio.current_task()] += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with self._condition:
            task = asyncio.current_task()
            if self._holders[task] > 0:
                self._holders[task] -= 1
                if self._holders[task] == 0:
                    del self._holders[task]
                self._active_shared -= 1
            elif self._suspended_holders[task] > 0:
                self._suspended_holders[task] -= 1
                if self._suspended_holders[task] == 0:
                    del self._suspended_holders[task]
            else:
                raise RuntimeError("RequestGate shared slot was not held by this task")
            self._condition.notify_all()

    def _restore_suspended_locked(self, task: asyncio.Task, count: int) -> None:
        if count <= 0:
            return
        self._suspended_holders[task] -= count
        if self._suspended_holders[task] == 0:
            del self._suspended_holders[task]
        self._holders[task] += count
        self._active_shared += count

    @asynccontextmanager
    async def exclusive(self, *, current_shared: bool = False):
        task = asyncio.current_task()
        suspended_count = 0
        entered = False
        async with self._condition:
            try:
                if current_shared:
                    suspended_count = self._holders.get(task, 0)
                    if suspended_count <= 0:
                        raise RuntimeError("current_shared=True requires the current task to hold a shared slot")
                    self._holders.pop(task, None)
                    self._suspended_holders[task] += suspended_count
                    self._active_shared -= suspended_count
                    self._condition.notify_all()

                self._exclusive_waiting += 1
                try:
                    while self._exclusive_active or self._active_shared > 0:
                        await self._condition.wait()
                    self._exclusive_active = True
                    entered = True
                finally:
                    self._exclusive_waiting -= 1
            except BaseException:
                self._restore_suspended_locked(task, suspended_count)
                self._condition.notify_all()
                raise
        try:
            yield self
        finally:
            async with self._condition:
                if entered:
                    self._exclusive_active = False
                self._restore_suspended_locked(task, suspended_count)
                self._condition.notify_all()


@dataclass
class RuntimeState:
    client: AIStudioClient | None = None
    busy_lock: RequestGate | None = None
    browser_port: int = 9222
    snapshot_cache: object | None = None  # SnapshotCache 实例
    account_service: object | None = None  # AccountService 实例
    rotator: object | None = None  # AccountRotator 实例
    anthropic_tool_context: dict[str, dict] = field(default_factory=dict)
    model_stats: dict[str, dict] = field(
        default_factory=lambda: defaultdict(
            lambda: {
                "requests": 0,
                "success": 0,
                "rate_limited": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "last_used": None,
            }
        )
    )

    def record(self, model: str, event: str, usage: dict | None = None):
        stats = self.model_stats[model]
        stats["requests"] += 1
        stats[event] += 1
        stats["last_used"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        if usage and event == "success":
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tt = usage.get("total_tokens", 0)
            stats["prompt_tokens"] += pt if isinstance(pt, int) else 0
            stats["completion_tokens"] += ct if isinstance(ct, int) else 0
            stats["total_tokens"] += tt if isinstance(tt, int) else 0


runtime_state = RuntimeState()
