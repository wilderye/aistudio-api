"""Unified CLI entrypoint for local development and installed usage."""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio unified entrypoint")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser("server", help="启动 OpenAI 兼容 API 服务")
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument("--browser-port", type=int, default=settings.browser_port)
    server_parser.add_argument("--camoufox-port", type=int, dest="browser_port", help=argparse.SUPPRESS)

    client_parser = subparsers.add_parser("client", help="发送一次客户端请求")
    client_parser.add_argument("prompt", nargs="?", default="你好", help="用户消息")
    client_parser.add_argument("--model", "-m", help="模型名称")
    client_parser.add_argument("--system", "-s", help="系统指令")
    client_parser.add_argument("--search", action="store_true", help="启用 Google Search")
    client_parser.add_argument("--code", action="store_true", help="启用 Code Execution")
    client_parser.add_argument("--image", action="store_true", help="生图模式")
    client_parser.add_argument("--save", help="图片保存路径")
    client_parser.add_argument("--attach", "-a", nargs="+", help="附加图片（文件路径）")
    client_parser.add_argument("--port", type=int, default=settings.browser_port, help="浏览器调试端口（仅 Camoufox 后端使用）")

    snapshot_parser = subparsers.add_parser("snapshot", help="抓取 snapshot")
    snapshot_parser.add_argument("prompt", nargs="?", default="你好，测试snapshot提取", help="触发用 prompt")

    login_parser = subparsers.add_parser("login", help="通过终端完成 Google 账号登录")
    login_parser.add_argument("--name", help="保存账号时使用的名称")
    login_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    login_parser.add_argument("--hl", default="en-US", help="Google 登录页语言，默认 en-US")
    login_parser.add_argument("--browser-port", type=int, default=settings.login_browser_port, help="登录浏览器调试端口")
    login_parser.add_argument("--camoufox-port", type=int, dest="browser_port", help=argparse.SUPPRESS)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "server":
        from aistudio_api.api.app import main as server_main
        import sys

        sys.argv = ["aistudio-api-server", "--port", str(args.port), "--browser-port", str(args.browser_port)]
        server_main()
        return

    if args.command == "client":
        from aistudio_api.config import DEFAULT_TEXT_MODEL
        from aistudio_api.infrastructure.gateway.cli import _run_cli

        if not args.model:
            args.model = DEFAULT_TEXT_MODEL
        asyncio.run(_run_cli(args))
        return

    if args.command == "snapshot":
        from aistudio_api.infrastructure.browser.snapshot_extractor import SnapshotExtractor

        async def _run_snapshot():
            extractor = SnapshotExtractor()
            snap = await extractor.extract(args.prompt)
            cookies = extractor.get_cookies()
            print(f"snapshot: {len(snap)} 字符")
            print(f"cookies: {len(cookies or {})} 个")

        asyncio.run(_run_snapshot())
        return

    if args.command == "login":
        from aistudio_api.application.account_service import AccountService
        from aistudio_api.infrastructure.account.account_store import AccountStore
        from aistudio_api.infrastructure.account.login_service import LoginService, LoginStatus

        async def _run_login():
            store = AccountStore()
            service = AccountService(
                account_store=store,
                login_service=LoginService(port=args.browser_port),
            )
            session_id = await service.start_login(
                args.name,
                headless=not args.headed,
                ui_locale=args.hl,
            )
            print(f"session_id: {session_id}")
            print(f"mode: {'headed' if args.headed else 'headless'}")

            last_status = None
            while True:
                session = service.get_login_status(session_id)
                if session is None:
                    raise RuntimeError("登录会话丢失")
                if session.status != last_status:
                    print(f"status: {session.status.value}")
                    last_status = session.status
                if session.status == LoginStatus.COMPLETED:
                    print(f"account_id: {session.account_id}")
                    if session.email:
                        print(f"email: {session.email}")
                    return
                if session.status == LoginStatus.FAILED:
                    raise RuntimeError(session.error or "登录失败")
                await asyncio.sleep(1)

        asyncio.run(_run_login())
        return
