"""AIエージェント・ハーネス Web検索ツールアダプター (adapters/web_search.py)

Web検索およびブラウジング調査機能を ToolDefinition として提供するアダプター。
"""
from __future__ import annotations

import logging
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.web_search")


class WebSearchToolAdapter:
    """Web検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_web", timeout_sec: float = 60.0) -> ToolDefinition:
        """search_web ツール定義を作成する"""
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

    @staticmethod
    async def execute_search(query: str, **kwargs: Any) -> str:
        """Web検索を実行し、要約テキストを返す"""
        log.info("[tool_registry] Invoking WebSearchToolAdapter query=%r", query)
        try:
            from harness.agent.tool_registry.web_research.dispatch import research_dispatch
            result = await research_dispatch(user_text=query, **kwargs)
            if isinstance(result, dict):
                return str(result.get("summary") or result.get("text") or result)
            return str(result)
        except Exception as e:
            log.warning("[tool_registry] WebSearchToolAdapter error: %s", e)
            return f"検索実行中にエラーが発生しました: {e}"
