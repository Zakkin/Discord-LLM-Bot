"""x.com タイムライン取得・検索のディスパッチ処理。"""
from __future__ import annotations

import asyncio
from typing import Any


def _dispatch_mod() -> Any:
    from harness.agent.tool_registry.web_research import dispatch

    return dispatch


async def x_timeline_dispatch(user_text: str, context: str, *, limit: int) -> dict[str, Any]:
    d = _dispatch_mod()
    if not d.cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="Xタイムライン確認にはブラウザ調査が無効です。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error="ブラウザ調査が無効です。",
        )

    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()
    default_limit = d._safe_x_timeline_limit(limit)
    requested_limit = d._safe_x_timeline_limit(
        d._xcom.extract_x_timeline_limit(
            combined,
            default=default_limit,
            max_limit=d.cfg_int("WEB_RESEARCH_X_TIMELINE_MAX_POSTS", d._DEFAULT_X_TIMELINE_HARD_LIMIT),
        )
    )

    x_timeout = float(d.cfg("WEB_RESEARCH_X_TIMELINE_TIMEOUT_SEC", 35.0) or 35.0)
    try:
        entries = await asyncio.wait_for(
            d._read_x_timeline_with_browser(d._get_shared_mcp_client(), limit=requested_limit),
            timeout=x_timeout,
        )
    except (asyncio.TimeoutError, TimeoutError) as e:
        d.log.warning("x timeline dispatch timed out (%.1fs): %r", x_timeout, e)
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="Xタイムラインの取得がタイムアウトしました。時間を置いて再度お試しください。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error="Xタイムラインの取得がタイムアウトしました。",
        )
    except Exception as e:
        d.log.warning("x timeline dispatch failed: %r", e)
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="Xタイムラインの取得に失敗しました。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error=d._browser_dispatch_error_message(str(e)),
        )

    if not entries:
        return d._build_result(
            used_web=False,
            used_browser=True,
            summary="Xホームタイムラインの投稿を抽出できませんでした。ログイン状態や表示状態を確認してください。",
            confidence=0.0,
            mode="x_timeline",
            query=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
            error="Xタイムラインの投稿を抽出できませんでした。",
        )

    sources = [
        d._build_source(
            source_type="browser",
            title=str(entry.get("handle") or "").strip(),
            url=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
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
    return d._build_result(
        used_web=True,
        used_browser=True,
        summary=d._xcom.format_x_timeline_summary(entries, limit=requested_limit),
        sources=sources,
        confidence=0.78,
        mode="x_timeline",
        query=str(getattr(d._xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home"),
    )


async def x_search_dispatch(
    query: str,
    context: str,
    *,
    limit: int,
    max_results: int = 4,
) -> dict[str, Any]:
    d = _dispatch_mod()
    build_fn = getattr(d._xcom, "build_x_search_url", None)
    search_url = build_fn(query) if callable(build_fn) else f"https://x.com/search?q={query}&f=live"

    x_search_timeout = float(d.cfg("WEB_RESEARCH_X_SEARCH_TIMEOUT_SEC", 30.0) or 30.0)
    entries: list[dict[str, str]] = []
    if d.cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        try:
            client = d._get_shared_mcp_client()
            entries = await asyncio.wait_for(
                d._read_x_timeline_with_browser(client, limit=limit, target_url=search_url),
                timeout=x_search_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as e:
            d.log.warning("x search dispatch timed out for query=%r (%.1fs): %r, falling back to browser_search", query, x_search_timeout, e)
        except Exception as e:
            d.log.warning("x search dispatch failed for query=%r: %r, falling back to browser_search", query, e)

    if entries:
        format_fn = getattr(d._xcom, "format_x_search_summary", None)
        summary = format_fn(entries, query=query, limit=limit) if callable(format_fn) else d._xcom.format_x_timeline_summary(entries, limit=limit)
        sources = [
            d._build_source(
                source_type="browser",
                title=str(entry.get("handle") or "").strip(),
                url=search_url,
                snippet=str(entry.get("text") or "").strip(),
                content_excerpt=str(entry.get("text") or "").strip(),
                content_text=str(entry.get("raw") or entry.get("text") or "").strip(),
                used_in_answer=True,
                source_backend="x_search",
                tweet_index=index,
                author=str(entry.get("author") or "").strip(),
                handle=str(entry.get("handle") or "").strip(),
            )
            for index, entry in enumerate(entries[:limit], start=1)
        ]
        return d._build_result(
            used_web=True,
            used_browser=True,
            summary=summary,
            sources=sources,
            confidence=0.80,
            mode="x_search",
            query=query,
        )

    d.log.info("X search produced no entries for query=%r; falling back to browser_search (Google)", query)
    try:
        return await d._browser_search_dispatch(query, context, max_results=max_results)
    except Exception as e:
        d.log.warning("x search fallback browser_search failed for query=%r: %r", query, e)
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary=f"X検索および代替検索に失敗しました: {e}",
            confidence=0.0,
            mode="x_search",
            query=query,
            error=str(e),
        )


__all__ = [
    "x_search_dispatch",
    "x_timeline_dispatch",
]
