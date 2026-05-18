"""解析浏览器 cookie 字符串，转换为 Playwright cookie / storage state 格式。"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

# Cookies that need httpOnly=False so JS can read them for SAPISIDHASH.
# Keep this list narrow: most auth cookies should remain httpOnly.
_AUTH_COOKIE_NAMES = {
    "SID", "APISID", "SAPISID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
}

_DOMAIN_OVERRIDES: dict[str, list[str]] = {
    "OSID": [".youtube.com"],
    "__Secure-OSID": [".youtube.com"],
    "__Secure-BUCKET": ["aistudio.google.com"],
    "OTZ": ["accounts.google.com"],
    "__Host-GAPS": ["accounts.google.com"],
    "__Host-1PLSID": ["accounts.google.com"],
    "__Host-3PLSID": ["accounts.google.com"],
    "LSID": ["accounts.google.com"],
    "SMSV": ["accounts.google.com"],
    "LSOLH": ["accounts.google.com"],
    "ACCOUNT_CHOOSER": ["accounts.google.com"],
    # SIDTS 系列只在 .youtube.com 域名下
    "__Secure-1PSIDTS": [".youtube.com"],
    "__Secure-3PSIDTS": [".youtube.com"],
}

# 需要复制到多个 Google 域名的核心认证 cookie
_MULTI_DOMAIN_COOKIES = {
    "SID", "__Secure-1PSID", "__Secure-3PSID",
    "HSID", "SSID",
    "APISID", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "NID",
}

# YouTube 域名列表（用于复制核心 cookie）
_GOOGLE_DOMAINS = [
    ".google.com",
    ".youtube.com",
    ".google.com.tw",
    ".google.com.hk",
    ".google.com.sg",
    ".aistudio.google.com",
    ".gemini.google.com",
    "accounts.google.com",
    "aistudio.google.com",
]


def _parse_cookie_pairs(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def build_google_cookie_list(
    pairs: Iterable[tuple[str, str]],
    *,
    allow_url_targets: bool,
) -> list[dict[str, Any]]:
    """Build Playwright-compatible cookies from raw name/value pairs.

    When ``allow_url_targets`` is enabled, host-only cookies are emitted with a
    ``url`` field instead of an imprecise ``domain`` so Playwright can inject
    them without tripping cookie prefix validation.
    """
    now = int(time.time())
    default_expires = now + 86400 * 180  # 180 天后过期

    seen: set[tuple[str, str, str]] = set()
    cookies: list[dict[str, Any]] = []

    def _add_cookie(name: str, value: str, target: str) -> None:
        is_host_target = not target.startswith(".")

        if name.startswith("__Host-") and not allow_url_targets:
            # storage_state 无法精确表达 host-only cookie，交给浏览器补全导出
            return

        target_kind = "url" if allow_url_targets and is_host_target else "domain"
        target_value = f"https://{target}/" if target_kind == "url" else target
        key = (name, target_kind, target_value)
        if key in seen:
            return
        seen.add(key)

        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "secure": True,
            "httpOnly": name not in _AUTH_COOKIE_NAMES,
            "sameSite": "None",
            "expires": default_expires,
        }
        if target_kind == "url":
            cookie["url"] = target_value
        else:
            cookie["domain"] = target
            cookie["path"] = "/"
        cookies.append(cookie)

    for name, value in pairs:
        targets = _DOMAIN_OVERRIDES.get(name)
        if not targets:
            targets = [".google.com"]
        for target in targets:
            _add_cookie(name, value, target)

    return cookies


def parse_cookie_string(raw: str) -> dict[str, Any]:
    """将 `key=value; key=value` 格式的 cookie 字符串解析为 Playwright storage state。

    Args:
        raw: 浏览器开发者工具或扩展导出的 cookie 字符串

    Returns:
        Playwright storage state dict，包含 cookies 和 origins 字段
    """
    return {
        "cookies": build_google_cookie_list(_parse_cookie_pairs(raw), allow_url_targets=False),
        "origins": [],
    }


def parse_and_filter_google_cookies(raw: str) -> list[dict[str, Any]]:
    state = parse_cookie_string(raw)
    return [c for c in state["cookies"] if "google" in c.get("domain", "")]
