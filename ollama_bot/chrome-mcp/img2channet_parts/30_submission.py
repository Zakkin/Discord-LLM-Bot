# Reply form parsing and IMG submission via Chrome MCP or direct HTTP fallback.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import asyncio
import contextlib
import html
from html.parser import HTMLParser
import json
import logging
import re
from typing import Any, Optional, TYPE_CHECKING
from urllib.parse import urljoin

import aiohttp

from lib.html_utils import clean_html
from lib.img2chan_utils import (
    _IMG_THREAD_URL_PATTERN,
    default_browser_headers as _default_headers,
    normalize_img_thread_url,
)

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")

if TYPE_CHECKING:
    def sanitize_img_post_submission_text(text: str, *, max_chars: int = 120, preserve_urls: Any = False, **kwargs: Any) -> str: ...
    async def fetch_img_thread(url: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def find_img_thread_reply_by_comment(thread: dict[str, Any] | None, comment: str, **kwargs: Any) -> dict[str, Any] | None: ...

_IMG2CHAN_POST_STATUS_SELECTOR = "#retmestip"
_IMG2CHAN_POST_TEXTAREA_SELECTOR = "#ftxa"
_IMG2CHAN_POST_EMAIL_SELECTOR = "input[name='email']"
_IMG2CHAN_POST_PASSWORD_SELECTOR = "input[name='pwd']"
_IMG2CHAN_POST_SUBMIT_SELECTOR = "#fm input[type='submit']"
_IMG2CHAN_POST_CACHE_TOKEN_URL = "https://img.2chan.net/bin/cachemt7.php"
_IMG2CHAN_POST_MCP_ALLOWED_TOOLS = (
    "chrome_navigate",
    "chrome_get_web_content",
    "chrome_read_page",
    "chrome_close_tabs",
    "chrome_click_element",
    "chrome_fill_or_select",
    "chrome_keyboard",
    "chrome_get_interactive_elements",
)
_IMG2CHAN_POST_MCP_BLOCKED_TOOL_KEYWORDS = (
    "download",
    "upload",
    "delete",
    "remove",
    "purchase",
    "buy",
    "checkout",
    "cart",
    "payment",
)

import os



class _FutabaReplyFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.form_depth = 0
        self.capture_textarea_name = ""
        self.action = ""
        self.method = "POST"
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {str(k).lower(): ("" if v is None else str(v)) for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "form":
            if self.in_form:
                self.form_depth += 1
                return
            form_id = attr.get("id", "")
            if form_id == "fm":
                self.in_form = True
                self.form_depth = 1
                self.action = attr.get("action", "")
                self.method = attr.get("method", "POST") or "POST"
            return

        if not self.in_form:
            return

        if tag_l == "input":
            name = attr.get("name", "")
            if not name:
                return
            input_type = attr.get("type", "text").lower()
            if input_type in {"submit", "button", "reset", "image", "file"}:
                return
            self.fields[name] = attr.get("value", "")
        elif tag_l == "textarea":
            name = attr.get("name", "")
            if name:
                self.capture_textarea_name = name
                self.fields.setdefault(name, "")

    def handle_data(self, data: str) -> None:
        if self.in_form and self.capture_textarea_name:
            self.fields[self.capture_textarea_name] = self.fields.get(self.capture_textarea_name, "") + data

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if self.in_form and tag_l == "textarea":
            self.capture_textarea_name = ""
        if self.in_form and tag_l == "form":
            self.form_depth -= 1
            if self.form_depth <= 0:
                self.in_form = False


def extract_img_reply_form(html_text: str) -> dict[str, Any]:
    """スレHTMLから返信フォームの action とフィールドを取り出す。"""
    parser = _FutabaReplyFormParser()
    parser.feed(str(html_text or ""))
    return {
        "action": parser.action,
        "method": parser.method,
        "fields": dict(parser.fields),
    }


def _looks_like_successful_post_response(text: str) -> bool:
    stripped = str(text or "").strip()
    if stripped == "ok":
        return True
    if not stripped:
        return False
    with contextlib.suppress(Exception):
        parsed = json.loads(stripped)
        return isinstance(parsed, (dict, list))
    return False


def extract_img_post_cache_token(script_text: object) -> str:
    """cachemt7.php 内の caco() から、投稿前に pthc へ入れるトークンを取り出す。"""
    match = re.search(
        r"function\s+caco\s*\(\s*\)\s*\{\s*return\s*['\"]([^'\"]+)['\"]\s*;?\s*\}",
        str(script_text or ""),
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


async def _fetch_img_post_cache_token(
    session: aiohttp.ClientSession,
    *,
    referer: str,
) -> str:
    try:
        async with session.get(
            _IMG2CHAN_POST_CACHE_TOKEN_URL,
            headers=_default_headers(referer=referer),
        ) as resp:
            if resp.status != 200:
                return ""
            script_text = await resp.text(errors="ignore")
    except Exception as e:
        log.debug("img2chan post cache token fetch failed: %r", e)
        return ""
    return extract_img_post_cache_token(script_text)


def _prepare_img_reply_form_fields(
    fields: dict[str, Any],
    *,
    thread_no: str,
    comment: str,
    email: str = "",
    password: str = "",
    cache_token: str = "",
    screen_size: str = "1280x720x24",
) -> dict[str, str]:
    prepared = {str(key): str(value) for key, value in dict(fields or {}).items()}
    normalized_token = str(cache_token or "").strip()
    if normalized_token:
        prepared["pthc"] = normalized_token
    prepared.update({
        "mode": "regist",
        "resto": str(thread_no or prepared.get("resto") or ""),
        "com": str(comment or ""),
        "email": str(email or ""),
        "pwd": str(password or prepared.get("pwd") or ""),
        "responsemode": "ajax",
        "js": "on",
        "scsz": str(screen_size or prepared.get("scsz") or "1280x720x24"),
    })
    return prepared


def _compact_error_text(text: str, *, max_chars: int = 180) -> str:
    value = clean_html(str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > max_chars:
        value = value[:max_chars].rstrip() + "..."
    return value


def _img2chan_post_mcp_enabled() -> bool:
    value = str(os.environ.get("IMG2CHAN_POST_MCP_ENABLED", "true") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _load_img2chan_mcp_bridge() -> dict[str, Any] | None:
    try:
        from ollama_bot.common.web_research.client import (
            MCPChromeHTTPClient,
            _browser_operation_guard,
            _build_navigate_args,
            _close_browser_tab_if_possible,
            _mcp_tool_map,
            _mcp_tool_schema_properties,
            _sleep_after_browser_navigation,
        )
        from ollama_bot.common.web_research.parsing import (
            _build_get_web_content_args,
            _extract_tab_id,
            _extract_text_from_tool_result,
            _should_close_browser_tab_after_navigation,
        )
    except Exception as e:
        log.warning("img2chan mcp bridge import failed: %r", e)
        return None

    return {
        "MCPChromeHTTPClient": MCPChromeHTTPClient,
        "_browser_operation_guard": _browser_operation_guard,
        "_mcp_tool_map": _mcp_tool_map,
        "_mcp_tool_schema_properties": _mcp_tool_schema_properties,
        "_build_navigate_args": _build_navigate_args,
        "_build_get_web_content_args": _build_get_web_content_args,
        "_extract_tab_id": _extract_tab_id,
        "_extract_text_from_tool_result": _extract_text_from_tool_result,
        "_close_browser_tab_if_possible": _close_browser_tab_if_possible,
        "_should_close_browser_tab_after_navigation": _should_close_browser_tab_after_navigation,
        "_sleep_after_browser_navigation": _sleep_after_browser_navigation,
    }


def _pick_mcp_selector_arg_name(properties: dict[str, Any]) -> str:
    for key in ("selector", "cssSelector", "css", "query", "elementSelector"):
        if key in properties:
            return key
    return "selector" if not properties else ""


def _pick_mcp_value_arg_name(properties: dict[str, Any]) -> str:
    for key in ("value", "text", "input", "content"):
        if key in properties:
            return key
    return "value" if not properties else ""


def _build_mcp_fill_args(
    bridge: dict[str, Any],
    tool_map: dict[str, dict[str, Any]],
    *,
    selector: str,
    value: str,
    tab_id: int | None,
) -> dict[str, Any]:
    properties = bridge["_mcp_tool_schema_properties"](tool_map, "chrome_fill_or_select")
    selector_key = _pick_mcp_selector_arg_name(properties)
    value_key = _pick_mcp_value_arg_name(properties)
    args: dict[str, Any] = {}
    if tab_id is not None and ("tabId" in properties or not properties):
        args["tabId"] = tab_id
    if selector_key:
        args[selector_key] = selector
    if value_key:
        args[value_key] = value
    return args


def _build_mcp_click_args(
    bridge: dict[str, Any],
    tool_map: dict[str, dict[str, Any]],
    *,
    selector: str,
    tab_id: int | None,
) -> dict[str, Any]:
    properties = bridge["_mcp_tool_schema_properties"](tool_map, "chrome_click_element")
    selector_key = _pick_mcp_selector_arg_name(properties)
    args: dict[str, Any] = {}
    if tab_id is not None and ("tabId" in properties or not properties):
        args["tabId"] = tab_id
    if selector_key:
        args[selector_key] = selector
    return args


async def _read_mcp_selector_text(
    bridge: dict[str, Any],
    client: Any,
    tool_map: dict[str, dict[str, Any]],
    *,
    url: str,
    tab_id: int | None,
    selector: str,
) -> str:
    if "chrome_get_web_content" not in tool_map:
        return ""
    args = bridge["_build_get_web_content_args"](
        tool_map,
        url=url,
        tab_id=tab_id,
        preferred_format="text",
        selector=selector,
    )
    if not args:
        args = {"selector": selector}
        if tab_id is not None:
            args["tabId"] = tab_id
    try:
        result = await client.call_tool("chrome_get_web_content", args)
    except Exception:
        return ""
    return str(bridge["_extract_text_from_tool_result"](result) or "").strip()


def _looks_like_mcp_post_success_status(text: object) -> bool:
    normalized = re.sub(r"\s+", "", clean_html(str(text or "")))
    if not normalized:
        return False
    return normalized in {"ok", "OK"} or "完了" in normalized


async def _submit_img_thread_reply_via_mcp(
    url: str,
    comment: str,
    *,
    email: str = "",
    password: str = "",
    timeout_sec: float = 20.0,
    preserve_urls: object = None,
) -> dict[str, Any]:
    normalized_url = normalize_img_thread_url(url)
    post_comment = sanitize_img_post_submission_text(comment, max_chars=240, preserve_urls=preserve_urls)
    result: dict[str, Any] = {
        "success": False,
        "submitted": False,
        "url": normalized_url or str(url or "").strip(),
        "thread_no": "",
        "comment": post_comment,
        "status": 0,
        "error": "",
        "response_text": "",
    }

    if not normalized_url:
        result["error"] = "IMGスレッドURLではありません。"
        return result
    if not post_comment:
        result["error"] = "投稿本文が空です。"
        return result

    thread_no_match = _IMG_THREAD_URL_PATTERN.search(normalized_url)
    result["thread_no"] = thread_no_match.group(1) if thread_no_match else ""

    bridge = _load_img2chan_mcp_bridge()
    if bridge is None:
        result["error"] = "Chrome MCP bridge を利用できません。"
        return result

    navigate_result: dict[str, Any] | None = None
    tab_id: int | None = None
    tool_map: dict[str, dict[str, Any]] = {}
    client = bridge["MCPChromeHTTPClient"](
        timeout_sec=timeout_sec,
        allowed_tool_names=_IMG2CHAN_POST_MCP_ALLOWED_TOOLS,
        blocked_tool_keywords=_IMG2CHAN_POST_MCP_BLOCKED_TOOL_KEYWORDS,
    )

    try:
        async with bridge["_browser_operation_guard"]():
            tools = await client.list_tools()
            tool_map = bridge["_mcp_tool_map"](tools)
            missing_tools = [
                name for name in ("chrome_navigate", "chrome_fill_or_select", "chrome_click_element")
                if name not in tool_map
            ]
            if missing_tools:
                result["error"] = f"Chrome MCP に必要な投稿ツールがありません: {', '.join(missing_tools)}"
                return result

            navigate_result = await client.call_tool(
                "chrome_navigate",
                bridge["_build_navigate_args"](tool_map, normalized_url, prefer_new_window=True),
            )
            tab_id = bridge["_extract_tab_id"](navigate_result)
            await bridge["_sleep_after_browser_navigation"](normalized_url, reason="img2chan_post")

            await client.call_tool(
                "chrome_fill_or_select",
                _build_mcp_fill_args(
                    bridge,
                    tool_map,
                    selector=_IMG2CHAN_POST_TEXTAREA_SELECTOR,
                    value=post_comment,
                    tab_id=tab_id,
                ),
            )
            if email:
                await client.call_tool(
                    "chrome_fill_or_select",
                    _build_mcp_fill_args(
                        bridge,
                        tool_map,
                        selector=_IMG2CHAN_POST_EMAIL_SELECTOR,
                        value=str(email),
                        tab_id=tab_id,
                    ),
                )
            if password:
                await client.call_tool(
                    "chrome_fill_or_select",
                    _build_mcp_fill_args(
                        bridge,
                        tool_map,
                        selector=_IMG2CHAN_POST_PASSWORD_SELECTOR,
                        value=str(password),
                        tab_id=tab_id,
                    ),
                )

            await client.call_tool(
                "chrome_click_element",
                _build_mcp_click_args(
                    bridge,
                    tool_map,
                    selector=_IMG2CHAN_POST_SUBMIT_SELECTOR,
                    tab_id=tab_id,
                ),
            )
            result["submitted"] = True

            last_status_text = ""
            for attempt in range(4):
                await asyncio.sleep(0.8 if attempt == 0 else 1.2)
                status_text = await _read_mcp_selector_text(
                    bridge,
                    client,
                    tool_map,
                    url=normalized_url,
                    tab_id=tab_id,
                    selector=_IMG2CHAN_POST_STATUS_SELECTOR,
                )
                if _looks_like_mcp_post_success_status(status_text):
                    result["success"] = True
                    result["response_text"] = status_text
                    return result
                if status_text:
                    last_status_text = status_text

                thread = await fetch_img_thread(normalized_url, max_replies=12, timeout_sec=min(timeout_sec, 10.0))
                if thread and not str(thread.get("error") or "").strip():
                    matched = find_img_thread_reply_by_comment(thread, post_comment)
                    if matched is not None:
                        result["success"] = True
                        result["registered_post_no"] = str(matched.get("post_no") or "").strip()
                        if last_status_text:
                            result["response_text"] = last_status_text
                        return result

            result["response_text"] = last_status_text
            result["error"] = (
                _compact_error_text(last_status_text)
                if last_status_text
                else "Chrome MCP で送信後の成功確認ができませんでした。"
            )
            return result
    except asyncio.TimeoutError:
        result["error"] = "Chrome MCP 投稿がタイムアウトしました。"
        return result
    except Exception as e:
        log.warning("submit_img_thread_reply_via_mcp failed url=%s err=%r", normalized_url, e)
        result["error"] = str(e)
        return result
    finally:
        if bridge is not None and navigate_result is not None:
            should_close = False
            with contextlib.suppress(Exception):
                should_close = bool(bridge["_should_close_browser_tab_after_navigation"](navigate_result))
            if should_close:
                with contextlib.suppress(Exception):
                    await bridge["_close_browser_tab_if_possible"](
                        client,
                        tool_map,
                        tab_id,
                    )


async def _submit_img_thread_reply_via_http(
    url: str,
    comment: str,
    *,
    email: str = "",
    password: str = "",
    timeout_sec: float = 20.0,
    preserve_urls: object = None,
) -> dict[str, Any]:
    normalized_url = normalize_img_thread_url(url)
    post_comment = sanitize_img_post_submission_text(comment, max_chars=240, preserve_urls=preserve_urls)
    result: dict[str, Any] = {
        "success": False,
        "submitted": False,
        "url": normalized_url or str(url or "").strip(),
        "thread_no": "",
        "comment": post_comment,
        "status": 0,
        "error": "",
        "response_text": "",
    }

    if not normalized_url:
        result["error"] = "IMGスレッドURLではありません。"
        return result
    if not post_comment:
        result["error"] = "投稿本文が空です。"
        return result

    thread_no_match = _IMG_THREAD_URL_PATTERN.search(normalized_url)
    result["thread_no"] = thread_no_match.group(1) if thread_no_match else ""

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    headers = _default_headers(referer=normalized_url)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(normalized_url, headers=headers) as resp:
            result["status"] = int(resp.status)
            if resp.status != 200:
                result["error"] = f"フォーム取得に失敗しました: HTTP {resp.status}"
                return result
            html_text = await resp.text(encoding="Shift_JIS", errors="ignore")

        form = extract_img_reply_form(html_text)
        fields = dict(form.get("fields") or {})
        action = str(form.get("action") or "").strip()
        if not action or "mode" not in fields or "resto" not in fields:
            result["error"] = "返信フォームを見つけられませんでした。"
            return result

        cache_token = await _fetch_img_post_cache_token(session, referer=normalized_url)
        fields = _prepare_img_reply_form_fields(
            fields,
            thread_no=result["thread_no"],
            comment=post_comment,
            email=email,
            password=password,
            cache_token=cache_token,
        )

        data = aiohttp.FormData(charset="shift_jis", default_to_multipart=True)
        for key, value in fields.items():
            data.add_field(str(key), str(value))

        post_headers = {
            **_default_headers(referer=normalized_url),
            "Origin": "https://img.2chan.net",
            "Accept": "*/*",
        }
        post_url = urljoin(normalized_url, action)
        async with session.post(post_url, data=data, headers=post_headers) as resp:
            result["status"] = int(resp.status)
            response_text = await resp.text(encoding="Shift_JIS", errors="ignore")
            result["response_text"] = response_text
            result["submitted"] = True
            if resp.status != 200:
                result["error"] = f"投稿に失敗しました: HTTP {resp.status}"
                return result
            if _looks_like_successful_post_response(response_text):
                result["success"] = True
                return result
            result["error"] = _compact_error_text(response_text) or "投稿に失敗しました。"
            return result


async def submit_img_thread_reply(
    url: str,
    comment: str,
    *,
    email: str = "",
    password: str = "",
    timeout_sec: float = 20.0,
    preserve_urls: object = None,
) -> dict[str, Any]:
    """img.2chan.net のスレッド返信フォームへコメントを投稿する。

    まず mcp-chrome で実ブラウザ投稿を試し、送信前に失敗した場合だけ
    既存の direct HTTP 投稿へフォールバックする。
    """
    mcp_error = ""
    if _img2chan_post_mcp_enabled():
        mcp_result = await _submit_img_thread_reply_via_mcp(
            url,
            comment,
            email=email,
            password=password,
            timeout_sec=timeout_sec,
            preserve_urls=preserve_urls,
        )
        if bool(mcp_result.get("success")) or bool(mcp_result.get("submitted")):
            return mcp_result
        mcp_error = str(mcp_result.get("error") or "").strip()

    try:
        http_result = await _submit_img_thread_reply_via_http(
            url,
            comment,
            email=email,
            password=password,
            timeout_sec=timeout_sec,
            preserve_urls=preserve_urls,
        )
    except asyncio.TimeoutError:
        http_result = {
            "success": False,
            "submitted": False,
            "url": normalize_img_thread_url(url) or str(url or "").strip(),
            "thread_no": "",
            "comment": sanitize_img_post_submission_text(comment, max_chars=240, preserve_urls=preserve_urls),
            "status": 0,
            "error": "投稿がタイムアウトしました。",
            "response_text": "",
        }
    except Exception as e:
        log.warning("submit_img_thread_reply failed url=%s err=%r", normalize_img_thread_url(url), e)
        http_result = {
            "success": False,
            "submitted": False,
            "url": normalize_img_thread_url(url) or str(url or "").strip(),
            "thread_no": "",
            "comment": sanitize_img_post_submission_text(comment, max_chars=240, preserve_urls=preserve_urls),
            "status": 0,
            "error": str(e),
            "response_text": "",
        }

    if not bool(http_result.get("success")) and mcp_error:
        base_error = str(http_result.get("error") or "").strip()
        http_result["error"] = f"{base_error} / Chrome MCP: {mcp_error}" if base_error else mcp_error
    return http_result


def format_img_post_discord_report(post_result: dict[str, Any], *, delay_sec: float | None = None) -> str:
    """IMG投稿後にDiscordへ出す報告文を作る。"""
    result = dict(post_result or {})
    url = str(result.get("url") or "").strip()
    comment = sanitize_img_post_submission_text(result.get("comment") or "", max_chars=240)
    delay_note = ""
    if delay_sec is not None:
        minutes = max(int(round(float(delay_sec) / 60.0)), 1)
        delay_note = f"（{minutes}分くらい待ってから）"

    if bool(result.get("success")):
        action = "imgのスレに書き込んできた"
        if bool(result.get("dry_run")):
            action = "imgのスレに書き込む予定だった内容を作った"
        lines = [f"{action}{delay_note}。"]
        if url:
            lines.append(url)
        if comment:
            lines.append(f"書き込み: {comment}")
        return "\n".join(lines)

    error = str(result.get("error") or "原因不明").strip()
    lines = [f"imgのスレに書き込もうとしたけど失敗した{delay_note}。理由: {error}"]
    if url:
        lines.append(url)
    if comment:
        lines.append(f"書こうとした内容: {comment}")
    return "\n".join(lines)
