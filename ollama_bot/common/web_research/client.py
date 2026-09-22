"""
web_research/client.py

このファイルはChrome MCPブラウザクライアントとブラウザ操作の基盤を提供します。
主な責務:
- MCPChromeHTTPClient: HTTP経由でChrome MCP拡張と通信するクライアントクラス
- ブラウザファイルロック・非同期ロックによる排他制御
- MCPセッションIDの永続化（ファイルベース）
- ブラウザナビゲーション・コンテンツ読み取り・タブ操作

AIエージェントがブラウザ操作を変更する場合はこのファイルを修正してください。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any, Iterable

import aiohttp

from ..config_helpers import cfg, cfg_bool, cfg_float, cfg_int
from ..http_session import get_shared_http_session
from ..json_compat import loads as json_loads
from ..ollama_helpers import truncate_text
from .config import (
    _MCP_PROTOCOL_VERSION,
    _browser_allowed_tool_names,
    _browser_blocked_tool_keywords,
    _browser_debug_enabled,
    _browser_debug_preview_chars,
    _browser_lock_file_path,
    _browser_post_navigation_delay_sec,
    _browser_read_retries,
    _browser_read_retry_delay_sec,
    _is_dynamic_browser_site,
    _mcp_session_file_path,
    _safe_browser_content_char_limit,
    _should_use_background_browser_access,
)
from .helpers import (
    _compact_whitespace,
)
from .parsing import (
    _extract_browser_page,
    _extract_tab_id,
    _extract_text_from_tool_result,
    _iter_tool_result_payloads,
    _navigation_result_message,
    _should_close_browser_tab_after_navigation,
    _web_content_arg_variants,
)

with contextlib.suppress(ImportError):
    import fcntl

log = logging.getLogger("ollama_bot.common.web_research")

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class MCPInvalidSessionError(RuntimeError):
    pass


class MCPTransportAlreadyConnectedError(RuntimeError):
    pass


def _is_invalid_mcp_session_error(error_text: Any) -> bool:
    lowered = str(error_text or "").lower()
    return "invalid mcp request or session" in lowered or (
        "invalid" in lowered and "mcp" in lowered and "session" in lowered
    )


def _is_mcp_transport_already_connected_error(error_text: Any) -> bool:
    lowered = str(error_text or "").lower()
    return "already connected to a transport" in lowered or (
        "call close() before connecting" in lowered and "transport" in lowered
    )


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_browser_operation_lock: asyncio.Lock | None = None
_shared_mcp_client: "MCPChromeHTTPClient | None" = None


def _browser_operation_local_lock() -> asyncio.Lock:
    global _browser_operation_lock
    if _browser_operation_lock is None:
        _browser_operation_lock = asyncio.Lock()
    return _browser_operation_lock


# ---------------------------------------------------------------------------
# File lock helpers
# ---------------------------------------------------------------------------


def _acquire_browser_file_lock(path: str) -> Any:
    if "fcntl" not in globals():
        return None
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_browser_file_lock(handle: Any) -> None:
    if handle is None or "fcntl" not in globals():
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


@contextlib.asynccontextmanager
async def _browser_operation_guard() -> Any:
    lock = _browser_operation_local_lock()
    await lock.acquire()
    file_handle = None
    try:
        file_handle = await asyncio.to_thread(_acquire_browser_file_lock, _browser_lock_file_path())
        yield
    finally:
        if file_handle is not None:
            await asyncio.to_thread(_release_browser_file_lock, file_handle)
        lock.release()


# ---------------------------------------------------------------------------
# MCP session persistence
# ---------------------------------------------------------------------------


def _read_saved_mcp_session_id() -> str:
    path = _mcp_session_file_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return str(handle.read() or "").strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.debug("failed to read saved Chrome MCP session id path=%s err=%r", path, e)
        return ""


def _write_saved_mcp_session_id(session_id: str) -> None:
    normalized = str(session_id or "").strip()
    if not normalized:
        return
    path = _mcp_session_file_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(normalized)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception as e:
        log.debug("failed to save Chrome MCP session id path=%s err=%r", path, e)


def _clear_saved_mcp_session_id(*, expected_session_id: str | None = None) -> None:
    expected = None if expected_session_id is None else str(expected_session_id or "").strip()
    if expected is not None and _read_saved_mcp_session_id() != expected:
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(_mcp_session_file_path())


# ---------------------------------------------------------------------------
# Shared client
# ---------------------------------------------------------------------------


def _get_shared_mcp_client() -> "MCPChromeHTTPClient":
    global _shared_mcp_client
    endpoint = str(cfg("WEB_RESEARCH_MCP_URL", "http://127.0.0.1:12306/mcp") or "").strip()
    timeout_sec = cfg_float("WEB_RESEARCH_MCP_TIMEOUT_SEC", 20.0)
    if (
        _shared_mcp_client is None
        or _shared_mcp_client.endpoint != endpoint
        or float(_shared_mcp_client.timeout_sec) != float(timeout_sec)
    ):
        _shared_mcp_client = MCPChromeHTTPClient(endpoint=endpoint, timeout_sec=timeout_sec)
    return _shared_mcp_client


def _reset_shared_mcp_client() -> None:
    global _shared_mcp_client
    _shared_mcp_client = None


# ---------------------------------------------------------------------------
# Browser navigation helpers
# ---------------------------------------------------------------------------


def _browser_result_preview(text: Any, *, fallback_chars: int = 220) -> str:
    limit = _browser_debug_preview_chars() or fallback_chars
    if limit <= 0:
        return ""
    return truncate_text(_compact_whitespace(str(text or "")), limit)


def _browser_tool_rejection_reason(name: str) -> str:
    normalized = str(name or "").strip()
    lowered = normalized.lower()
    if not normalized:
        return "tool name was empty"
    for keyword in _browser_blocked_tool_keywords():
        if keyword and keyword in lowered:
            return f"tool contains blocked keyword '{keyword}'"
    if normalized not in _browser_allowed_tool_names():
        return "tool is not in the allowlist"
    return ""


def _mcp_tool_map(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(tool.get("name") or "").strip(): tool
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    }


def _mcp_tool_schema_properties(tool_map: dict[str, dict[str, Any]], tool_name: str) -> dict[str, Any]:
    input_schema = tool_map.get(tool_name, {}).get("inputSchema")
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    return properties if isinstance(properties, dict) else {}


def _build_navigate_args(
    tool_map: dict[str, dict[str, Any]],
    url: str = "",
    *,
    prefer_new_window: bool = True,
    tab_id: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    properties = _mcp_tool_schema_properties(tool_map, "chrome_navigate")
    args: dict[str, Any] = {}
    if url:
        args["url"] = url
    if tab_id is not None and ("tabId" in properties or not properties):
        args["tabId"] = tab_id
    if refresh and "refresh" in properties:
        args["refresh"] = True
    if prefer_new_window:
        if "newWindow" in properties:
            args["newWindow"] = True
        if "background" in properties and _should_use_background_browser_access(url):
            args["background"] = True
    if "width" in properties:
        args["width"] = cfg_int("WEB_RESEARCH_BROWSER_VIEWPORT_WIDTH", 1280)
    if "height" in properties:
        args["height"] = cfg_int("WEB_RESEARCH_BROWSER_VIEWPORT_HEIGHT", 720)
    return args


async def _close_browser_tab_if_possible(
    client: "MCPChromeHTTPClient",
    tool_map: dict[str, dict[str, Any]],
    tab_id: int | None,
) -> None:
    if tab_id is None or "chrome_close_tabs" not in tool_map:
        return
    with contextlib.suppress(Exception):
        await client.call_tool("chrome_close_tabs", {"tabIds": [tab_id]})


async def _sleep_after_browser_navigation(url: str, *, reason: str) -> None:
    delay_sec = _browser_post_navigation_delay_sec(url)
    if delay_sec <= 0:
        return
    if _browser_debug_enabled():
        log.info(
            "Chrome MCP browser settle wait reason=%s url=%s delay_sec=%.2f",
            reason, url, delay_sec,
        )
    await asyncio.sleep(delay_sec)


async def _read_browser_content_with_retries(
    client: "MCPChromeHTTPClient",
    tool_map: dict[str, dict[str, Any]],
    *,
    url: str,
    tab_id: int | None,
    preferred_format: str,
    reject_login_wall: bool,
    selector: str = "",
    is_login_wall_fn=None,
) -> dict[str, Any]:
    attempts = _browser_read_retries()
    retry_delay_sec = _browser_read_retry_delay_sec()
    last_error: Exception | None = None
    last_result: dict[str, Any] | None = None
    variants = _web_content_arg_variants(
        tool_map,
        url=url,
        tab_id=tab_id,
        preferred_format=preferred_format,
        selector=selector,
    )

    for attempt in range(1, attempts + 1):
        for args in variants:
            try:
                result = await client.call_tool("chrome_get_web_content", args)
                last_result = result
                extracted_text = _extract_text_from_tool_result(result)
                if not reject_login_wall:
                    return result
                if extracted_text and (is_login_wall_fn is None or not is_login_wall_fn(extracted_text)):
                    return result
                break
            except Exception as e:
                last_error = e

        if attempt < attempts and retry_delay_sec > 0:
            await asyncio.sleep(retry_delay_sec)

    if last_result is not None:
        return last_result
    raise last_error or RuntimeError("Failed to read content from browser")


async def _refresh_x_timeline_tab_if_reused(
    client: "MCPChromeHTTPClient",
    tool_map: dict[str, dict[str, Any]],
    *,
    navigate_result: dict[str, Any],
    url: str,
    tab_id: int | None,
) -> str:
    if not cfg_bool("WEB_RESEARCH_X_TIMELINE_REFRESH_EXISTING_TAB", True):
        return url
    if _should_close_browser_tab_after_navigation(navigate_result):
        return url
    if tab_id is None:
        return url

    properties = _mcp_tool_schema_properties(tool_map, "chrome_navigate")
    if "refresh" in properties:
        refresh_args = _build_navigate_args(tool_map, prefer_new_window=False, tab_id=tab_id, refresh=True)
    elif "tabId" in properties or not properties:
        refresh_args = _build_navigate_args(tool_map, url, prefer_new_window=False, tab_id=tab_id)
    else:
        return url

    try:
        await client.call_tool("chrome_navigate", refresh_args)
        await _sleep_after_browser_navigation(url, reason="x_timeline_refresh")
    except Exception as e:
        log.warning("x timeline existing tab refresh failed; reading current tab: %r", e)
    return url


async def _read_search_results_with_browser(
    client: "MCPChromeHTTPClient",
    query: str,
    *,
    max_results: int,
) -> dict[str, Any]:
    if not query:
        return {}

    from .parsing import _search_url_for_query
    async with _browser_operation_guard():
        tools = await client.list_tools()
        tool_map = _mcp_tool_map(tools)
        if "chrome_navigate" not in tool_map or "chrome_get_web_content" not in tool_map:
            raise RuntimeError("Required Chrome MCP tools are not available")

        search_url = _search_url_for_query(query)
        navigate_result = await client.call_tool(
            "chrome_navigate",
            _build_navigate_args(tool_map, search_url, prefer_new_window=True),
        )
        tab_id = _extract_tab_id(navigate_result)
        await _sleep_after_browser_navigation(search_url, reason="search")
        try:
            return await _read_browser_content_with_retries(
                client,
                tool_map,
                url=search_url,
                tab_id=tab_id,
                preferred_format="html",
                reject_login_wall=False,
            )
        finally:
            if _should_close_browser_tab_after_navigation(navigate_result):
                await _close_browser_tab_if_possible(client, tool_map, tab_id)


# ---------------------------------------------------------------------------
# MCPChromeHTTPClient
# ---------------------------------------------------------------------------


class MCPChromeHTTPClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        timeout_sec: float | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        blocked_tool_keywords: Iterable[str] | None = None,
    ) -> None:
        self.endpoint = str(endpoint or cfg("WEB_RESEARCH_MCP_URL", "http://127.0.0.1:12306/mcp") or "").strip()
        self.timeout_sec = float(timeout_sec if timeout_sec is not None else cfg_float("WEB_RESEARCH_MCP_TIMEOUT_SEC", 20.0))
        self._request_id = 0
        self._session_id = _read_saved_mcp_session_id()
        self._protocol_version = _MCP_PROTOCOL_VERSION
        self._initialized = bool(self._session_id)
        self._tools_cache: list[dict[str, Any]] | None = None
        self._allowed_tool_names = (
            {str(name or "").strip() for name in allowed_tool_names if str(name or "").strip()}
            if allowed_tool_names is not None
            else _browser_allowed_tool_names()
        )
        self._blocked_tool_keywords = (
            tuple(str(k or "").strip().lower() for k in blocked_tool_keywords if str(k or "").strip())
            if blocked_tool_keywords is not None
            else _browser_blocked_tool_keywords()
        )

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _tool_rejection_reason(self, name: str) -> str:
        normalized = str(name or "").strip()
        lowered = normalized.lower()
        if not normalized:
            return "tool name was empty"
        for keyword in self._blocked_tool_keywords:
            if keyword and keyword in lowered:
                return f"tool contains blocked keyword '{keyword}'"
        if normalized not in self._allowed_tool_names:
            return "tool is not in the allowlist"
        return ""

    def _reset_session_state(self, *, clear_saved: bool = True, expected_session_id: str | None = None) -> None:
        self._session_id = ""
        self._initialized = False
        self._tools_cache = None
        if clear_saved:
            _clear_saved_mcp_session_id(expected_session_id=expected_session_id)

    def _activate_saved_session_if_available(self) -> bool:
        saved_session_id = _read_saved_mcp_session_id()
        if not saved_session_id:
            return False
        self._session_id = saved_session_id
        self._initialized = True
        self._tools_cache = None
        return True

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._activate_saved_session_if_available():
            return
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "discord-ai-bot", "version": "1.0"},
                },
            )
        except MCPTransportAlreadyConnectedError:
            if self._activate_saved_session_if_available():
                log.info("Chrome MCP transport already connected; reused saved session id")
                return
            raise
        self._protocol_version = str(result.get("protocolVersion") or self._protocol_version)
        self._initialized = True
        _write_saved_mcp_session_id(self._session_id)
        with contextlib.suppress(Exception):
            await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        if self._tools_cache is not None:
            return list(self._tools_cache)
        result = await self._request_with_session_recovery("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else []
        allowed_tools: list[dict[str, Any]] = []
        for tool in list(tools or []):
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if self._tool_rejection_reason(name):
                continue
            allowed_tools.append(tool)
        self._tools_cache = allowed_tools
        return list(self._tools_cache)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.initialize()
        normalized_name = str(name or "").strip()
        rejection_reason = self._tool_rejection_reason(normalized_name)
        if rejection_reason:
            raise RuntimeError(f"Blocked Chrome MCP tool '{normalized_name}': {rejection_reason}")
        log.info("MCP call_tool started: name=%s", normalized_name)
        result = await self._request_with_session_recovery(
            "tools/call",
            {"name": normalized_name, "arguments": dict(arguments or {})},
        )
        log.info("MCP call_tool finished: name=%s", normalized_name)
        if bool(result.get("isError")):
            raise RuntimeError(_compact_whitespace(_extract_text_from_tool_result(result)) or f"MCP tool failed: {name}")
        return result

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post_jsonrpc(
            {"jsonrpc": "2.0", "method": method, "params": dict(params or {})},
            expect_response=False,
        )

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._post_jsonrpc(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": dict(params or {})},
            expect_response=True,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"Invalid MCP response for {method}")
        error = response.get("error")
        if isinstance(error, dict):
            message = _compact_whitespace(error.get("message")) or f"MCP request failed: {method}"
            if _is_invalid_mcp_session_error(message):
                invalid_session_id = self._session_id
                self._reset_session_state(clear_saved=bool(invalid_session_id), expected_session_id=invalid_session_id or None)
                raise MCPInvalidSessionError(message)
            if _is_mcp_transport_already_connected_error(message):
                raise MCPTransportAlreadyConnectedError(message)
            raise RuntimeError(message)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP result missing for {method}")
        return result

    async def _request_with_session_recovery(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._request(method, params)
        except MCPInvalidSessionError:
            log.info("Chrome MCP session became invalid; reinitializing before retrying method=%s", method)
            self._reset_session_state(clear_saved=False)
            await self.initialize()
            return await self._request(method, params)

    async def _post_jsonrpc(self, payload: dict[str, Any], *, expect_response: bool) -> dict[str, Any] | None:
        session = await get_shared_http_session(timeout_sec=self.timeout_sec)
        method = str(payload.get("method") or "").strip()
        request_id = payload.get("id")
        started_at = time.monotonic()
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version,
        }
        sent_session_id = self._session_id
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        log.info("MCP request started: method=%s id=%s endpoint=%s timeout=%.1fs", method, request_id, self.endpoint, self.timeout_sec)
        try:
            async with session.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
            ) as response:
                response_text = await response.text()
                elapsed = time.monotonic() - started_at
                content_type = str(response.headers.get("Content-Type", "") or "").lower()
                response_session_id = str(response.headers.get("Mcp-Session-Id") or "").strip()
                log.info("MCP request completed: method=%s id=%s status=%s len=%d type=%s session=%s elapsed=%.2fs", 
                         method, request_id, response.status, len(response_text), content_type, response_session_id, elapsed)
                
                if response.status >= 400:
                    preview = truncate_text(response_text, 400)
                    if _is_invalid_mcp_session_error(preview):
                        self._reset_session_state(clear_saved=bool(sent_session_id), expected_session_id=sent_session_id or None)
                        raise MCPInvalidSessionError(f"MCP HTTP error status={response.status}: {preview}")
                    if _is_mcp_transport_already_connected_error(preview):
                        raise MCPTransportAlreadyConnectedError(f"MCP HTTP error status={response.status}: {preview}")
                    raise RuntimeError(f"MCP HTTP error status={response.status}: {preview}")
        except Exception as e:
            elapsed = time.monotonic() - started_at
            log.info("MCP request failed: method=%s id=%s elapsed=%.2fs err=%r", method, request_id, elapsed, e)
            raise

        if response_session_id:
            self._session_id = response_session_id
            _write_saved_mcp_session_id(response_session_id)

        if not expect_response or not response_text.strip():
            return None
        return self._decode_response_text(response_text, content_type=content_type)

    def _decode_response_text(self, response_text: str, *, content_type: str) -> dict[str, Any]:
        if "text/event-stream" in content_type:
            events = self._parse_sse_events(response_text)
            for event in reversed(events):
                if isinstance(event, dict):
                    return event
            raise RuntimeError("Failed to decode MCP SSE response")
        parsed = json_loads(response_text)
        if not isinstance(parsed, dict):
            raise RuntimeError("MCP response was not a JSON object")
        return parsed

    def _parse_sse_events(self, payload: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        current_data: list[str] = []

        def _flush() -> None:
            if not current_data:
                return
            raw = "\n".join(current_data).strip()
            current_data.clear()
            if not raw or raw == "[DONE]":
                return
            with contextlib.suppress(Exception):
                parsed = json_loads(raw)
                if isinstance(parsed, dict):
                    events.append(parsed)

        for line in payload.splitlines():
            if line.startswith("data:"):
                current_data.append(line[5:].strip())
            elif not line.strip():
                _flush()
        _flush()
        return events
