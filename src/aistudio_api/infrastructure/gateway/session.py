"""Shared browser session management for gateway operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.browser.browser_engine import (
    build_browser_context_options,
    describe_browser_backend,
    is_camoufox_engine,
    sync_launch_browser,
    sync_launch_persistent_context,
    sync_maximize_page_window,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

log = logging.getLogger("aistudio.session")

AI_STUDIO_URL = settings.aistudio_start_url or "https://aistudio.google.com/prompts/new_chat?model=gemma-4-31b-it"
AI_STUDIO_URL_FALLBACK = "https://aistudio.google.com/app/prompts/new_chat"
GOOGLE_LOGIN_BOOTSTRAP_URL = (
    "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com"
)
INSTALL_HOOKS_JS = r"""
mw:((() => {
    // Verify hooks are actually present on XHR prototype, not just a stale flag
    const xhrHookAlive = XMLHttpRequest.prototype.open.__api_hooked === true;
    const fetchHookAlive = window.fetch.__api_hooked === true;
    if (window.__bg_hooked && xhrHookAlive && fetchHookAlive) return 'already_hooked';
    // Reset stale flag if hooks are missing
    if (window.__bg_hooked && (!xhrHookAlive || !fetchHookAlive)) window.__bg_hooked = false;

    if (!window.__api_diag_records) window.__api_diag_records = [];
    if (!window.__api_diag_inflight) window.__api_diag_inflight = {};
    if (!window.__api_diag_seq) window.__api_diag_seq = 0;
    function shortHash(value) {
        const text = String(value || '');
        let hash = 2166136261;
        for (let i = 0; i < text.length; i++) {
            hash ^= text.charCodeAt(i);
            hash = Math.imul(hash, 16777619);
        }
        return (hash >>> 0).toString(16).padStart(8, '0');
    }
    function summarizeSlot(value) {
        if (value === null) return {type: 'null'};
        if (value === undefined) return {type: 'undefined'};
        if (typeof value === 'string') return {type: 'string', len: value.length, hash: shortHash(value)};
        if (Array.isArray(value)) return {type: 'array', len: value.length};
        if (typeof value === 'object') return {type: 'object', keys: Object.keys(value).slice(0, 12)};
        return {type: typeof value, value: value};
    }
    function summarizeBody(body) {
        const summary = {body_len: body ? String(body).length : 0};
        if (!body) return summary;
        try {
            const payload = JSON.parse(body);
            if (!Array.isArray(payload)) {
                summary.payload_type = typeof payload;
                return summary;
            }
            summary.top_len = payload.length;
            summary.model = payload.length > 0 ? payload[0] : null;
            summary.safety = payload.length > 2 ? payload[2] : null;
            summary.generation_config = payload.length > 3 ? payload[3] : null;
            summary.snapshot_len = payload.length > 4 && typeof payload[4] === 'string' ? payload[4].length : null;
            summary.snapshot_hash = payload.length > 4 && typeof payload[4] === 'string' ? shortHash(payload[4]) : null;
            summary.has_tools = payload.length > 6 && payload[6] != null;
            summary.request_flag = payload.length > 10 ? payload[10] : null;
            summary.timezone = payload.length > 13 ? payload[13] : null;
            summary.slot_summary = payload.slice(0, 15).map(summarizeSlot);
            return summary;
        } catch (e) {
            summary.parse_error = String(e);
            return summary;
        }
    }
    function filterHeaders(headers) {
        const out = {};
        const blocked = new Set(['authorization', 'cookie', 'x-client-data']);
        const allowedExact = new Set(['content-type', 'origin', 'referer', 'x-same-domain']);
        try {
            Object.entries(headers || {}).forEach(([key, value]) => {
                const lower = String(key).toLowerCase();
                if (blocked.has(lower)) return;
                if (!allowedExact.has(lower) && !lower.startsWith('x-goog-') && !lower.startsWith('sec-fetch-')) return;
                out[lower] = String(value).slice(0, 240);
            });
        } catch (e) {}
        return out;
    }
    function headersToObject(headers) {
        const out = {};
        try {
            if (!headers) return out;
            if (headers instanceof Headers) {
                headers.forEach((value, key) => { out[String(key).toLowerCase()] = String(value); });
                return out;
            }
            if (Array.isArray(headers)) {
                headers.forEach((entry) => {
                    if (entry && entry.length >= 2) out[String(entry[0]).toLowerCase()] = String(entry[1]);
                });
                return out;
            }
            Object.entries(headers).forEach(([key, value]) => { out[String(key).toLowerCase()] = String(value); });
        } catch (e) {}
        return out;
    }
    function isReplayGenerateUrl(url) {
        const text = String(url || '');
        return text.includes('MakerSuiteService/GenerateContent') && !text.includes('Count');
    }
    function activationState() {
        try {
            if (!navigator.userActivation) return null;
            return {
                is_active: navigator.userActivation.isActive === true,
                has_been_active: navigator.userActivation.hasBeenActive === true
            };
        } catch (e) {
            return {error: String(e).slice(0, 120)};
        }
    }
    function readXhrResponse(xhr) {
        try {
            if (typeof xhr.responseText === 'string') return xhr.responseText;
        } catch (e) {
            return '<responseText failed: ' + String(e).slice(0, 120) + '>';
        }
        try {
            if (typeof xhr.response === 'string') return xhr.response;
            if (xhr.response !== undefined && xhr.response !== null) {
                return '<response ' + Object.prototype.toString.call(xhr.response) + '>';
            }
        } catch (e) {
            return '<response failed: ' + String(e).slice(0, 120) + '>';
        }
        return '';
    }
    function pushDiag(kind, transport, url, body, status, responseText, extra) {
        if (!isReplayGenerateUrl(url)) return;
        const summary = summarizeBody(body);
        summary.kind = kind;
        summary.transport = transport;
        summary.url = String(url).slice(0, 600);
        summary.endpoint = String(url).split('/').pop();
        if (status !== undefined && status !== null) summary.status = status;
        if (responseText !== undefined && responseText !== null) summary.response = String(responseText).slice(0, 300);
        if (extra) Object.assign(summary, extra);
        summary.ts = Date.now();
        window.__api_diag_records.push(summary);
        if (window.__api_diag_records.length > 100) window.__api_diag_records.shift();
    }

    const dms = window.default_MakerSuite;
    if (!dms) return 'no_default_MakerSuite';

    // Auto-detect snapshot function via feature matching
    let snapKey = null;
    for (const k of Object.keys(dms)) {
        try {
            if (typeof dms[k] !== 'function') continue;
            const src = dms[k].toString();
            if (src.includes('.snapshot({') && src.includes('content') && src.includes('yield')) {
                snapKey = k;
                break;
            }
        } catch(e) {}
    }
    if (!snapKey) return 'no_snapshot_fn';

    // Hook snapshot function to capture service (only if not already hooked)
    if (!dms[snapKey].__api_hooked) {
        const origSnap = dms[snapKey];
        dms[snapKey] = function(...args) {
            window.__bg_service = args[0];
            const result = origSnap.apply(this, args);
            if (result instanceof Promise) return result.then(s => { window.__bg_snapshot = s; return s; });
            window.__bg_snapshot = result;
            return result;
        };
        dms[snapKey].__api_hooked = true;
    }

    // XHR hook for body replacement (always re-install if missing)
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const origSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
    const hookedOpen = function(method, url, ...args) {
        this.__url = url;
        this.__api_diag_method = method || 'GET';
        this.__api_diag_headers = {};
        this.__is_gen = isReplayGenerateUrl(url);
        window.__last_hook_url = url;
        return origOpen.call(this, method, url, ...args);
    };
    hookedOpen.__api_hooked = true;
    XMLHttpRequest.prototype.open = hookedOpen;
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        try {
            if (!this.__api_diag_headers) this.__api_diag_headers = {};
            this.__api_diag_headers[String(name).toLowerCase()] = String(value);
        } catch (e) {}
        return origSetRequestHeader.call(this, name, value);
    };
    XMLHttpRequest.prototype.setRequestHeader.__api_hooked = true;
    function attachXhrDiag(xhr, sentBody) {
        if (xhr.__api_diag_attached) return;
        xhr.__api_diag_attached = true;
        const reqId = xhr.__api_diag_id;
        if (reqId && window.__api_diag_inflight[reqId]) {
            window.__api_diag_inflight[reqId].attached = true;
        }
        const done = (eventName) => {
            if (reqId && window.__api_diag_inflight[reqId]) {
                window.__api_diag_inflight[reqId].ready_state = xhr.readyState;
                window.__api_diag_inflight[reqId].status = xhr.status || 0;
                window.__api_diag_inflight[reqId].last_event = eventName;
            }
            if (eventName === 'readystatechange' && xhr.readyState !== 4) return;
            if (xhr.__api_diag_done) return;
            xhr.__api_diag_done = true;
            pushDiag('response', 'xhr', xhr.__url || '', sentBody, xhr.status || 0, readXhrResponse(xhr), {
                req_id: reqId || null,
                event: eventName,
                method: xhr.__api_diag_method || null,
                headers: filterHeaders(xhr.__api_diag_headers || {}),
                ready_state: xhr.readyState,
                response_type: xhr.responseType || '',
                response_url: String(xhr.responseURL || '').slice(0, 600),
                with_credentials: xhr.withCredentials === true,
                activation_done: activationState()
            });
            if (reqId && window.__api_diag_inflight[reqId]) {
                window.__api_diag_inflight[reqId].done = true;
                window.__api_diag_inflight[reqId].done_ts = Date.now();
            }
        };
        xhr.addEventListener('readystatechange', () => done('readystatechange'));
        ['loadend', 'error', 'abort', 'timeout'].forEach((name) => {
            xhr.addEventListener(name, () => done(name), {once: true});
        });
    }
    XMLHttpRequest.prototype.send = function(body) {
        let sentBody = body;
        const reqId = this.__api_diag_id || ('xhr-' + (++window.__api_diag_seq));
        this.__api_diag_id = reqId;
        const requestExtra = {
            req_id: reqId,
            method: this.__api_diag_method || null,
            headers: filterHeaders(this.__api_diag_headers || {}),
            response_type: this.responseType || '',
            with_credentials: this.withCredentials === true,
            activation: activationState()
        };
        if (this.__is_gen && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = this.__url || '';
            sentBody = captured;
            pushDiag('request', 'xhr', this.__url || '', sentBody, null, null, requestExtra);
            window.__api_diag_inflight[reqId] = {
                req_id: reqId,
                transport: 'xhr',
                url: String(this.__url || '').slice(0, 600),
                model: summarizeBody(sentBody).model || null,
                body_len: sentBody ? String(sentBody).length : 0,
                start_ts: Date.now(),
                ready_state: this.readyState,
                status: this.status || 0,
                activation: requestExtra.activation
            };
            attachXhrDiag(this, sentBody);
            return origSend.call(this, captured);
        }
        if (this.__is_gen) {
            pushDiag('request', 'xhr', this.__url || '', sentBody, null, null, requestExtra);
            window.__api_diag_inflight[reqId] = {
                req_id: reqId,
                transport: 'xhr',
                url: String(this.__url || '').slice(0, 600),
                model: summarizeBody(sentBody).model || null,
                body_len: sentBody ? String(sentBody).length : 0,
                start_ts: Date.now(),
                ready_state: this.readyState,
                status: this.status || 0,
                activation: requestExtra.activation
            };
            attachXhrDiag(this, sentBody);
        }
        return origSend.call(this, body);
    };

    // fetch hook for body replacement (streaming uses fetch)
    const origFetch = window.fetch;
    const hookedFetch = function(input, init) {
        let url = typeof input === 'string' ? input : (input instanceof Request ? input.url : String(input));
        let body = init && init.body ? init.body : (input instanceof Request ? input.body : null);
        const method = (init && init.method) || (input instanceof Request ? input.method : 'GET');
        const headers = filterHeaders(headersToObject((init && init.headers) || (input instanceof Request ? input.headers : null)));
        const reqId = 'fetch-' + (++window.__api_diag_seq);
        const requestExtra = {
            req_id: reqId,
            method: method || null,
            headers,
            credentials: (init && init.credentials) || (input instanceof Request ? input.credentials : null),
            mode: (init && init.mode) || (input instanceof Request ? input.mode : null),
            activation: activationState()
        };
        if (isReplayGenerateUrl(url) && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = url;
            if (init) {
                init.body = captured;
            } else {
                init = { body: captured };
            }
            body = captured;
        }
        if (isReplayGenerateUrl(url)) {
            pushDiag('request', 'fetch', url, body, null, null, requestExtra);
            window.__api_diag_inflight[reqId] = {
                req_id: reqId,
                transport: 'fetch',
                url: String(url || '').slice(0, 600),
                model: summarizeBody(body).model || null,
                body_len: body ? String(body).length : 0,
                start_ts: Date.now(),
                activation: requestExtra.activation
            };
        }
        const result = origFetch.call(this, input, init);
        if (isReplayGenerateUrl(url)) {
            return Promise.resolve(result).then((response) => {
                if (window.__api_diag_inflight[reqId]) {
                    window.__api_diag_inflight[reqId].status = response.status || 0;
                    window.__api_diag_inflight[reqId].done = true;
                    window.__api_diag_inflight[reqId].done_ts = Date.now();
                }
                try {
                    response.clone().text().then((text) => {
                        pushDiag('response', 'fetch', url, body, response.status || 0, text || '', Object.assign({}, requestExtra, {activation_done: activationState()}));
                    }).catch(() => {
                        pushDiag('response', 'fetch', url, body, response.status || 0, '<response text failed>', Object.assign({}, requestExtra, {activation_done: activationState()}));
                    });
                } catch (e) {
                    pushDiag('response', 'fetch', url, body, response.status || 0, '<response clone failed>', Object.assign({}, requestExtra, {activation_done: activationState()}));
                }
                return response;
            }, (error) => {
                if (window.__api_diag_inflight[reqId]) {
                    window.__api_diag_inflight[reqId].status = 0;
                    window.__api_diag_inflight[reqId].error = String(error).slice(0, 180);
                    window.__api_diag_inflight[reqId].done = true;
                    window.__api_diag_inflight[reqId].done_ts = Date.now();
                }
                pushDiag('response', 'fetch', url, body, 0, '<fetch rejected: ' + String(error).slice(0, 180) + '>', Object.assign({}, requestExtra, {activation_done: activationState()}));
                throw error;
            });
        }
        return result;
    };
    hookedFetch.__api_hooked = true;
    window.fetch = hookedFetch;

    window.__bg_hooked = true;
    window.__snap_key = snapKey;
    return 'hooked:' + snapKey;
})())
"""

DIALOG_CLEANUP_JS = """(() => {
    document.querySelectorAll('button').forEach((button) => {
        const text = (button.textContent || '').trim().toLowerCase();
        if (['dismiss', 'close', 'accept', 'ok', 'agree', 'got it'].includes(text)) {
            button.click();
        }
    });
    document.querySelectorAll('.cdk-overlay-backdrop').forEach((node) => node.remove());
    document.querySelectorAll('.cdk-overlay-container').forEach((node) => node.remove());
})()"""

BOTGUARD_BOOTSTRAP_PROMPT = "ping"
TEMPLATE_CAPTURE_PROMPT = "say 't'"


def _short_diag_text(value: Any, limit: int = 180) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _summarize_generate_body(body: str | None) -> dict[str, Any]:
    summary: dict[str, Any] = {"body_len": len(body or "")}
    if not body:
        return summary
    try:
        payload = json.loads(body)
    except Exception as exc:
        summary["parse_error"] = str(exc)
        return summary
    if not isinstance(payload, list):
        summary["payload_type"] = type(payload).__name__
        return summary

    prompt = None
    try:
        prompt = payload[1][0][0][1]
    except Exception:
        pass

    snapshot = payload[4] if len(payload) > 4 and isinstance(payload[4], str) else None

    def _summarize_slot(value: Any) -> dict[str, Any]:
        if value is None:
            return {"type": "null"}
        if isinstance(value, str):
            return {"type": "string", "len": len(value), "hash": hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]}
        if isinstance(value, list):
            return {"type": "array", "len": len(value)}
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value.keys())[:12]}
        return {"type": type(value).__name__, "value": value}

    summary.update(
        {
            "top_len": len(payload),
            "model": payload[0] if len(payload) > 0 else None,
            "prompt": _short_diag_text(prompt),
            "safety": payload[2] if len(payload) > 2 else None,
            "generation_config": payload[3] if len(payload) > 3 else None,
            "snapshot_len": len(snapshot) if snapshot is not None else None,
            "snapshot_hash": hashlib.sha256(snapshot.encode("utf-8", errors="ignore")).hexdigest()[:12] if snapshot is not None else None,
            "has_tools": len(payload) > 6 and payload[6] is not None,
            "request_flag": payload[10] if len(payload) > 10 else None,
            "timezone": payload[13] if len(payload) > 13 else None,
            "slot_summary": [_summarize_slot(value) for value in payload[:15]],
        }
    )
    return summary


def _summarize_request_meta(request) -> dict[str, Any]:
    allowed_exact = {
        "content-type",
        "origin",
        "referer",
        "x-same-domain",
    }
    allowed_prefixes = (
        "x-goog-",
        "sec-fetch-",
    )
    blocked = {
        "authorization",
        "cookie",
        "x-client-data",
    }
    headers: dict[str, str] = {}
    for key, value in dict(request.headers).items():
        lower = key.lower()
        if lower in blocked:
            continue
        if lower in allowed_exact or any(lower.startswith(prefix) for prefix in allowed_prefixes):
            headers[lower] = _short_diag_text(value, 240) or ""
    return {
        "method": request.method,
        "url": _short_diag_text(request.url, 600),
        "endpoint": request.url.rsplit("/", 1)[-1] if "/" in request.url else request.url,
        "headers": headers,
    }


def _summarize_raw_request_meta(method: str, url: str, headers: dict[str, str]) -> dict[str, Any]:
    class _RequestView:
        def __init__(self, method: str, url: str, headers: dict[str, str]):
            self.method = method
            self.url = url
            self.headers = headers

    return _summarize_request_meta(_RequestView(method, url, headers))


def _is_response_diag_url(url: str) -> bool:
    return _is_makersuite_generate_content_url(url)


def _is_makersuite_generate_content_url(url: str | None) -> bool:
    if not url:
        return False
    if "CountTokens" in url or "Count" in url:
        return False
    return "MakerSuiteService/GenerateContent" in url


class BrowserSession:
    def __init__(self, port: int):
        self.port = port
        self._auth_file = settings.auth_file or self._discover_active_auth_file()
        self._profile_dir = self._derive_profile_dir(self._auth_file)
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._playwright = None
        self._snap_key: str | None = None
        self._templates: dict[str, dict[str, Any]] = {}
        self._bootstrap_template: dict[str, Any] | None = None
        self._response_diag_pages: set[int] = set()
        self._response_diag_contexts: set[int] = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aistudio-browser")
        self._botguard_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()

    async def ensure_context(self):
        return await self._run_sync(self._ensure_browser_sync)

    async def switch_auth(self, auth_file: str | None) -> None:
        await self._run_sync(self._switch_auth_sync, auth_file)

    async def ensure_hook_page(self):
        await self._run_sync(self._ensure_hook_page_sync)
        return True

    async def ensure_botguard_service(self):
        await self._run_sync(self._ensure_botguard_service_sync)
        return True

    async def get_page_diag_records(self) -> list[dict[str, Any]]:
        return await self._run_sync(self._get_page_diag_records_sync)

    async def get_page_diag_state(self) -> dict[str, Any]:
        return await self._run_sync(self._get_page_diag_state_sync)

    async def prime_input_surface_debug(self) -> dict[str, Any]:
        return await self._run_sync(self._prime_input_surface_debug_sync)

    async def import_cookies(self, cookie_string: str, auth_file: str | None = None) -> int:
        """注入 cookie 字符串到浏览器，访问页面获取完整 cookie，再导出保存。"""
        def _sync():
            from aistudio_api.infrastructure.account.cookie_refresher import load_cookies_from_string

            pw_cookies = load_cookies_from_string(cookie_string)
            target_auth_file = auth_file or self._auth_file
            original_auth_file = self._auth_file
            original_profile_dir = self._profile_dir
            had_live_context = (
                self._ctx is not None
                and self._hook_page is not None
                and not self._hook_page.is_closed()
            )

            if not target_auth_file:
                return len(pw_cookies)

            # Seed target auth first so a fresh persistent profile can bootstrap
            # from the refreshed cookie jar on first launch.
            self._save_cookies_sync(auth_file=target_auth_file, cookies=pw_cookies)

            switched_target = False
            target_ctx = self._ctx
            if (
                self._ctx is None
                or not original_auth_file
                or Path(target_auth_file).resolve() != Path(original_auth_file).resolve()
            ):
                self._switch_auth_sync(target_auth_file)
                switched_target = True
                target_ctx = self._ensure_browser_sync()
            elif self._ctx is None:
                target_ctx = self._ensure_browser_sync()

            if not target_ctx:
                return len(pw_cookies)

            target_ctx.add_cookies(pw_cookies)

            # 先走 Google 登录页，让浏览器补全 host-only / session cookies
            try:
                page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                self._bootstrap_google_session_sync(page)
            except Exception as e:
                log.warning("[import_cookies] browser visit failed: %s", e)

            # 从浏览器导出全部 cookies
            try:
                browser_cookies = self._ctx.cookies()
                if browser_cookies:
                    self._save_cookies_sync(auth_file=target_auth_file, cookies=browser_cookies)
                    log.info("[import_cookies] exported %d cookies from browser", len(browser_cookies))
                else:
                    # fallback: 用 curl_cffi 的结果
                    self._save_cookies_sync(auth_file=target_auth_file, cookies=pw_cookies)
                return len(browser_cookies or pw_cookies)
            finally:
                if switched_target and original_auth_file:
                    self._switch_auth_sync(original_auth_file)
                    self._profile_dir = original_profile_dir
                    if had_live_context:
                        try:
                            self._ensure_browser_sync()
                        except Exception as restore_exc:
                            log.warning("[import_cookies] failed to restore original browser context: %s", restore_exc)

        return await self._run_sync(_sync)

    async def capture_template(self, model: str) -> dict[str, Any]:
        return await self._run_sync(self._capture_template_sync, model)

    async def upload_images(self, image_paths: list[str]) -> list[str]:
        return await self._run_sync(self._upload_images_sync, image_paths)

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        loop = asyncio.get_running_loop()
        async with self._snapshot_lock:
            return await loop.run_in_executor(self._executor, lambda: self._generate_snapshot_sync(contents))

    async def send_hooked_request(self, *, body: str, timeout_ms: int) -> tuple[int, bytes]:
        return await self._run_sync(self._send_hooked_request_sync, body, timeout_ms)

    async def send_streaming_request(self, *, body: str, timeout_ms: int):
        """Send a streaming request, yielding ("status", int) and ("chunk", bytes) events."""
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def _stream_worker():
            try:
                log.debug("[stream] worker started")
                self._send_streaming_request_sync(body, timeout_ms, queue, loop, cancel_event)
                log.debug("[stream] worker finished")
            except Exception as e:
                log.debug(f"[stream] worker exception: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", e))
                loop.call_soon_threadsafe(queue.put_nowait, None)

        executor_task = loop.run_in_executor(self._executor, _stream_worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                tag, data = item
                if tag == "error":
                    raise data
                yield tag, data
        finally:
            cancel_event.set()
            await executor_task

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        async with self._botguard_lock:
            return await loop.run_in_executor(self._executor, lambda: func(*args))

    @staticmethod
    def _discover_active_auth_file() -> str | None:
        try:
            store = AccountStore()
            account = store.get_active_account()
            if account is None:
                return None
            path = store.get_auth_path_optional(account.id, require_exists=False)
            return str(path) if path is not None else None
        except Exception:
            return None

    @staticmethod
    def _derive_profile_dir(auth_file: str | None) -> str | None:
        if not auth_file:
            fallback_auth_file = BrowserSession._discover_active_auth_file()
            if not fallback_auth_file:
                return None
            auth_file = fallback_auth_file
        return str(Path(auth_file).resolve().parent / "profile")

    def _bootstrap_google_session_sync(self, page) -> None:
        """Visit Google surfaces so Chromium can materialize a stable profile."""
        page.goto(GOOGLE_LOGIN_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                if "accounts.google.com" not in (page.url or ""):
                    return
            except Exception:
                continue
        raise RuntimeError(f"bootstrap stayed on login flow: url={page.url}")

    def _get_captured_info(self) -> tuple[str, dict[str, str]]:
        """Get captured URL and headers from template."""
        for tpl in self._templates.values():
            if tpl.get("url"):
                url = tpl["url"]
                if not _is_makersuite_generate_content_url(url):
                    log.warning("忽略非标准 GenerateContent 模板 URL: %s", url)
                    continue
                headers = {k: v for k, v in tpl.get("headers", {}).items() if k.lower() not in ("host", "content-length")}
                return url, headers
        raise RuntimeError("no MakerSuite GenerateContent URL available for replay")

    def _send_streaming_request_sync(
        self,
        body: str,
        timeout_ms: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        cancel_event: threading.Event,
    ):
        """Sync method: sends XHR request and consumes page-side stream events."""
        import time as _t
        _t0 = _t.time()

        page, captured_url, captured_headers = self._prepare_streaming_sync()
        log.debug(f"[stream] prep done in {_t.time()-_t0:.1f}s, url={captured_url}")

        timeout_s = timeout_ms / 1000
        rid = uuid.uuid4().hex[:8]

        # Start XHR in page context. Each request gets an isolated state object
        # keyed by rid, allowing multiple concurrent XHRs on the same page.
        page.evaluate("""(args) => {
            const rid = args.rid;
            if (!window.__streams) window.__streams = {};

            const existing = window.__streams[rid];
            if (existing && existing.xhr && existing.xhr.readyState !== 4) {
                try { existing.xhr.abort(); } catch (e) {}
            }

            const state = {
                xhr: null,
                events: [],
                waiter: null,
                recvPos: 0,
                statusSent: false,
            };
            window.__streams[rid] = state;

            function push(event) {
                if (state.waiter) {
                    const waiter = state.waiter;
                    state.waiter = null;
                    waiter(event);
                    return;
                }
                state.events.push(event);
            }

            function pushStatus(xhr) {
                if (state.statusSent || xhr.readyState < 2) return;
                state.statusSent = true;
                push({type: 'status', status: xhr.status || 0});
            }

            function pushChunk(xhr) {
                if (xhr.readyState < 3) return;
                const chunk = xhr.responseText.substring(state.recvPos);
                if (!chunk) return;
                state.recvPos = xhr.responseText.length;
                push({type: 'chunk', text: chunk});
            }

            if (!window.__stream_next) window.__stream_next = {};
            window.__stream_next[rid] = function(timeoutMs) {
                if (state.events.length) return Promise.resolve(state.events.shift());
                return new Promise((resolve) => {
                    let done = false;
                    const timer = setTimeout(() => {
                        if (done) return;
                        done = true;
                        if (state.waiter === finish) state.waiter = null;
                        resolve({type: 'idle'});
                    }, timeoutMs);
                    const finish = (event) => {
                        if (done) return;
                        done = true;
                        clearTimeout(timer);
                        resolve(event);
                    };
                    state.waiter = finish;
                });
            };

            if (!window.__stream_abort) window.__stream_abort = {};
            window.__stream_abort[rid] = function() {
                if (state.xhr && state.xhr.readyState !== 4) {
                    try { state.xhr.abort(); } catch (e) {}
                }
            };

            var xhr = new XMLHttpRequest();
            xhr.open('POST', args.url);
            var h = args.headers;
            for (var k in h) {
                xhr.setRequestHeader(k, h[k]);
            }
            xhr.withCredentials = true;
            xhr.timeout = args.timeout * 1000;

            xhr.onreadystatechange = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onprogress = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onload = function() {
                pushStatus(xhr);
                pushChunk(xhr);
                push({type: 'done'});
            };
            xhr.onerror = function() {
                push({type: 'error', message: 'network error'});
            };
            xhr.ontimeout = function() {
                push({type: 'error', message: 'timeout'});
            };
            xhr.onabort = function() {
                push({type: 'aborted'});
            };

            state.xhr = xhr;
            xhr.send(args.body);
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
            "rid": rid,
        })

        deadline = _t.time() + timeout_s
        status_sent = False
        while _t.time() < deadline:
            if cancel_event.is_set():
                log.debug("[stream] cancellation requested for %s", rid)
                page.evaluate("rid => { if (window.__stream_abort && window.__stream_abort[rid]) window.__stream_abort[rid](); }", rid)
                break

            event = page.evaluate("rid => window.__stream_next[rid](250)", rid)
            event_type = event.get("type")

            if event_type == "idle":
                continue
            if event_type == "status":
                status = event.get("status", 0)
                log.debug(f"[stream] got status={status} after {_t.time()-_t0:.1f}s")
                loop.call_soon_threadsafe(queue.put_nowait, ("status", status))
                status_sent = True
                continue
            if event_type == "chunk":
                text = event.get("text") or ""
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", text.encode("utf-8")))
                continue
            if event_type == "error":
                message = event.get("message", "unknown error")
                log.debug(f"[stream] error after {_t.time()-_t0:.1f}s: {message}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError(f"streaming request failed: {message}")))
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            if event_type in ("done", "aborted"):
                break

        if not status_sent:
            log.debug(f"[stream] timeout after {_t.time()-_t0:.1f}s before response status")
            loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError("streaming request timeout: no response status")))
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return

        # Signal completion
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def _prepare_streaming_sync(self):
        """Prepare page for streaming request. Returns (page, url, headers)."""
        page = self._ensure_botguard_service_sync()
        if not self._templates:
            # Template not yet captured — capture one so we have a URL
            from aistudio_api.config import DEFAULT_TEXT_MODEL
            try:
                self._capture_template_sync(DEFAULT_TEXT_MODEL)
            except Exception as e:
                log.warning("auto template capture failed: %s", e)
        url, headers = self._get_captured_info()
        return page, url, headers

    def _switch_auth_sync(self, auth_file: str | None) -> None:
        self._auth_file = auth_file
        self._profile_dir = self._derive_profile_dir(auth_file)
        self._templates.clear()
        self._bootstrap_template = None
        self._close_sync()

    def _ensure_browser_sync(self):
        if self._ctx is not None and self._hook_page is not None and not self._hook_page.is_closed():
            return self._ctx

        import time as _t
        _t0 = _t.time()

        self._close_sync()

        # Chromium backend: auth.json
        if not is_camoufox_engine():
            return self._ensure_browser_chromium_sync(_t0)

        # Legacy mode: Camoufox + auth.json
        from camoufox.sync_api import Camoufox
        from aistudio_api.config import build_camoufox_proxy

        self._cf = Camoufox(
            headless=settings.browser_headless,
            main_world_eval=True,
            proxy=build_camoufox_proxy(settings.proxy_url),
        )
        self._browser = self._cf.__enter__()
        self._ctx = self._browser.new_context(**build_browser_context_options())
        self._apply_auth_file_sync()
        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        sync_maximize_page_window(self._hook_page)
        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        self._goto_aistudio_sync(self._hook_page)
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        self._install_hooks_sync(self._hook_page)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        return self._ctx

    def _ensure_browser_chromium_sync(self, _t0: float):
        """Chromium backend: prefer per-account persistent profile, fallback to auth.json."""
        import time as _t

        profile_dir = self._profile_dir
        should_seed_from_auth = True
        if profile_dir:
            profile_path = Path(profile_dir)
            # If a profile already existed before launch, trust it as the source of
            # truth and do not re-inject auth.json on failures. Mixing the two was
            # causing Google to flag the profile's cookie state as inconsistent.
            should_seed_from_auth = not (profile_path.exists() and any(profile_path.iterdir()))
            profile_path.mkdir(parents=True, exist_ok=True)
            self._ctx = sync_launch_persistent_context(
                profile_dir,
                **build_browser_context_options(),
            )
            self._browser = None
            self._cf = None
            self._playwright = None
        else:
            self._browser, self._cf, self._playwright = sync_launch_browser()
            self._ctx = self._browser.new_context(**build_browser_context_options())

        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        sync_maximize_page_window(self._hook_page)

        # First, see whether the persistent profile / current context is already alive.
        try:
            self._hook_page.goto("https://aistudio.google.com/", wait_until="domcontentloaded", timeout=15000)
            if "accounts.google.com" not in (self._hook_page.url or ""):
                profile_label = "profile" if profile_dir else "auth.json cache"
                log.info("[chromium-auth] %s hit", profile_label)
                self._goto_aistudio_sync(self._hook_page)
                self._install_hooks_sync(self._hook_page)
                log.debug(f"[timing] page loaded (cached) in {_t.time()-_t0:.1f}s")
                return self._ctx
        except Exception as e:
            log.debug("[chromium-auth] initial profile check failed: %s", e)

        # Only seed a fresh profile from auth.json once. After a profile exists,
        # auth.json must not be re-injected or it can corrupt the browser state.
        if should_seed_from_auth and self._auth_file and Path(self._auth_file).exists():
            try:
                data = json.loads(Path(self._auth_file).read_text())
                cached = data.get("cookies") or []
                if cached:
                    self._ctx.add_cookies(cached)
                    self._bootstrap_google_session_sync(self._hook_page)
                    if "accounts.google.com" not in (self._hook_page.url or ""):
                        log.info("[chromium-auth] auth.json seeded context (%d cookies)", len(cached))
                        self._save_cookies_sync()
                        self._goto_aistudio_sync(self._hook_page)
                        self._install_hooks_sync(self._hook_page)
                        log.debug(f"[timing] page loaded (cached) in {_t.time()-_t0:.1f}s")
                        return self._ctx
                    log.info("[chromium-auth] auth.json appears expired")
            except Exception as e:
                log.debug("[chromium-auth] auth.json load failed: %s", e)
        elif profile_dir:
            log.info("[chromium-auth] existing profile present; skipped auth.json seeding to avoid cookie pollution")

        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        self._goto_aistudio_sync(self._hook_page)
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        self._install_hooks_sync(self._hook_page)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        return self._ctx

    def _apply_auth_file_sync(self):
        """Legacy mode: load cookies from auth.json."""
        if self._auth_file and Path(self._auth_file).exists():
            log.info(f"Loading auth from: {self._auth_file}")
            data = json.loads(Path(self._auth_file).read_text())
            cookies = data.get("cookies") or []
            if cookies:
                self._ctx.add_cookies(cookies)
                log.info(f"Added {len(cookies)} cookies to context")
        else:
            log.warning(f"No auth_file! self._auth_file={self._auth_file}")


    def _save_cookies_sync(
        self,
        *,
        auth_file: str | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        """将 cookie 保存回 auth.json。"""
        target_auth_file = auth_file or self._auth_file
        if not target_auth_file:
            return
        try:
            current_cookies = cookies
            if current_cookies is None:
                if self._ctx is None:
                    return
                current_cookies = self._ctx.cookies()
            if not current_cookies:
                return
            auth_path = Path(target_auth_file)
            # 读取现有的 origins 数据（如果有）
            origins = []
            if auth_path.exists():
                try:
                    existing = json.loads(auth_path.read_text())
                    origins = existing.get("origins", [])
                except Exception:
                    pass
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(json.dumps({"cookies": current_cookies, "origins": origins}, indent=2))
            log.info(f"Saved {len(current_cookies)} cookies to {target_auth_file}")
        except Exception as e:
            log.debug(f"Failed to save cookies: {e}")

    def _ensure_hook_page_sync(self):
        self._ensure_browser_sync()
        if "aistudio.google.com" not in (self._hook_page.url or ""):
            self._goto_aistudio_sync(self._hook_page)
        self._install_hooks_sync(self._hook_page)
        return self._hook_page

    def _get_page_diag_records_sync(self) -> list[dict[str, Any]]:
        page = self._ensure_hook_page_sync()
        records = page.evaluate("mw:window.__api_diag_records || []")
        return records if isinstance(records, list) else []

    def _get_page_diag_state_sync(self) -> dict[str, Any]:
        page = self._ensure_hook_page_sync()
        state = page.evaluate(
            """
mw:(() => {
    const textarea = document.querySelector('textarea');
    const active = document.activeElement;
    const activation = (() => {
        try {
            if (!navigator.userActivation) return null;
            return {
                is_active: navigator.userActivation.isActive === true,
                has_been_active: navigator.userActivation.hasBeenActive === true
            };
        } catch (e) {
            return {error: String(e).slice(0, 120)};
        }
    })();
    return {
        url: location.href,
        title: document.title,
        ready_state: document.readyState,
        visibility: document.visibilityState,
        has_focus: document.hasFocus(),
        page_age_ms: Math.round(performance.now()),
        user_activation: activation,
        bg_service: !!window.__bg_service,
        snap_key: window.__snap_key || null,
        bg_snapshot_len: window.__bg_snapshot ? String(window.__bg_snapshot).length : 0,
        sr_len: window.__sr ? String(window.__sr).length : 0,
        pending_body_len: window.__pending_body ? String(window.__pending_body).length : 0,
        last_hook_url: window.__last_hook_url || '',
        active_tag: active ? active.tagName : null,
        active_is_textarea: !!textarea && active === textarea,
        textarea_value_len: textarea && typeof textarea.value === 'string' ? textarea.value.length : null,
        input_prime_done: window.__api_input_prime_done === true,
        input_prime_mode: window.__api_input_prime_mode || null,
        input_prime_diag: window.__api_input_prime_diag || null,
        diag_records_len: (window.__api_diag_records || []).length,
        diag_inflight: Object.values(window.__api_diag_inflight || {}).slice(-20),
        now: Date.now()
    };
})()
"""
        )
        return state if isinstance(state, dict) else {}

    def _prime_input_surface_debug_sync(self) -> dict[str, Any]:
        page = self._ensure_hook_page_sync()
        try:
            page.evaluate(
                """
mw:(() => {
    window.__api_input_prime_done = false;
    window.__api_input_prime_mode = null;
    window.__api_input_prime_diag = null;
})()
"""
            )
        except Exception:
            pass
        before = self._get_page_diag_state_sync()
        self._prime_input_surface_sync(page)
        after = self._get_page_diag_state_sync()
        return {
            "configured_seconds": settings.aistudio_input_prime_seconds,
            "configured_mode": settings.aistudio_input_prime_mode,
            "before": before,
            "after": after,
        }

    def _ensure_botguard_service_sync(self):
        import time as _t
        _t0 = _t.time()
        page = self._ensure_hook_page_sync()
        if page.evaluate("mw:!!window.__bg_service"):
            log.debug(f"[timing] botguard cached, took {_t.time()-_t0:.1f}s")
            return page
        if settings.aistudio_bootstrap_delay_seconds > 0:
            delay_ms = settings.aistudio_bootstrap_delay_seconds * 1000
            log.info(
                "[response-diag] delaying first BotGuard capture for %ss to test page readiness",
                settings.aistudio_bootstrap_delay_seconds,
            )
            page.wait_for_timeout(delay_ms)
        self._prime_input_surface_sync(page)

        captured: dict[str, Any] = {}

        def on_response(response):
            if not _is_makersuite_generate_content_url(response.url):
                return
            if not settings.response_diag:
                return
            try:
                text = response.text()
            except Exception as exc:
                text = f"<response.text() failed: {exc}>"
            log.info(
                "[response-diag] botguard capture response: status=%s body=%s",
                response.status,
                _short_diag_text(text, 300),
            )

        def on_request(request):
            if not _is_makersuite_generate_content_url(request.url) or captured:
                return
            body = request.post_data
            if not body:
                return
            captured["url"] = request.url
            captured["headers"] = dict(request.headers)
            captured["body"] = body
            if settings.response_diag:
                summary = _summarize_generate_body(body)
                summary["request"] = _summarize_request_meta(request)
                log.info(
                    "[response-diag] botguard capture request: %s",
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                )

        page.evaluate(DIALOG_CLEANUP_JS)
        textarea = page.query_selector("textarea")
        if textarea is None:
            # Debug: show page state
            try:
                dbg_url = page.url
                dbg_title = page.title()
                dbg_body = page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")
            except Exception:
                dbg_url = dbg_title = dbg_body = '<error>'
            raise RuntimeError(f"textarea not found while capturing BotGuardService; url={dbg_url}, title={dbg_title}, body={dbg_body[:200]}")
        original_text = self._read_textarea_value_sync(textarea)
        page.on("request", on_request)
        page.on("response", on_response)
        try:
            self._type_textarea_value_sync(page, textarea, BOTGUARD_BOOTSTRAP_PROMPT)
            page.wait_for_timeout(800)
            page.evaluate(DIALOG_CLEANUP_JS)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send while capturing BotGuardService")

            for i in range(45):
                page.wait_for_timeout(1000)
                if page.evaluate("mw:!!window.__bg_service"):
                    self._wait_until_idle_sync(page)
                    if captured and self._bootstrap_template is None:
                        self._bootstrap_template = dict(captured)
                    self._restore_textarea_value_sync(textarea, original_text)
                    log.debug(f"[timing] botguard captured after {i+1}s, total {_t.time()-_t0:.1f}s")
                    return page

            raise RuntimeError("BotGuardService capture timeout")
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
            self._restore_textarea_value_sync(textarea, original_text)

    def _prime_input_surface_sync(self, page) -> None:
        if settings.aistudio_input_prime_seconds <= 0:
            return
        try:
            already_done = page.evaluate("mw:window.__api_input_prime_done === true")
        except Exception:
            already_done = False
        if already_done:
            return

        textarea = page.query_selector("textarea")
        if textarea is None:
            if settings.response_diag:
                log.info("[response-diag] input prime skipped: textarea not found")
            return

        original_text = self._read_textarea_value_sync(textarea)
        mode = settings.aistudio_input_prime_mode
        if mode == "native":
            if self._prime_input_surface_native_windows_sync(page, textarea, original_text):
                return
            if settings.response_diag:
                log.info("[response-diag] native input prime did not run; no playwright fallback in native mode")
            return

        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            textarea.click()
            page.keyboard.press("End")
            page.keyboard.type("x", delay=25)
            page.wait_for_timeout(150)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(settings.aistudio_input_prime_seconds * 1000)
            page.evaluate("mw:window.__api_input_prime_done = true")
            if settings.response_diag:
                state = self._get_page_diag_state_sync()
                log.info(
                    "[response-diag] input prime done: wait=%ss state=%s",
                    settings.aistudio_input_prime_seconds,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                )
        except Exception as exc:
            if settings.response_diag:
                log.info("[response-diag] input prime failed: %s", exc)
        finally:
            self._restore_textarea_value_sync(textarea, original_text)

    def _prime_input_surface_native_windows_sync(self, page, textarea, original_text: str) -> bool:
        import ctypes
        import sys

        if not sys.platform.startswith("win"):
            if settings.response_diag:
                log.info("[response-diag] native input prime skipped: not Windows")
            return False

        try:
            page.bring_to_front()
        except Exception:
            pass

        try:
            metrics = page.evaluate(
                """
mw:(() => {
    const el = document.querySelector('textarea');
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return {
        screen_x: window.screenX,
        screen_y: window.screenY,
        outer_width: window.outerWidth,
        outer_height: window.outerHeight,
        inner_width: window.innerWidth,
        inner_height: window.innerHeight,
        device_pixel_ratio: window.devicePixelRatio || 1,
        rect: {
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height
        }
    };
})()
"""
            )
        except Exception as exc:
            if settings.response_diag:
                log.info("[response-diag] native input prime metrics failed: %s", exc)
            return False

        if not isinstance(metrics, dict) or not isinstance(metrics.get("rect"), dict):
            if settings.response_diag:
                log.info("[response-diag] native input prime skipped: textarea metrics unavailable")
            return False

        rect = metrics["rect"]
        border_x = max(0, (float(metrics.get("outer_width") or 0) - float(metrics.get("inner_width") or 0)) / 2)
        chrome_y = max(0, float(metrics.get("outer_height") or 0) - float(metrics.get("inner_height") or 0) - border_x)
        target_x_offset = min(80, max(10, float(rect.get("width") or 0) / 2))
        target_y_offset = max(5, float(rect.get("height") or 0) / 2)
        base_x = float(metrics.get("screen_x") or 0) + float(rect.get("left") or 0) + target_x_offset
        base_y = float(metrics.get("screen_y") or 0) + float(rect.get("top") or 0) + target_y_offset
        candidate_points = [
            {
                "name": "screen_plus_rect",
                "x": int(base_x),
                "y": int(base_y),
            },
            {
                "name": "screen_plus_rect_with_chrome",
                "x": int(base_x + border_x),
                "y": int(base_y + chrome_y),
            },
        ]

        user32 = ctypes.windll.user32

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        original_point = POINT()
        user32.GetCursorPos(ctypes.byref(original_point))
        before_text = self._read_textarea_value_sync(textarea)
        before_len = len(before_text)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        GA_ROOT = 2
        SW_RESTORE = 9
        VK_BACK = 0x08

        user32.WindowFromPoint.argtypes = [POINT]
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool

        ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [
                ("ki", KEYBDINPUT),
                ("mi", MOUSEINPUT),
                ("hi", HARDWAREINPUT),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_ulong),
                ("union", INPUT_UNION),
            ]

            @property
            def ki(self):
                return self.union.ki

            @ki.setter
            def ki(self, value):
                self.union.ki = value

        def send_unicode_char(char: str) -> int:
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].ki = KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE, 0, 0)
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].ki = KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            return int(user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT)))

        def send_vk(vk: int) -> int:
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].ki = KEYBDINPUT(vk, 0, 0, 0, 0)
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
            return int(user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT)))

        def send_scan(scan_code: int) -> int:
            KEYEVENTF_SCANCODE = 0x0008
            inputs = (INPUT * 2)()
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].ki = KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE, 0, 0)
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].ki = KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)
            return int(user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT)))

        def describe_hwnd(hwnd: int | None) -> dict[str, Any]:
            if not hwnd:
                return {"hwnd": 0}
            handle = ctypes.c_void_p(int(hwnd))
            title_len = max(0, int(user32.GetWindowTextLengthW(handle)))
            title_buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(handle, title_buf, title_len + 1)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, class_buf, 256)
            pid = ctypes.c_ulong()
            thread_id = int(user32.GetWindowThreadProcessId(handle, ctypes.byref(pid)))
            return {
                "hwnd": int(hwnd),
                "title": title_buf.value[:160],
                "class": class_buf.value[:120],
                "pid": int(pid.value),
                "thread_id": thread_id,
            }

        def foreground_info() -> dict[str, Any]:
            return describe_hwnd(user32.GetForegroundWindow())

        def point_window_info(point: dict[str, Any]) -> dict[str, Any]:
            child = user32.WindowFromPoint(POINT(int(point["x"]), int(point["y"])))
            root = user32.GetAncestor(ctypes.c_void_p(int(child or 0)), GA_ROOT) if child else None
            return {
                "point": point,
                "child": describe_hwnd(int(child or 0)),
                "root": describe_hwnd(int(root or 0)),
            }

        def activate_point_window(point: dict[str, Any]) -> dict[str, Any]:
            info = point_window_info(point)
            root_hwnd = int(info.get("root", {}).get("hwnd") or 0)
            if root_hwnd:
                user32.ShowWindow(ctypes.c_void_p(root_hwnd), SW_RESTORE)
                set_ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(root_hwnd)))
                page.wait_for_timeout(200)
            else:
                set_ok = False
            info["set_foreground_ok"] = set_ok
            info["foreground_after"] = foreground_info()
            return info

        def read_focus_state() -> dict[str, Any]:
            state = page.evaluate(
                """
mw:(() => {
    const textarea = document.querySelector('textarea');
    const active = document.activeElement;
    return {
        active_tag: active ? active.tagName : null,
        active_is_textarea: !!textarea && active === textarea,
        textarea_value_len: textarea && typeof textarea.value === 'string' ? textarea.value.length : null
    };
})()
"""
            )
            return state if isinstance(state, dict) else {}

        def try_key_method(name: str, send_func) -> dict[str, Any]:
            sent = int(send_func())
            page.wait_for_timeout(200)
            text = self._read_textarea_value_sync(textarea)
            return {
                "name": name,
                "sent": sent,
                "len": len(text),
                "delta": len(text) - before_len,
                "endswith_x": text.endswith("x"),
                "focus": read_focus_state(),
            }

        try:
            if settings.response_diag:
                log.info(
                    "[response-diag] native input prime start: candidates=%s before_len=%s metrics=%s",
                    json.dumps(candidate_points, ensure_ascii=False, separators=(",", ":")),
                    before_len,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                )
            page.evaluate(
                "mw:(diag) => { window.__api_input_prime_diag = diag; window.__api_input_prime_mode = 'native'; }",
                {
                    "mode": "native",
                    "phase": "start",
                    "candidates": candidate_points,
                    "before_len": before_len,
                    "metrics": metrics,
                    "foreground_start": foreground_info(),
                },
            )
            click_checks: list[dict[str, Any]] = []
            selected_point: dict[str, Any] | None = None
            for point in candidate_points:
                window_info = activate_point_window(point)
                user32.SetCursorPos(int(point["x"]), int(point["y"]))
                page.wait_for_timeout(120)
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                page.wait_for_timeout(80)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                page.wait_for_timeout(250)
                check = page.evaluate(
                    """
mw:(() => {
    const textarea = document.querySelector('textarea');
    const active = document.activeElement;
    return {
        active_tag: active ? active.tagName : null,
        active_is_textarea: !!textarea && active === textarea,
        textarea_value_len: textarea && typeof textarea.value === 'string' ? textarea.value.length : null
    };
})()
"""
                )
                click_checks.append({"point": point, "check": check, "window": window_info})
                if isinstance(check, dict) and check.get("active_is_textarea") is True:
                    selected_point = point
                    break

            if selected_point is None:
                failed_diag = {
                    "mode": "native",
                    "phase": "click_failed",
                    "candidates": candidate_points,
                    "click_checks": click_checks,
                    "before_len": before_len,
                    "metrics": metrics,
                }
                page.evaluate("mw:(diag) => { window.__api_input_prime_done = true; window.__api_input_prime_diag = diag; }", failed_diag)
                if settings.response_diag:
                    log.info(
                        "[response-diag] native input prime click failed: %s",
                        json.dumps(failed_diag, ensure_ascii=False, separators=(",", ":")),
                    )
                page.wait_for_timeout(settings.aistudio_input_prime_seconds * 1000)
                return True

            key_attempts = []
            key_attempts.append(try_key_method("sendinput_unicode", lambda: send_unicode_char("x")))
            if key_attempts[-1]["delta"] == 0:
                key_attempts.append(try_key_method("sendinput_vk_x", lambda: send_vk(0x58)))
            if key_attempts[-1]["delta"] == 0:
                key_attempts.append(try_key_method("sendinput_scancode_x", lambda: send_scan(0x2D)))

            after_type_text = self._read_textarea_value_sync(textarea)
            active_after_type = read_focus_state()
            successful_key_attempt = next((attempt for attempt in key_attempts if attempt["delta"] > 0), None)
            type_diag = {
                "mode": "native",
                "phase": "after_type",
                "selected_point": selected_point,
                "click_checks": click_checks,
                "before_len": before_len,
                "after_type_len": len(after_type_text),
                "after_type_delta": len(after_type_text) - before_len,
                "after_type_endswith_x": after_type_text.endswith("x"),
                "key_attempts": key_attempts,
                "successful_key_attempt": successful_key_attempt["name"] if successful_key_attempt else None,
                "active_after_type": active_after_type,
                "metrics": metrics,
            }
            page.evaluate("mw:(diag) => { window.__api_input_prime_diag = diag; }", type_diag)
            if settings.response_diag:
                log.info(
                    "[response-diag] native input prime after type: %s",
                    json.dumps(type_diag, ensure_ascii=False, separators=(",", ":")),
                )
            sent_backspace = send_vk(VK_BACK) if len(after_type_text) > before_len else 0
            page.wait_for_timeout(settings.aistudio_input_prime_seconds * 1000)
            after_delete_text = self._read_textarea_value_sync(textarea)
            done_diag = {
                "mode": "native",
                "phase": "done",
                "selected_point": selected_point,
                "click_checks": click_checks,
                "before_len": before_len,
                "after_type_len": len(after_type_text),
                "after_type_delta": len(after_type_text) - before_len,
                "after_type_endswith_x": after_type_text.endswith("x"),
                "key_attempts": key_attempts,
                "successful_key_attempt": successful_key_attempt["name"] if successful_key_attempt else None,
                "sendinput_backspace_sent": sent_backspace,
                "after_delete_len": len(after_delete_text),
                "after_delete_delta": len(after_delete_text) - before_len,
                "after_delete_equals_before": after_delete_text == before_text,
                "active_after_type": active_after_type,
                "metrics": metrics,
            }
            page.evaluate(
                "mw:(diag) => { window.__api_input_prime_done = true; window.__api_input_prime_mode = 'native'; window.__api_input_prime_diag = diag; }",
                done_diag,
            )
            if settings.response_diag:
                state = self._get_page_diag_state_sync()
                log.info(
                    "[response-diag] native input prime done: wait=%ss state=%s",
                    settings.aistudio_input_prime_seconds,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                )
            return True
        except Exception as exc:
            try:
                page.evaluate(
                    "mw:(diag) => { window.__api_input_prime_diag = diag; window.__api_input_prime_mode = 'native'; }",
                    {"mode": "native", "phase": "error", "error": str(exc)[:180]},
                )
            except Exception:
                pass
            if settings.response_diag:
                log.info("[response-diag] native input prime failed: %s", exc)
            return False
        finally:
            try:
                user32.SetCursorPos(original_point.x, original_point.y)
            except Exception:
                pass
            self._restore_textarea_value_sync(textarea, original_text)

    def _capture_template_sync(self, model: str) -> dict[str, Any]:
        import time as _t
        _t0 = _t.time()
        if model in self._templates:
            log.debug(f"[timing] template cached for {model}")
            return self._templates[model]

        page = self._ensure_botguard_service_sync()
        if self._bootstrap_template:
            captured = dict(self._bootstrap_template)
            self._templates[model] = captured
            log.debug(f"[timing] reused bootstrap template for {model} in {_t.time()-_t0:.1f}s")
            return captured
        log.debug(f"[timing] botguard done in {_t.time()-_t0:.1f}s, starting template capture")
        captured: dict[str, Any] = {}
        last_generate_response: dict[str, Any] | None = None

        def on_request(request):
            if not _is_makersuite_generate_content_url(request.url) or captured:
                return
            body = request.post_data
            if not body or len(body) <= 100:
                return
            captured["url"] = request.url
            captured["headers"] = dict(request.headers)
            captured["body"] = body
            if settings.response_diag:
                summary = _summarize_generate_body(body)
                summary["request"] = _summarize_request_meta(request)
                log.info(
                    "[response-diag] template capture request: %s",
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                )

        def on_response(response):
            nonlocal last_generate_response
            if not _is_makersuite_generate_content_url(response.url):
                return
            try:
                text = response.text()
            except Exception as exc:
                text = f"<response.text() failed: {exc}>"
            last_generate_response = {
                "status": response.status,
                "url": response.url,
                "body": text[:500],
            }
            if settings.response_diag:
                log.info(
                    "[response-diag] template capture response: status=%s body=%s",
                    response.status,
                    _short_diag_text(text, 300),
                )

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during template capture")
            original_text = self._read_textarea_value_sync(textarea)
            self._type_textarea_value_sync(page, textarea, TEMPLATE_CAPTURE_PROMPT)
            page.wait_for_timeout(500)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send during template capture")

            for _ in range(30):
                page.wait_for_timeout(1000)
                if captured:
                    break
            if not captured:
                if last_generate_response is not None:
                    raise RuntimeError(
                        "template capture failed after request: "
                        f"status={last_generate_response['status']} "
                        f"url={last_generate_response['url']} "
                        f"body={last_generate_response['body']}"
                    )
                raise RuntimeError(f"template capture timeout for model={model}")

            self._wait_until_idle_sync(page)
            self._restore_textarea_value_sync(textarea, original_text)
            self._templates[model] = captured
            log.debug(f"[timing] template captured for {model} in {_t.time()-_t0:.1f}s")
            return captured
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
            if 'textarea' in locals() and textarea is not None and 'original_text' in locals():
                self._restore_textarea_value_sync(textarea, original_text)

    def _generate_snapshot_sync(self, contents: list[AistudioContent]) -> str:
        page = self._ensure_botguard_service_sync()
        if not self._snap_key:
            raise RuntimeError("Snapshot function not detected")

        # 计算 content hash（包含图片数据，与 camoufox-api 一致）
        hash_parts: list[str] = []
        for content in contents:
            for part in content.parts:
                if part.inline_data:
                    hash_parts.append(part.inline_data[1])  # base64 data
                if part.text:
                    hash_parts.append(str(part.text))
                if part.function_call:
                    hash_parts.append(json.dumps(part.function_call, ensure_ascii=False, sort_keys=True))
                if part.function_response:
                    hash_parts.append(json.dumps(part.function_response, ensure_ascii=False, sort_keys=True))
        content_hash = sha256(" ".join(hash_parts).encode("utf-8")).hexdigest()

        page.evaluate(
            """
mw:((hash) => {
    const dms = window.default_MakerSuite;
    const service = window.__bg_service;
    const snapKey = window.__snap_key;
    if (!dms || !service || !snapKey || typeof dms[snapKey] !== 'function') {
        window.__sr = '';
        window.__sl = 0;
        window.__snap_error = 'service_unavailable';
        return;
    }
    window.__sr = '';
    window.__sl = 0;
    window.__snap_error = '';
    const result = dms[snapKey](service, hash);
    if (result instanceof Promise) {
        result.then((snapshot) => {
            window.__sr = snapshot || '';
            window.__sl = snapshot ? snapshot.length : 0;
        }).catch((error) => {
            window.__snap_error = String(error);
        });
        return;
    }
    window.__sr = result || '';
    window.__sl = result ? result.length : 0;
})(%s)
"""
            % json.dumps(content_hash)
        )
        for _ in range(20):
            if page.evaluate("mw:(window.__sl || 0)") > 0:
                break
            page.wait_for_timeout(500)

        snapshot = page.evaluate("mw:window.__sr")
        if snapshot:
            return snapshot
        error = page.evaluate("mw:window.__snap_error || ''")
        raise RuntimeError(f"Snapshot generation failed: {error or 'unknown'}")

    def _upload_images_sync(self, image_paths: list[str]) -> list[str]:
        if not image_paths:
            return []

        # 尝试非 UI 方式上传（更快、更可靠）
        # 需要在主线程中获取 cookies，因为 Playwright 的同步 API 有 greenlet 限制
        try:
            if self._ctx is not None:
                cookies = self._ctx.cookies()
                return self._upload_images_via_api_sync(image_paths, cookies)
        except Exception as e:
            # 如果非 UI 方式失败，回退到 UI 方式
            import logging
            logging.getLogger("aistudio").debug("Non-UI upload failed, falling back to UI: %s", e)
            pass

        # UI 方式上传（原有逻辑）
        page = self._ensure_botguard_service_sync()
        self._wait_until_idle_sync(page)
        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            for image_path in image_paths:
                target_count = len(uploaded_ids) + 1
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_btn = page.locator('[aria-label="Insert images, videos, audio, or files"]').first
                if not upload_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload button not visible")
                upload_btn.click()
                page.wait_for_timeout(1500)
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    upload_btn.click()
                    page.wait_for_timeout(1000)
                    upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload files button not visible")
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    upload_files_btn.click()
                chooser_info.value.set_files(image_path)

                deadline = time.time() + 30
                while time.time() < deadline:
                    if len(uploaded_ids) >= target_count:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _upload_images_via_api_sync(self, image_paths: list[str], cookies: list[dict]) -> list[str]:
        """通过 Playwright 的 setInputFiles 方法上传图片（非 UI 点击方式）"""
        page = self._hook_page
        if page is None:
            raise RuntimeError("Hook page not initialized")

        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            # 找到文件输入元素（如果有的话）
            file_input = page.query_selector('input[type="file"]')

            if file_input:
                # 直接使用 setInputFiles 方法上传
                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)
            else:
                # 如果没有 file input，尝试创建一个
                page.evaluate("""
                    () => {
                        const input = document.createElement('input');
                        input.type = 'file';
                        input.id = '__api_file_input__';
                        input.style.display = 'none';
                        input.accept = 'image/*';
                        document.body.appendChild(input);

                        // 监听文件选择事件
                        input.addEventListener('change', (e) => {
                            const file = e.target.files[0];
                            if (file) {
                                // 触发上传逻辑
                                window.__api_upload_file = file;
                            }
                        });
                    }
                """)

                file_input = page.query_selector('#__api_file_input__')
                if not file_input:
                    raise RuntimeError("Failed to create file input")

                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)
                    page.wait_for_timeout(1000)

                    # 触发上传
                    page.evaluate("""
                        () => {
                            if (window.__api_upload_file) {
                                // 模拟拖放或触发上传按钮
                                const event = new Event('change', { bubbles: true });
                                const input = document.querySelector('#__api_file_input__');
                                if (input) input.dispatchEvent(event);
                            }
                        }
                    """)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)

        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _send_hooked_request_sync(self, body: str, timeout_ms: int) -> tuple[int, bytes]:
        import time as _t
        _t0 = _t.time()
        page = self._ensure_botguard_service_sync()
        log.debug(f"[timing] botguard ready in {_t.time()-_t0:.1f}s")
        captured_url, captured_headers = self._get_captured_info()

        # Replay via XHR in browser context (same approach as non-streaming replay_v2)
        timeout_s = timeout_ms / 1000
        if settings.response_diag:
            summary = _summarize_generate_body(body)
            summary["request"] = _summarize_raw_request_meta("POST", captured_url, captured_headers)
            log.info(
                "[response-diag] replay xhr request: %s",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )
        result = page.evaluate("""(args) => {
            return new Promise((resolve) => {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', args.url);
                var h = args.headers;
                for (var k in h) {
                    xhr.setRequestHeader(k, h[k]);
                }
                xhr.withCredentials = true;
                xhr.timeout = args.timeout * 1000;
                xhr.onload = function() {
                    resolve({status: xhr.status, body: xhr.responseText});
                };
                xhr.onerror = function() {
                    resolve({status: 0, body: 'network error'});
                };
                xhr.ontimeout = function() {
                    resolve({status: 0, body: 'timeout'});
                };
                xhr.send(args.body);
            });
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
        })

        status = result.get("status", 0)
        raw_text = result.get("body", "")
        if settings.response_diag:
            log.info(
                "[response-diag] replay xhr response: status=%s body=%s",
                status,
                _short_diag_text(raw_text, 300),
            )
        log.debug(f"[timing] replay done in {_t.time()-_t0:.1f}s, status={status}")
        if status == 0:
            raise RuntimeError(f"replay failed: {raw_text}")
        return status, raw_text.encode("utf-8")

    def _goto_aistudio_sync(self, page) -> None:
        import time as _t
        last_exc = None
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
            try:
                _t0 = _t.time()
                page.goto(url, wait_until="networkidle", timeout=30000)
                log.debug(f"[timing] goto {url} took {_t.time()-_t0:.1f}s")
                # 检查是否被重定向到登录页
                current_url = page.url or ""
                if "accounts.google.com" in current_url and "signin" in current_url:
                    raise RuntimeError(
                        f"Cookie 认证失败，已被重定向到 Google 登录页。"
                        f" (url={current_url})"
                    )
                # Wait for SPA framework and chat UI to render
                for _ in range(60):
                    page.wait_for_timeout(1000)
                    has_dms = page.evaluate("mw:!!window.default_MakerSuite")
                    has_textarea = page.query_selector("textarea") is not None
                    if has_dms and has_textarea:
                        log.debug(f"[timing] UI ready (dms+textarea) after {_t.time()-_t0:.1f}s")
                        self._save_cookies_sync()
                        return
                    if has_dms and _ > 20:
                        page.evaluate(DIALOG_CLEANUP_JS)
                log.debug(f"[timing] UI partially ready after {_t.time()-_t0:.1f}s (dms={has_dms}, textarea={has_textarea})")
                self._save_cookies_sync()
                return
            except Exception as exc:
                log.debug(f"[timing] goto {url} failed after {_t.time()-_t0:.1f}s: {exc}")
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    def _install_hooks_sync(self, page) -> None:
        self._install_response_diag_sync(page)
        result = page.evaluate(INSTALL_HOOKS_JS)
        if result == "already_hooked":
            return
        if isinstance(result, str) and result.startswith("hooked:"):
            self._snap_key = result.split(":", 1)[1]
            return
        for _ in range(3):
            page.wait_for_timeout(2000)
            result = page.evaluate(INSTALL_HOOKS_JS)
            if result == "already_hooked":
                return
            if isinstance(result, str) and result.startswith("hooked:"):
                self._snap_key = result.split(":", 1)[1]
                return
        page_url = page.url if page else "(no page)"
        page_title = ""
        try:
            page_title = page.title()
        except Exception:
            pass
        raise RuntimeError(f"Hook install failed: {result} (url={page_url}, title={page_title!r})")

    def _install_response_diag_sync(self, page) -> None:
        if not settings.response_diag:
            return
        self._install_response_diag_context_sync(page)
        page_id = id(page)
        if page_id in self._response_diag_pages:
            return
        self._response_diag_pages.add(page_id)

        def on_request(request):
            if not _is_response_diag_url(request.url):
                return
            summary = _summarize_generate_body(request.post_data)
            summary["request"] = _summarize_request_meta(request)
            log.info(
                "[response-diag] page GenerateContent request: %s",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )

        def on_response(response):
            if not _is_response_diag_url(response.url):
                return
            try:
                body = response.text()
            except Exception as exc:
                body = f"<response.text() failed: {exc}>"
            summary = _summarize_generate_body(response.request.post_data)
            summary["request"] = _summarize_request_meta(response.request)
            log.info(
                "[response-diag] page GenerateContent response: status=%s request=%s body=%s",
                response.status,
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                _short_diag_text(body, 300),
            )

        page.on("request", on_request)
        page.on("response", on_response)

    def _install_response_diag_context_sync(self, page) -> None:
        ctx = self._ctx
        if ctx is None:
            try:
                ctx = page.context
            except Exception:
                ctx = None
        if ctx is None:
            return

        ctx_id = id(ctx)
        if ctx_id in self._response_diag_contexts:
            return
        self._response_diag_contexts.add(ctx_id)

        def on_request(request):
            if not _is_response_diag_url(request.url):
                return
            summary = _summarize_generate_body(request.post_data)
            summary["request"] = _summarize_request_meta(request)
            log.info(
                "[response-diag] context request: %s",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )

        def on_response(response):
            if not _is_response_diag_url(response.url):
                return
            try:
                body = response.text()
            except Exception as exc:
                body = f"<response.text() failed: {exc}>"
            summary = _summarize_generate_body(response.request.post_data)
            summary["request"] = _summarize_request_meta(response.request)
            log.info(
                "[response-diag] context response: status=%s request=%s body=%s",
                response.status,
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                _short_diag_text(body, 300),
            )

        ctx.on("request", on_request)
        ctx.on("response", on_response)

    def _click_run_button_sync(self, page) -> bool:
        click_error: Exception | None = None
        try:
            button = page.query_selector("button:has-text('Run')")
        except Exception as exc:
            click_error = exc
            button = None
        if button is None:
            self._log_click_diag_sync(page, "run button not found", click_error)
            return False
        try:
            button.click()
            return True
        except Exception as exc:
            self._log_click_diag_sync(page, "run button click failed", exc)
            return False

    def _log_click_diag_sync(self, page, reason: str, error: Exception | None = None) -> None:
        if not settings.response_diag:
            return
        try:
            snapshot = page.evaluate(
                """() => ({
                    url: location.href,
                    title: document.title,
                    textareaCount: document.querySelectorAll('textarea').length,
                    textareaValues: Array.from(document.querySelectorAll('textarea')).slice(0, 3).map((node) => node.value || ''),
                    activeTag: document.activeElement ? document.activeElement.tagName : null,
                    activeText: document.activeElement ? (document.activeElement.textContent || '').trim().slice(0, 80) : null,
                    buttons: Array.from(document.querySelectorAll('button')).slice(0, 30).map((button) => ({
                        text: (button.textContent || '').trim().slice(0, 80),
                        aria: button.getAttribute('aria-label'),
                        title: button.getAttribute('title'),
                        disabled: button.disabled || button.getAttribute('aria-disabled') === 'true',
                        visible: !!(button.offsetWidth || button.offsetHeight || button.getClientRects().length),
                    })),
                })"""
            )
        except Exception as exc:
            snapshot = {"snapshot_error": str(exc)}
        log.info(
            "[response-diag] click Run failed: reason=%s error=%s snapshot=%s",
            reason,
            str(error) if error else None,
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        )

    def _has_run_button_sync(self, page) -> bool:
        try:
            return page.query_selector("button:has-text('Run')") is not None
        except Exception:
            return False

    def _wait_until_idle_sync(self, page) -> None:
        for _ in range(60):
            if self._has_run_button_sync(page):
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("page never became idle")

    def _read_textarea_value_sync(self, textarea) -> str:
        try:
            return textarea.input_value()
        except Exception:
            return ""

    def _type_textarea_value_sync(self, page, textarea, value: str) -> None:
        try:
            textarea.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(value, delay=25)
            return
        except Exception as exc:
            if settings.response_diag:
                log.info("[response-diag] keyboard textarea input failed, falling back to fill: %s", exc)
        textarea.fill(value)

    def _restore_textarea_value_sync(self, textarea, value: str) -> None:
        try:
            current = textarea.input_value()
        except Exception:
            current = None
        if current == value:
            return
        try:
            textarea.fill(value)
        except Exception:
            pass

    def _close_sync(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._browser is not None and self._cf is None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._cf is not None:
            try:
                self._cf.__exit__(None, None, None)
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._playwright = None
        self._snap_key = None
