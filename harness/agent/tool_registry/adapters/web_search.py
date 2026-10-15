"""AIエージェント・ハーネス Web検索ツールアダプター (adapters/web_search.py)

Web検索およびブラウジング調査機能を ToolDefinition として提供するアダプター。
Chrome MCP（ポート12306）によるブラウザ検索を優先実行し、障害時は simple_search へ多重防御フォールバックする。
"""
from __future__ import annotations

import logging
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.web_search")

# 検索クエリ引数の揺らぎ候補キー
_QUERY_FALLBACK_KEYS = (
    "search_query",
    "keyword",
    "keywords",
    "q",
    "text",
    "query_text",
    "search_text",
    "search_term",
    "input",
    "prompt",
    "target",
)


class WebSearchToolAdapter:
    """Web検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_web", timeout_sec: float = 40.0) -> ToolDefinition:
        """search_web ツール定義を作成する（デフォルトタイムアウト: 40秒）"""
        return ToolDefinition(
            name=name,
            description="インターネットを検索して最新の情報や不明な単語の意味を調べます。最新ニュースや調査が必要な場合に使用してください。",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="検索キーワード（例: '最新のAIニュース', 'Python MCPとは'）",
                    required=True,
                )
            ],
            handler=cls.execute_search,
            category=ToolCategory.WEB,
            tags={"web", "search", "research"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def _resolve_query(cls, query: str = "", **kwargs: Any) -> str:
        """query 引数が空の場合に kwargs から検索キーワードを自動解決する"""
        resolved = str(query or "").strip()
        if resolved:
            return resolved

        for key in _QUERY_FALLBACK_KEYS:
            val = kwargs.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()

        return ""

    @classmethod
    def _format_search_observation(cls, query: str, result: Any) -> str:
        """後続フェーズ（Observation / Final Response）向け構造化テキストに整形する。

        【Web検索結果】
        検索キーワード: <query>
        概要: <summary>
        主な情報源・解説:
        - <タイトル>: <本文スニペット/要約> (<URL>)
        """
        if not isinstance(result, dict):
            text = str(result or "").strip()
            if not text:
                return (
                    f"【Web検索結果】\n"
                    f"検索キーワード: {query or '(なし)'}\n"
                    f"概要: 該当する情報は見つかりませんでした。\n"
                    f"主な情報源・解説:\n"
                    f"- 該当する情報は見つかりませんでした"
                )
            return (
                f"【Web検索結果】\n"
                f"検索キーワード: {query or '(なし)'}\n"
                f"概要: {text}\n"
                f"主な情報源・解説:\n"
                f"- 検索結果メモ: {text}"
            )

        summary = str(result.get("summary") or "").strip()
        sources = list(result.get("sources") or [])
        error = str(result.get("error") or "").strip()

        source_lines: list[str] = []
        seen_urls: set[str] = set()
        for s in sources:
            if not isinstance(s, dict):
                continue
            title = str(s.get("title") or "").strip()
            url = str(s.get("url") or s.get("href") or "").strip()
            snippet = str(
                s.get("content_excerpt")
                or s.get("snippet")
                or s.get("body")
                or s.get("content_text")
                or ""
            ).strip()

            snippet = " ".join(snippet.split())
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."

            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)

            if title or snippet:
                display_title = title or "Web情報"
                desc_part = f": {snippet}" if snippet else ""
                url_part = f" ({url})" if url else ""
                source_lines.append(f"- {display_title}{desc_part}{url_part}")

        if not summary:
            if source_lines:
                summary = f"{query} に関するWeb検索結果を取得しました。"
            elif error:
                summary = f"検索中にエラーが発生しました: {error}"
            else:
                summary = "該当する情報は見つかりませんでした。"

        if not source_lines:
            sources_text = "- 該当する情報は見つかりませんでした"
        else:
            sources_text = "\n".join(source_lines[:5])

        return (
            f"【Web検索結果】\n"
            f"検索キーワード: {query or '(なし)'}\n"
            f"概要: {summary}\n"
            f"主な情報源・解説:\n"
            f"{sources_text}"
        )

    @classmethod
    async def execute_search(cls, query: str = "", **kwargs: Any) -> str:
        """Web検索を実行し、構造化テキストを返す。

        1. query 引数の揺らぎ解決（kwargs からフォールバック）
        2. Chrome MCP 優先実行 (mode="browser_search")
        3. 障害時の simple_search 多重防御フォールバック
        4. Observation 向け構造化テキスト整形
        """
        resolved_query = cls._resolve_query(query, **kwargs)
        log.info("[tool_registry] Invoking WebSearchToolAdapter resolved_query=%r", resolved_query)

        if not resolved_query:
            return cls._format_search_observation(
                query="",
                result={
                    "summary": "検索キーワードが指定されていません。",
                    "error": "検索キーワードが空です。",
                },
            )

        dispatch_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in ("mode", "user_text", "query") and k not in _QUERY_FALLBACK_KEYS
        }

        try:
            from harness.agent.tool_registry.web_research.dispatch import research_dispatch

            # 1. Chrome MCP 優先実行 (mode="browser_search")
            log.info("[tool_registry] Attempting browser_search via Chrome MCP query=%r", resolved_query)
            result: Any = None
            try:
                result = await research_dispatch(
                    user_text=resolved_query,
                    mode="browser_search",
                    **dispatch_kwargs,
                )
            except Exception as mcp_err:
                log.warning(
                    "[tool_registry] Chrome MCP browser_search exception for query=%r: %s",
                    resolved_query,
                    mcp_err,
                )
                result = None

            # 2. フォールバック判定 (未起動・エラー・結果なし等の場合に simple_search へ)
            need_fallback = False
            if result is None:
                need_fallback = True
            elif isinstance(result, dict):
                has_error = bool(result.get("error"))
                has_sources = bool(result.get("sources"))
                has_web = bool(result.get("used_web"))
                if has_error or (not has_sources and not has_web):
                    need_fallback = True
            else:
                need_fallback = True

            if need_fallback:
                log.info(
                    "[tool_registry] Chrome MCP browser_search failed or returned error; falling back to simple_search query=%r",
                    resolved_query,
                )
                try:
                    fallback_result = await research_dispatch(
                        user_text=resolved_query,
                        mode="simple_search",
                        **dispatch_kwargs,
                    )
                    if isinstance(fallback_result, dict):
                        if fallback_result.get("sources") or fallback_result.get("used_web") or not result:
                            result = fallback_result
                    elif not result:
                        result = fallback_result
                except Exception as fallback_err:
                    log.warning(
                        "[tool_registry] Fallback simple_search exception query=%r: %s",
                        resolved_query,
                        fallback_err,
                    )
                    if result is None:
                        result = {
                            "error": str(fallback_err),
                            "summary": f"検索に失敗しました: {fallback_err}",
                        }

            return cls._format_search_observation(resolved_query, result)

        except Exception as e:
            log.exception("[tool_registry] WebSearchToolAdapter unexpected error: %s", e)
            return cls._format_search_observation(
                resolved_query,
                {"error": str(e), "summary": f"検索実行中にエラーが発生しました: {e}"},
            )
