"""
web_research/dispatch.py

このファイルは各リサーチモード（ウェブ検索、ブラウザ直接閲覧、Xタイムライン、IMGスレッド等）の
オーケストレーション（実行の制御）を行います。
主な責務:
- LLMによる検索プランニング・検索結果の要約
- 各モードごとの固有の処理とフォールバックハンドリング
- research_dispatch 関数の提供
- エラーハンドリングと最終結果の構成

AIエージェントが新しい調査機能を追加する場合はこのファイルを修正してください。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config_helpers import cfg, cfg_bool, cfg_int
from ..fact_check import extract_urls, build_link_summaries_from_text
from ..ollama_helpers import call_ollama_json, truncate_text
from .config import (
    ResearchDispatchMode,
    _BROWSER_QUERY_SCHEMA,
    _BROWSER_QUERY_SYSTEM_PROMPT,
    _BROWSER_SUMMARY_SCHEMA,
    _BROWSER_SUMMARY_SYSTEM_PROMPT,
    _DEFAULT_X_TIMELINE_HARD_LIMIT,
    _NO_RESULTS_TEXT,
    _safe_browser_page_limit,
    _safe_result_limit,
    _safe_x_timeline_limit,
    _x_timeline_extra_wait_sec,
)
from .helpers import (
    _choose_query,
    _compact_whitespace,
    _is_login_wall_or_error_page_fallback,
    _is_safe_public_browser_url_resolved,
    _query_terms,
    _remember_known_terms_from_text,
    detect_reply_research_rules,
    resolve_research_mode,
)
from .parsing import (
    _build_result,
    _build_source,
    _dedupe_sources,
    _extract_browser_page,
    _extract_raw_text_from_tool_result,
    _extract_tab_id,
    _extract_text_from_tool_result,
    _format_sources_summary,
    _normalize_search_source,
    _iter_tool_result_payloads,
    _extract_search_entries_from_tool_result,
    _build_read_page_args,
)
from .client import (
    MCPChromeHTTPClient,
    _browser_operation_guard,
    _browser_debug_enabled,
    _build_navigate_args,
    _close_browser_tab_if_possible,
    _get_shared_mcp_client,
    _is_invalid_mcp_session_error,
    _is_mcp_transport_already_connected_error,
    _mcp_tool_map,
    _read_browser_content_with_retries,
    _read_search_results_with_browser,
    _refresh_x_timeline_tab_if_reused,
    _should_close_browser_tab_after_navigation,
    _sleep_after_browser_navigation,
    _browser_result_preview,
)

# 実行時に動的にインポートされるヘルパーモジュール
_xcom: Any = None
_img_helper: Any = None

log = logging.getLogger("ollama_bot.common.web_research")

def _init_helpers(xcom: Any, img_helper: Any) -> None:
    global _xcom, _img_helper
    _xcom = xcom
    _img_helper = img_helper


_MCP_CHROME_UNAVAILABLE_ERROR = (
    "mcp-chrome が起動していないか、拡張機能が未接続です（127.0.0.1:12306 に接続不可）。"
)

def _is_login_wall_or_error_page(content_text: str) -> bool:
    if _xcom and hasattr(_xcom, "is_login_wall_or_error_page"):
        return bool(_xcom.is_login_wall_or_error_page(content_text))
    return _is_login_wall_or_error_page_fallback(content_text)

def _browser_dispatch_error_message(error_text: str) -> str:
    compacted = _compact_whitespace(error_text)
    lowered = compacted.lower()
    if (
        "connectionrefused" in lowered
        or "connection refused" in lowered
        or "connect call failed" in lowered
    ) and ("127.0.0.1" in lowered or "12306" in lowered):
        return _MCP_CHROME_UNAVAILABLE_ERROR
    if "cannot connect to host" in lowered and ("127.0.0.1" in lowered or "12306" in lowered):
        return _MCP_CHROME_UNAVAILABLE_ERROR
    if _is_invalid_mcp_session_error(lowered):
        return "mcp-chrome のセッションが切れたため、ブラウザ調査に失敗しました。"
    if _is_mcp_transport_already_connected_error(lowered):
        return "mcp-chrome が別セッションに接続済みのため、ブラウザ調査に失敗しました。"
    return "ブラウザ調査に失敗しました。"


async def _search_web_entries_with_mcp(query: str, *, max_results: int) -> list[dict[str, str]]:
    client = _get_shared_mcp_client()
    result = await _read_search_results_with_browser(client, query, max_results=max_results)
    return _extract_search_entries_from_tool_result(result, max_results=max_results)


async def _read_url_with_browser(
    client: MCPChromeHTTPClient,
    url: str,
    *,
    source: dict[str, Any] | None = None,
    query: str = "",
) -> dict[str, Any]:
    if not await _is_safe_public_browser_url_resolved(url):
        raise RuntimeError(f"Blocked unsafe browser target URL: {url}")

    async with _browser_operation_guard():
        tools = await client.list_tools()
        tool_map = _mcp_tool_map(tools)
        if "chrome_navigate" not in tool_map or "chrome_get_web_content" not in tool_map:
            raise RuntimeError("Required Chrome MCP tools are not available")

        navigate_result = await client.call_tool(
            "chrome_navigate",
            _build_navigate_args(tool_map, url, prefer_new_window=True),
        )
        tab_id = _extract_tab_id(navigate_result)
        await _sleep_after_browser_navigation(url, reason="read_url")
        try:
            content_result = await _read_browser_content_with_retries(
                client,
                tool_map,
                url=url,
                tab_id=tab_id,
                preferred_format="text",
                reject_login_wall=True,
            )
        finally:
            if _should_close_browser_tab_after_navigation(navigate_result):
                await _close_browser_tab_if_possible(client, tool_map, tab_id)

    page = _extract_browser_page(url, content_result, query=query)
    content_text = page.get("content_text") or ""
    if _is_login_wall_or_error_page(content_text):
        log.warning(
            "browser read produced login-wall/error content url=%s page_url=%s title=%r text_len=%s preview=%r",
            url,
            page.get("url") or "",
            page.get("title") or "",
            len(content_text),
            _browser_result_preview(content_text, fallback_chars=220) if _browser_debug_enabled() else "",
        )
        raise RuntimeError(f"Login wall or error page detected for URL: {url}")
    merged = _build_source(
        source_type="browser",
        title=page.get("title") or str((source or {}).get("title") or "").strip(),
        url=page.get("url") or url,
        snippet=str((source or {}).get("snippet") or "").strip(),
        content_text=page.get("content_text") or "",
        content_excerpt=page.get("content_excerpt") or "",
        used_in_answer=True,
    )
    return merged


def _browser_source_quality(source: dict[str, Any], query: str) -> int:
    score = 0
    excerpt = str(source.get("content_excerpt") or "").strip()
    content_text = str(source.get("content_text") or "").strip()
    title = str(source.get("title") or "").strip()
    score += min(len(excerpt) // 120, 4)
    score += min(len(content_text) // 400, 4)
    lowered = f"{title} {excerpt}".lower()
    for term in _query_terms(query):
        if term.lower() in lowered:
            score += 2
    return score


def _build_browser_observation_prompt(query: str, context: str, sources: list[dict[str, Any]]) -> str:
    source_blocks: list[str] = []
    for idx, source in enumerate(sources, start=1):
        source_blocks.append(
            f"[{idx}] タイトル: {source.get('title') or '(なし)'}\n"
            f"URL: {source.get('url') or '(なし)'}\n"
            f"検索抜粋: {truncate_text(str(source.get('snippet') or ''), 240)}\n"
            f"本文観測: {truncate_text(str(source.get('content_text') or source.get('content_excerpt') or ''), 1800)}"
        )
    return (
        "以下はブラウザで観測したWeb調査メモです。"
        "後段の会話モデルへ渡すため、事実メモとして要点だけを日本語で短くまとめてください。\n"
        "観測が足りない場合は insufficient=true にし、その理由を insufficiency_reason に書いてください。\n\n"
        "【検索クエリ】\n"
        f"{query or '(なし)'}\n\n"
        "【補助文脈】\n"
        f"{truncate_text(_compact_whitespace(context), 500) or '(なし)'}\n\n"
        "【観測メモ】\n"
        f"{chr(10).join(source_blocks) if source_blocks else '(なし)'}"
    )


async def _generate_browser_search_plan(user_text: str, context: str, *, max_results: int) -> dict[str, Any]:
    prompt = f"ユーザー質問:\n{user_text}\n\n補助文脈:\n{context}\n\n検索計画を立ててください。max_results={max_results}です。"
    try:
        return await call_ollama_json(
            prompt,
            system_prompt=_BROWSER_QUERY_SYSTEM_PROMPT,
            model=str(cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
            think=False,
            schema=_BROWSER_QUERY_SCHEMA,
            timeout_sec=float(cfg("WEB_RESEARCH_PLAN_TIMEOUT_SEC", 8.0) or 8.0),
            retries=1,
            temperature=0.1,
            num_predict=128,
        )
    except Exception as e:
        log.warning("browser search plan generation failed; using fallback: %r", e)
        return {"query": _choose_query(user_text, context), "open_top_n": 3, "recency_priority": False}


async def _summarize_browser_observations(
    query: str,
    context: str,
    sources: list[dict[str, Any]],
) -> tuple[str, float, str]:
    if not sources:
        return _NO_RESULTS_TEXT, 0.0, ""

    try:
        result = await call_ollama_json(
            _build_browser_observation_prompt(query, context, sources),
            system_prompt=_BROWSER_SUMMARY_SYSTEM_PROMPT,
            model=str(cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
            think=False,
            schema=_BROWSER_SUMMARY_SCHEMA,
            timeout_sec=float(cfg("WEB_RESEARCH_SUMMARY_TIMEOUT_SEC", 12.0) or 12.0),
            retries=0,
            temperature=0.1,
            num_predict=max(cfg_int("WEB_RESEARCH_SUMMARY_NUM_PREDICT", 256), 128),
        )
        summary = str(result.get("summary") or "").strip()
        confidence = max(0.0, min(float(result.get("confidence") or 0.0), 1.0))
        insufficiency_reason = str(result.get("insufficiency_reason") or "").strip()
        if bool(result.get("insufficient")) and insufficiency_reason:
            summary = f"{summary}\n不足: {insufficiency_reason}".strip()
        if summary:
            return summary, confidence, insufficiency_reason
    except Exception as e:
        log.warning("browser observation summarization failed; using fallback summary: %r", e)

    return _format_sources_summary(sources, no_results_text=_NO_RESULTS_TEXT), 0.78 if sources else 0.0, ""


async def _orchestrate_browser_search(
    *,
    user_text: str,
    context: str,
    max_results: int,
) -> dict[str, Any]:
    plan = await _generate_browser_search_plan(user_text, context, max_results=max_results)
    query = str(plan.get("query") or "").strip()
    if not query:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="検索クエリを作れませんでした。",
            confidence=0.0,
            mode="browser_search",
            error="検索クエリを作れませんでした。",
        )

    search_candidate_limit = _safe_result_limit(max(max_results, cfg_int("WEB_RESEARCH_BROWSER_MAX_RESULTS_TO_OPEN", 5)))
    simple_result = await _simple_search_dispatch(query, max_results=search_candidate_limit)
    if simple_result.get("error"):
        return _build_result(
            used_web=bool(simple_result.get("used_web")),
            used_browser=bool(simple_result.get("used_browser")),
            summary=str(simple_result.get("summary") or ""),
            sources=list(simple_result.get("sources") or []),
            confidence=float(simple_result.get("confidence") or 0.0),
            mode="browser_search",
            query=query,
            error=str(simple_result.get("error") or ""),
        )

    candidate_sources = list(simple_result.get("sources") or [])
    if not candidate_sources:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=_NO_RESULTS_TEXT,
            confidence=0.0,
            mode="browser_search",
            query=query,
        )

    if not cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return _build_result(
            used_web=True,
            used_browser=bool(simple_result.get("used_browser")),
            summary=_format_sources_summary(candidate_sources, no_results_text=_NO_RESULTS_TEXT),
            sources=candidate_sources,
            confidence=float(simple_result.get("confidence") or 0.0),
            mode="browser_search",
            query=query,
        )

    client = _get_shared_mcp_client()
    initial_limit = max(min(int(plan.get("open_top_n") or 3), len(candidate_sources)), 1)
    max_open_limit = max(
        initial_limit,
        min(_safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_RESULTS_TO_OPEN", 5)), len(candidate_sources)),
    )
    browser_sources: list[dict[str, Any]] = []
    total_observed_chars = 0

    async def _observe_range(start: int, end: int) -> None:
        nonlocal total_observed_chars
        for source in candidate_sources[start:end]:
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            try:
                observed = await _read_url_with_browser(client, url, source=source, query=query)
                observed = _build_source(
                    source_type=str(observed.get("source_type") or "browser"),
                    title=str(observed.get("title") or source.get("title") or "").strip(),
                    url=str(observed.get("url") or url).strip(),
                    snippet=str(observed.get("snippet") or source.get("snippet") or "").strip(),
                    content_text=str(observed.get("content_text") or "").strip(),
                    content_excerpt=str(observed.get("content_excerpt") or "").strip(),
                    used_in_answer=True,
                )
                observed["quality"] = _browser_source_quality(observed, query)
                browser_sources.append(observed)
                total_observed_chars += len(str(observed.get("content_text") or observed.get("content_excerpt") or ""))
            except Exception as e:
                log.warning("browser orchestrator read failed url=%s err=%r", url, e)

    await _observe_range(0, initial_limit)

    should_second_pass = cfg_bool("WEB_RESEARCH_BROWSER_SECOND_PASS_ENABLED", True) and (
        len(browser_sources) < 2
        or total_observed_chars < max(cfg_int("WEB_RESEARCH_BROWSER_SECOND_PASS_MIN_TOTAL_CHARS", 1400), 400)
    )
    if should_second_pass and initial_limit < max_open_limit:
        await _observe_range(initial_limit, max_open_limit)

    if browser_sources:
        candidate_term = str(((plan.get("rule_info") or {}) if isinstance(plan.get("rule_info"), dict) else {}).get("candidate_term") or "").strip()
        _remember_known_terms_from_text(candidate_term or query)
        browser_sources.sort(key=lambda item: int(item.get("quality") or 0), reverse=True)
        summary, summary_confidence, insufficiency_reason = await _summarize_browser_observations(
            query,
            context,
            browser_sources[:max_open_limit],
        )
        merged_sources = _dedupe_sources(browser_sources[:max_open_limit] + candidate_sources)
        result = _build_result(
            used_web=True,
            used_browser=True,
            summary=summary,
            sources=merged_sources,
            confidence=max(summary_confidence, 0.78 if browser_sources else 0.0),
            mode="browser_search",
            query=query,
        )
        result["plan"] = {
            "query": query,
            "initial_limit": initial_limit,
            "max_open_limit": max_open_limit,
            "second_pass_used": bool(should_second_pass and initial_limit < max_open_limit),
        }
        result["insufficiency_reason"] = insufficiency_reason
        return result

    fallback_summary = _format_sources_summary(candidate_sources, no_results_text=_NO_RESULTS_TEXT)
    if context:
        fallback_summary = fallback_summary or truncate_text(_compact_whitespace(context), 500)
    return _build_result(
        used_web=True,
        used_browser=True,
        summary=fallback_summary,
        sources=candidate_sources,
        confidence=float(simple_result.get("confidence") or 0.0),
        mode="browser_search",
        query=query,
    )


async def _simple_search_dispatch(query: str, *, max_results: int) -> dict[str, Any]:
    max_results = _safe_result_limit(max_results)
    try:
        entries = await _search_web_entries_with_mcp(query, max_results=max_results)
    except TimeoutError:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="検索がタイムアウトしました。",
            confidence=0.0,
            mode="simple_search",
            query=query,
            error="Chrome MCP検索がタイムアウトしました。少し待ってからもう一度試してください。",
        )
    except Exception as e:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=f"検索中にエラーが発生しました: {e}",
            confidence=0.0,
            mode="simple_search",
            query=query,
            error=f"Chrome MCP検索中にエラーが発生しました。少し待ってからもう一度試してください。詳細: {e}",
        )

    sources = [_normalize_search_source(entry) for entry in entries]
    if sources:
        _remember_known_terms_from_text(query)
    summary = _format_sources_summary(sources, no_results_text=_NO_RESULTS_TEXT)
    confidence = 0.55 if sources else 0.0
    return _build_result(
        used_web=bool(sources),
        used_browser=bool(sources),
        summary=summary,
        sources=sources,
        confidence=confidence,
        mode="simple_search",
        query=query,
    )


async def _browser_search_dispatch(query: str, context: str, *, max_results: int) -> dict[str, Any]:
    return await _orchestrate_browser_search(
        user_text=query,
        context=context,
        max_results=max_results,
    )


def _x_timeline_entries_from_tool_results(
    content_results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = max(int(limit or 1), 1)
    for result in content_results:
        raw_text = _extract_raw_text_from_tool_result(result) or _extract_text_from_tool_result(result)
        if not raw_text:
            continue
        for entry in _xcom.parse_x_timeline_entries(raw_text, limit=max_items):
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


async def _read_x_timeline_with_browser(client: MCPChromeHTTPClient, *, limit: int) -> list[dict[str, str]]:
    url = str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home")
    selector = str(getattr(_xcom, "X_TIMELINE_TWEET_SELECTOR", 'article[data-testid="tweet"]') or "").strip()
    if not await _is_safe_public_browser_url_resolved(url):
        raise RuntimeError(f"Blocked unsafe browser target URL: {url}")

    async with _browser_operation_guard():
        tools = await client.list_tools()
        tool_map = _mcp_tool_map(tools)
        if "chrome_navigate" not in tool_map or "chrome_get_web_content" not in tool_map:
            raise RuntimeError("Required Chrome MCP tools are not available")

        navigate_result = await client.call_tool(
            "chrome_navigate",
            _build_navigate_args(tool_map, url, prefer_new_window=True),
        )
        tab_id = _extract_tab_id(navigate_result)
        await _sleep_after_browser_navigation(url, reason="x_timeline")
        await _refresh_x_timeline_tab_if_reused(
            client,
            tool_map,
            navigate_result=navigate_result,
            url=url,
            tab_id=tab_id,
        )
        content_results: list[dict[str, Any]] = []
        try:
            content_result = await _read_browser_content_with_retries(
                client,
                tool_map,
                url=url,
                tab_id=tab_id,
                preferred_format="text",
                reject_login_wall=True,
                selector=selector,
            )
            content_results.append(content_result)
            selector_text = _extract_raw_text_from_tool_result(content_result) or _extract_text_from_tool_result(content_result)
            selector_entries = _xcom.parse_x_timeline_entries(selector_text, limit=limit)
            if selector and len(selector_entries) < min(limit, 3):
                extra_wait_sec = _x_timeline_extra_wait_sec()
                if extra_wait_sec > 0:
                    await asyncio.sleep(extra_wait_sec)
                content_results.append(
                    await _read_browser_content_with_retries(
                        client,
                        tool_map,
                        url=url,
                        tab_id=tab_id,
                        preferred_format="text",
                        reject_login_wall=True,
                    )
                )
            combined_entries = _x_timeline_entries_from_tool_results(content_results, limit=limit)
            if len(combined_entries) < min(limit, 3) and "chrome_read_page" in tool_map:
                content_results.append(
                    await client.call_tool(
                        "chrome_read_page",
                        _build_read_page_args(tool_map, tab_id=tab_id),
                    )
                )
        finally:
            if _should_close_browser_tab_after_navigation(navigate_result):
                await _close_browser_tab_if_possible(client, tool_map, tab_id)

    raw_text = "\n\n".join(
        text
        for text in (
            _extract_raw_text_from_tool_result(result) or _extract_text_from_tool_result(result)
            for result in content_results
        )
        if text
    )
    if _is_login_wall_or_error_page(raw_text):
        raise RuntimeError("Login wall or error page detected for X timeline")
    return _x_timeline_entries_from_tool_results(content_results, limit=limit)


async def _x_timeline_dispatch(user_text: str, context: str, *, limit: int) -> dict[str, Any]:
    if not cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="Xタイムライン確認にはブラウザ調査が無効です。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error="ブラウザ調査が無効です。",
        )

    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()
    default_limit = _safe_x_timeline_limit(limit)
    requested_limit = _safe_x_timeline_limit(
        _xcom.extract_x_timeline_limit(
            combined,
            default=default_limit,
            max_limit=cfg_int("WEB_RESEARCH_X_TIMELINE_MAX_POSTS", _DEFAULT_X_TIMELINE_HARD_LIMIT),
        )
    )

    try:
        entries = await _read_x_timeline_with_browser(_get_shared_mcp_client(), limit=requested_limit)
    except Exception as e:
        log.warning("x timeline dispatch failed: %r", e)
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="Xタイムラインの取得に失敗しました。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error=_browser_dispatch_error_message(str(e)),
        )

    if not entries:
        return _build_result(
            used_web=False,
            used_browser=True,
            summary="Xホームタイムラインの投稿を抽出できませんでした。ログイン状態や表示状態を確認してください。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error="Xタイムラインの投稿を抽出できませんでした。",
        )

    sources = [
        _build_source(
            source_type="browser",
            title=str(entry.get("handle") or "").strip(),
            url=str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            snippet=str(entry.get("text") or "").strip(),
            content_excerpt=str(entry.get("text") or "").strip(),
            content_text=str(entry.get("raw") or entry.get("text") or "").strip(),
            used_in_answer=True,
            source_backend="x_timeline",
            tweet_index=index,
            author=str(entry.get("author") or "").strip(),
            handle=str(entry.get("handle") or "").strip(),
        )
        for index, entry in enumerate(entries[:requested_limit], start=1)
    ]
    return _build_result(
        used_web=True,
        used_browser=True,
        summary=_xcom.format_x_timeline_summary(entries, limit=requested_limit),
        sources=sources,
        confidence=0.78,
        mode="x_timeline",
        query=str(getattr(_xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
    )


async def _browser_read_url_dispatch(user_text: str, context: str) -> dict[str, Any]:
    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()
    user_urls = extract_urls(user_text)
    urls = user_urls or extract_urls(combined)
    fallback_text = str(user_text or "").strip() if user_urls else combined
    fallback_query = _choose_query(user_text, context)
    if not urls:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="URLが見つかりませんでした。",
            confidence=0.0,
            mode="browser_read_url",
            error="調査対象のURLが見つかりませんでした。",
        )

    try:
        fallback_summaries = await build_link_summaries_from_text(
            fallback_text,
            max_links=_safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES", 2)),
        )
    except Exception as e:
        log.warning("link summary fallback failed urls=%s err=%r", urls, e)
        fallback_summaries = []
    fallback_sources = [
        _build_source(
            source_type="link_summary",
            title="",
            url="",
            snippet=str(item or "").strip(),
            content_excerpt="",
            used_in_answer=False,
        )
        for item in fallback_summaries
        if str(item or "").strip()
    ]
    try:
        search_fallbacks = await _search_web_entries_with_mcp(fallback_query, max_results=5)
        for s in search_fallbacks:
            fallback_sources.append(
                _build_source(
                    source_type="search_fallback",
                    title=str(s.get("title") or "").strip(),
                    url=str(s.get("href") or "").strip(),
                    snippet=str(s.get("body") or "").strip(),
                )
            )
    except Exception as e:
        log.debug("browser read fallback search failed: %r", e)

    fallback_sources = [
        source
        for source in fallback_sources
        if not _is_login_wall_or_error_page(str(source.get("snippet") or ""))
    ]

    if not cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return _build_result(
            used_web=bool(fallback_sources),
            used_browser=False,
            summary=_format_sources_summary(fallback_sources, no_results_text=_NO_RESULTS_TEXT) if fallback_sources else "URLの読み取りに失敗しました。",
            sources=fallback_sources,
            confidence=0.5 if fallback_sources else 0.0,
            mode="browser_read_url",
            query=urls[0],
        )

    browser_sources: list[dict[str, Any]] = []
    client = _get_shared_mcp_client()
    browser_limit = _safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES", 2))
    last_browser_error = ""

    for url in urls[:browser_limit]:
        try:
            observed = await _read_url_with_browser(client, url, query=fallback_query or url)
            browser_sources.append(
                _build_source(
                    source_type=str(observed.get("source_type") or "browser"),
                    title=str(observed.get("title") or "").strip(),
                    url=str(observed.get("url") or url).strip(),
                    snippet=str(observed.get("snippet") or "").strip(),
                    content_text=str(observed.get("content_text") or "").strip(),
                    content_excerpt=str(observed.get("content_excerpt") or "").strip(),
                    used_in_answer=bool(observed.get("used_in_answer", True)),
                    fetched_at=str(observed.get("fetched_at") or "").strip(),
                )
            )
        except Exception as e:
            err_msg = str(e)
            if "Login wall" in err_msg or "error page detected" in err_msg:
                log.info("browser_read_url skipped login-wall url=%s", url)
            else:
                last_browser_error = err_msg
                log.warning("browser_read_url failed url=%s err=%r", url, e)

    if browser_sources:
        merged_sources = _dedupe_sources(browser_sources + fallback_sources)
        return _build_result(
            used_web=True,
            used_browser=True,
            summary=_format_sources_summary(merged_sources, no_results_text=_NO_RESULTS_TEXT),
            sources=merged_sources,
            confidence=0.82,
            mode="browser_read_url",
            query=urls[0],
        )

    return _build_result(
        used_web=bool(fallback_sources),
        used_browser=False,
        summary=_format_sources_summary(fallback_sources, no_results_text=_NO_RESULTS_TEXT) if fallback_sources else "URLの読み取りに失敗しました。",
        sources=fallback_sources,
        confidence=0.5 if fallback_sources else 0.0,
        mode="browser_read_url",
        query=urls[0],
        error="" if fallback_sources else _browser_dispatch_error_message(last_browser_error),
    )


async def _img_top5_dispatch(user_text: str, context: str = "") -> dict[str, Any]:
    try:
        if not hasattr(_img_helper, "fetch_img_top5_threads"):
            raise ValueError("img2channet.py is not properly loaded or fetch_img_top5_threads not found.")
        threads = await getattr(_img_helper, "fetch_img_top5_threads")(limit=5)
    except Exception as e:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="IMGカタログの取得に失敗しました。",
            confidence=0.0,
            mode="img_top5",
            error=str(e),
        )

    summary = ""
    if hasattr(_img_helper, "format_img_top5_summary"):
        summary = getattr(_img_helper, "format_img_top5_summary")(threads)

    sources = [
        _build_source(
            source_type="browser",
            title=f"IMG勢い上位{index}位",
            url=str(thread.get("url") or "").strip(),
            snippet=truncate_text(str(thread.get("text") or "").strip(), 240),
            content_excerpt=truncate_text(str(thread.get("text") or "").strip(), 500),
            content_text=str(thread.get("text") or "").strip(),
            used_in_answer=True,
            source_backend="img_top5",
            rank=index,
            res_count=int(thread.get("res_count") or 0),
        )
        for index, thread in enumerate(list(threads or [])[:5], start=1)
    ]
    result = _build_result(
        used_web=True,
        used_browser=True,
        summary=summary,
        sources=sources,
        confidence=0.8 if threads else 0.0,
        mode="img_top5",
        query="https://img.2chan.net/b/futaba.php?mode=cat&sort=6",
    )
    result["threads"] = list(threads or [])[:5]
    return result


async def _img_thread_dispatch(user_text: str, context: str = "") -> dict[str, Any]:
    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()

    if not hasattr(_img_helper, "_extract_img_thread_urls_full"):
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッド取得機能が利用できません。",
            confidence=0.0,
            mode="img_thread",
            error="img2channet._extract_img_thread_urls_full が見つかりません。",
        )

    thread_urls: list[str] = getattr(_img_helper, "_extract_img_thread_urls_full")(combined)
    if not thread_urls:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッドのURLが見つかりませんでした。",
            confidence=0.0,
            mode="img_thread",
            error="img.2chan.net スレッドURLが含まれていません。",
        )

    target_url = thread_urls[0]
    max_replies = int(cfg("IMG_THREAD_MAX_REPLIES", 30) or 30)
    timeout_sec = float(cfg("IMG_THREAD_TIMEOUT_SEC", 15.0) or 15.0)

    try:
        thread = await getattr(_img_helper, "fetch_img_thread")(
            target_url, max_replies=max_replies, timeout_sec=timeout_sec
        )
    except Exception as e:
        log.warning("img_thread_dispatch fetch failed url=%s err=%r", target_url, e)
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッドの取得中にエラーが発生しました。",
            confidence=0.0,
            mode="img_thread",
            query=target_url,
            error=str(e),
        )

    error = str(thread.get("error") or "").strip()
    if error:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=f"IMGスレッドの取得に失敗しました: {error}",
            confidence=0.0,
            mode="img_thread",
            query=target_url,
            error=error,
        )

    summary = ""
    summary_max_replies = max(1, min(max_replies, cfg_int("IMG_THREAD_SUMMARY_MAX_REPLIES", 18)))
    if hasattr(_img_helper, "format_img_thread_summary"):
        summary = getattr(_img_helper, "format_img_thread_summary")(thread, max_replies=summary_max_replies)
    else:
        op_text = str(thread.get("op_text") or "").strip()
        summary = f"【スレッド】{target_url}\nOP: {op_text[:400]}"
    summary = truncate_text(summary, max(cfg_int("IMG_THREAD_SUMMARY_MAX_CHARS", 2400), 600))

    log_formatter = getattr(_img_helper, "format_img_thread_log_summary", None)
    if callable(log_formatter):
        log.info(
            "img_thread_dispatch parsed thread\n%s",
            log_formatter(
                thread,
                max_replies=max(cfg_int("IMG_THREAD_LOG_MAX_REPLIES", 5), 0),
                max_chars=max(cfg_int("IMG_THREAD_LOG_MAX_CHARS", 1400), 300),
            ),
        )
    else:
        log.info(
            "img_thread_dispatch parsed url=%s thread_no=%s replies=%s fetched=%s op=%r",
            target_url,
            str(thread.get("thread_no") or ""),
            int(thread.get("total_reply_count") or 0),
            int(thread.get("fetched_reply_count") or 0),
            truncate_text(str(thread.get("op_text") or "").strip(), 160),
        )

    source = _build_source(
        source_type="browser",
        title=f"IMGスレッド {thread.get('thread_no', '')}",
        url=target_url,
        snippet=str(thread.get("op_text") or "")[:200],
        content_text=summary,
        content_excerpt=summary[:500],
        used_in_answer=True,
        source_backend="img_thread",
        thread_no=str(thread.get("thread_no") or ""),
    )
    result = _build_result(
        used_web=True,
        used_browser=True,
        summary=summary,
        sources=[source],
        confidence=0.85 if not error else 0.0,
        mode="img_thread",
        query=target_url,
    )
    result["thread"] = thread
    return result





async def research_dispatch(
    user_text: str,
    context: str = "",
    mode: ResearchDispatchMode = "auto",
    *,
    max_results: int | None = None,
) -> dict[str, Any]:
    normalized_user_text = str(user_text or "").strip()
    normalized_context = str(context or "").strip()
    query = _choose_query(normalized_user_text, normalized_context)
    resolved_mode = resolve_research_mode(
        normalized_user_text,
        context=normalized_context,
        mode=mode,
    )
    resolved_max_results = _safe_result_limit(
        max(int(max_results if max_results is not None else cfg_int("WEB_RESEARCH_MAX_RESULTS", 4)), 1)
    )
    resolved_x_timeline_limit = _safe_x_timeline_limit()

    if not query and resolved_mode not in {"browser_read_url", "x_timeline", "img_top5", "img_thread"}:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="検索クエリを作れませんでした。",
            confidence=0.0,
            mode=resolved_mode,
            error="検索クエリを作れませんでした。",
        )

    if resolved_mode == "browser_read_url":
        return await _browser_read_url_dispatch(normalized_user_text, normalized_context)
    if resolved_mode == "img_top5":
        return await _img_top5_dispatch(normalized_user_text, normalized_context)
    if resolved_mode == "img_thread":
        return await _img_thread_dispatch(normalized_user_text, normalized_context)
    if resolved_mode == "x_timeline":
        return await _x_timeline_dispatch(
            normalized_user_text,
            normalized_context,
            limit=resolved_x_timeline_limit,
        )
    if resolved_mode == "browser_search":
        return await _browser_search_dispatch(query, normalized_context, max_results=resolved_max_results)
    return await _simple_search_dispatch(query, max_results=resolved_max_results)


def get_ollama_web_search_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "インターネットを検索して最新の情報や不明な単語の意味を調べます。わからないキーワードがあればまずこれを使ってください。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "検索キーワード（例: '最新のAIニュース', 'Python MCPとは'）"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]
