"""AIエージェント・ハーネス 汎用MCPクライアント基盤 (client.py)

HTTP / JSON-RPC ベースの MCP サーバーとの通信、セッション維持、ツール一覧取得・呼び出しを提供する。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .types import MCPToolCallResult, MCPToolInfo

log = logging.getLogger("harness.agent.tool_registry.mcp.client")


class BaseMCPClient:
    """汎用MCPクライアント基底インターフェース"""

    async def list_tools(self) -> list[dict[str, Any]]:
        """利用可能なツール一覧を取得する"""
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """指定したツールを実行する"""
        raise NotImplementedError

    async def get_tool_infos(self) -> list[MCPToolInfo]:
        """構造化された MCPToolInfo のリストを取得する"""
        raw_tools = await self.list_tools()
        return [MCPToolInfo.from_dict(t) for t in raw_tools if isinstance(t, dict)]
