"""x.com タイムラインの Chrome MCP ブラウザ読み取り処理。

`harness.agent.tool_registry.web_research.dispatch` のパッチ可能属性を動的に参照し、
既存の `unittest.mock.patch` によるテストとの後方互換性を完全に保ちます。
"""
from __future__ import annotations

import asyncio
from typing import Any


def _dispatch_mod() -> Any:
    from harness.agent.tool_registry.web_research import dispatch

    return dispatch


def x_timeline_entries_from_tool_results(
    content_results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    d = _dispatch_mod()
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = max(int(limit or 1), 1)
    for result in content_results:
        raw_text = d._extract_raw_text_from_tool_result(result) or d._extract_text_from_tool_result(result)
        if not raw_text:
            continue
        for entry in d._xcom.parse_x_timeline_entries(raw_text, limit=max_items):
            handle = str(entry.get("handle") or "").strip()
            text = str(entry.get("text") or "").strip()
            identity = f"{handle.lower()}\n{text.lower()}"
            if not text or identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)
            if len(entries) >= max_items:
                return entries
    return entries


async def read_x_timeline_with_browser(
    client: Any,
    *,
    limit: int,
    target_url: str | None = None,
) -> list[dict[str, str]]:
    d = _dispatch_mod()
    url = str(target_url or getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home")
    selector = str(getattr(d._xcom, "X_TIMELINE_TWEET_SELECTOR", 'article[data-testid="tweet"]') or "").strip()
    if not await d._is_safe_public_browser_url_resolved(url):
        raise RuntimeError(f"Blocked unsafe browser target URL: {url}")

    async with d._browser_operation_guard():
        tools = await client.list_tools()
        tool_map = d._mcp_tool_map(tools)
        if "chrome_navigate" not in tool_map or "chrome_get_web_content" not in tool_map:
            raise RuntimeError("Required Chrome MCP tools are not available")

        navigate_result = await client.call_tool(
            "chrome_navigate",
            d._build_navigate_args(tool_map, url, prefer_new_window=True),
        )
        tab_id = d._extract_tab_id(navigate_result)
        await d._sleep_after_browser_navigation(url, reason="x_timeline")
        await d._refresh_x_timeline_tab_if_reused(
            client,
            tool_map,
            navigate_result=navigate_result,
            url=url,
            tab_id=tab_id,
        )
        content_results: list[dict[str, Any]] = []
        try:
            content_result = await d._read_browser_content_with_retries(
                client,
                tool_map,
                url=url,
                tab_id=tab_id,
                preferred_format="text",
                reject_login_wall=True,
                selector=selector,
            )
            content_results.append(content_result)
            selector_text = d._extract_raw_text_from_tool_result(content_result) or d._extract_text_from_tool_result(content_result)
            selector_entries = d._xcom.parse_x_timeline_entries(selector_text, limit=limit)
            if selector and len(selector_entries) < min(limit, 3):
                extra_wait_sec = d._x_timeline_extra_wait_sec()
                if extra_wait_sec > 0:
                    await asyncio.sleep(extra_wait_sec)
                content_results.append(
                    await d._read_browser_content_with_retries(
                        client,
                        tool_map,
                        url=url,
                        tab_id=tab_id,
                        preferred_format="text",
                        reject_login_wall=True,
                    )
                )
            combined_entries = d._x_timeline_entries_from_tool_results(content_results, limit=limit)
            if len(combined_entries) < min(limit, 3) and "chrome_read_page" in tool_map:
                content_results.append(
                    await client.call_tool(
                        "chrome_read_page",
                        d._build_read_page_args(tool_map, tab_id=tab_id),
                    )
                )
        finally:
            if d._should_close_browser_tab_after_navigation(navigate_result):
                await d._close_browser_tab_if_possible(client, tool_map, tab_id)

    raw_text = "\n\n".join(
        text
        for text in (
            d._extract_raw_text_from_tool_result(result) or d._extract_text_from_tool_result(result)
            for result in content_results
        )
        if text
    )
    if d._is_login_wall_or_error_page(raw_text):
        raise RuntimeError("Login wall or error page detected for X timeline")
    return d._x_timeline_entries_from_tool_results(content_results, limit=limit)


__all__ = [
    "read_x_timeline_with_browser",
    "x_timeline_entries_from_tool_results",
]
