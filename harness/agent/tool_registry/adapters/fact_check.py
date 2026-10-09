"""AIエージェント・ハーネス ファクトチェックツールアダプター (adapters/fact_check.py)

ファクトチェック・言説検証・Web検索機能を ToolDefinition として提供するアダプター。
"""
from __future__ import annotations

import logging
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.fact_check")


class FactCheckToolAdapter:
    """ファクトチェック・Web裏取りツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "fact_check_verify", timeout_sec: float = 30.0) -> ToolDefinition:
        """fact_check_verify ツール定義を作成する"""
        return ToolDefinition(
            name=name,
            description="指定された言説・テキストの裏取りWeb検索および真偽検証を行い、信頼度付きの要約と出典URLを取得します。",
            parameters=[
                ToolParameter(
                    name="claim",
                    type="string",
                    description="検証対象の言説・テキスト・ニュース（URLを含む場合は自動展開されます）",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="検索件数の上限（デフォルト: 3）",
                    required=False,
                    default=3,
                ),
            ],
            handler=cls.execute_fact_check,
            category=ToolCategory.WEB,
            tags={"fact_check", "verification", "web_search"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def create_search_tool(cls, name: str = "search_web", timeout_sec: float = 20.0) -> ToolDefinition:
        """search_web ツール定義を作成する"""
        return ToolDefinition(
            name=name,
            description="Web検索を実行し、最新の関連Webページ・記事情報を取得します。",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="検索キーワード・クエリ",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="検索件数の上限（デフォルト: 3）",
                    required=False,
                    default=3,
                ),
            ],
            handler=cls.execute_search,
            category=ToolCategory.WEB,
            tags={"search", "web"},
            timeout_sec=timeout_sec,
        )


    @staticmethod
    async def execute_fact_check(claim: str, max_results: int = 3, **kwargs: Any) -> str:
        """ファクトチェックを実行し、要約テキストを返す"""
        log.info("[tool_registry] Invoking FactCheckToolAdapter claim=%r", claim)
        try:
            from ..mcp.fact_check import FactCheckService
            service = FactCheckService()
            result = await service.verify_claim(claim, max_results=max_results)
            return f"【ファクトチェック結果】\nステータス: {result.status}\n信頼度: {result.confidence:.1f}\n\n{result.summary}"
        except Exception as e:
            log.warning("[tool_registry] FactCheckToolAdapter error: %s", e)
            return f"ファクトチェックの実行中にエラーが発生しました: {e}"

    @staticmethod
    async def execute_search(query: str, max_results: int = 3, **kwargs: Any) -> str:
        """Web検索を実行し、要約テキストを返す"""
        log.info("[tool_registry] Invoking FactCheckToolAdapter search query=%r", query)
        try:
            from ..mcp.fact_check import search_web
            return await search_web(query, max_results=max_results)
        except Exception as e:
            log.warning("[tool_registry] FactCheckToolAdapter search error: %s", e)
            return f"Web検索の実行中にエラーが発生しました: {e}"
