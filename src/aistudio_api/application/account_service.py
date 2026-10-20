"""账号管理应用服务，协调 account_store 和 login_service。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aistudio_api.infrastructure.account.account_store import AccountStore, AccountMeta
from aistudio_api.infrastructure.account.login_service import LoginService, LoginSession

logger = logging.getLogger("aistudio.account")


class AccountActivationError(RuntimeError):
    """账号未能启用；浏览器恢复完成后才向调用方报告失败。"""


class AccountService:
    """账号管理服务。"""

    def __init__(
        self,
        account_store: AccountStore,
        login_service: LoginService,
    ) -> None:
        self._store = account_store
        self._login = login_service
        self._activation_lock = asyncio.Lock()

    def list_accounts(self) -> list[AccountMeta]:
        """列出所有账号。"""
        return self._store.list_accounts()

    def get_account(self, account_id: str) -> AccountMeta | None:
        """获取单个账号。"""
        return self._store.get_account(account_id)

    def get_active_account(self) -> AccountMeta | None:
        """获取当前活跃账号。"""
        return self._store.get_active_account()

    async def get_active_account_status(self) -> AccountMeta | None:
        """切换或恢复结束后，返回可供页面显示的账号。"""
        async with self._activation_lock:
            return self.get_active_account()

    async def start_login(
        self,
        name: str | None = None,
        *,
        headless: bool = False,
        ui_locale: str | None = None,
    ) -> str:
        """启动登录流程，返回 session_id。"""
        return await self._login.start_login(
            self._store,
            name,
            headless=headless,
            ui_locale=ui_locale,
        )

    def get_login_status(self, session_id: str) -> LoginSession | None:
        """获取登录状态。"""
        return self._login.get_status(session_id)

    async def activate_account(
        self,
        account_id: str,
        browser_session: Any,
        snapshot_cache: Any,
        busy_lock: Any = None,  # None = skip lock (caller already holds it)
        keep_snapshot_cache: bool = False,
    ) -> AccountMeta | None:
        """切换到指定账号。

        Args:
            account_id: 目标账号 ID
            browser_session: BrowserSession 实例
            snapshot_cache: SnapshotCache 实例
            busy_lock: 请求并发限制。None 表示调用方已占用请求名额
            keep_snapshot_cache: 是否保留 snapshot 缓存（默认 False，避免切号后复用旧 snapshot）

        Returns:
            切换后的账号元数据，或 None（如果账号不存在）
        """
        async def _do_switch():
            # busy_lock 可以是允许多个请求的 Semaphore；账号切换本身仍须串行。
            async with self._activation_lock, browser_session.operation_lock:
                account = self._store.get_account(account_id)
                auth_path = self._store.get_auth_path_optional(account_id, require_exists=False)
                if account is None or auth_path is None:
                    return None

                previous = self._store.get_active_account()
                previous_auth = (
                    self._store.get_auth_path_optional(previous.id, require_exists=False)
                    if previous else None
                )
                try:
                    await browser_session.switch_auth(str(auth_path))
                    await browser_session.ensure_context()
                    if not keep_snapshot_cache and snapshot_cache is not None:
                        snapshot_cache.clear()
                    account = self._store.set_active_account(account_id)
                    if account is None:
                        raise RuntimeError("账号记录已被删除，无法启用")
                except (Exception, asyncio.CancelledError) as exc:
                    logger.warning("账号 %s 启用失败，正在恢复原账号: %s", account_id, exc)
                    # 即使恢复同一账号，也已更换浏览器页面，旧 snapshot 不可再用。
                    if snapshot_cache is not None:
                        snapshot_cache.clear()
                    restored = False
                    if previous_auth is not None:
                        try:
                            await browser_session.switch_auth(str(previous_auth))
                            await browser_session.ensure_context()
                            restored = True
                        except Exception:
                            logger.exception("恢复原账号 %s 失败", previous.id)
                    if not restored:
                        self._store.clear_active_account()
                        await browser_session.switch_auth(None)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    reason = str(exc).splitlines()[0][:240] if str(exc) else type(exc).__name__
                    status = (
                        f"已恢复原账号 {previous.id}"
                        if restored else "当前没有激活账号，请重新启用账号"
                    )
                    raise AccountActivationError(f"账号启用失败；{status}。原因：{reason}") from exc

                logger.info("已切换到账号: %s (%s)", account_id, account.name)
                return account

        # 复用请求并发限制；浏览器操作由 operation_lock 互斥。
        if busy_lock is not None:
            async with busy_lock:
                return await _do_switch()
        else:
            return await _do_switch()

    def delete_account(self, account_id: str) -> bool:
        """删除账号。"""
        return self._store.delete_account(account_id)

    def update_account(self, account_id: str, name: str) -> AccountMeta | None:
        """更新账号名称。"""
        return self._store.update_account(account_id, name)
