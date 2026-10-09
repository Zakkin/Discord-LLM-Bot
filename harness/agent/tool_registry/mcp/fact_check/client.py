"""ファクトチェック MCP クライアント (mcp/fact_check/client.py)

BaseMCPClient を継承し、ファクトチェック MCP サーバーとの通信（ダイレクト実行または HTTP 通信）
およびツール一覧取得・ツール実行を提供する。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..client import BaseMCPClient
from ..types import MCPToolCallResult, MCPToolInfo
from .server import FactCheckMCPServer
from .types import FactCheckResult, FactCheckTargetContext, ReplyActionType

log = logging.getLogger("harness.agent.tool_registry.mcp.fact_check.client")


class FactCheckMCPClient(BaseMCPClient):
    """ファクトチェック MCP クライアント

    ダイレクト（インプロセス）実行とリモート（HTTP JSON-RPC）実行の両方に対応。
    """

    def __init__(
        self,
        server_url: str | None = None,
        server: FactCheckMCPServer | None = None,
        timeout_sec: float = 20.0,
    ) -> None:
        self.server_url = server_url
        self.server = server or (FactCheckMCPServer() if not server_url else None)
        self.timeout_sec = timeout_sec

    async def list_tools(self) -> list[dict[str, Any]]:
        """利用可能なツール一覧を取得する"""
        if self.server is not None:
            res = await self.server.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            result_data = res.get("result", {})
            tools = result_data.get("tools", [])
            return list(tools) if isinstance(tools, list) else []

        if not self.server_url:
            return []

        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.server_url, json=payload) as resp:
                data = await resp.json()
                tools = data.get("result", {}).get("tools", [])
                return list(tools) if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """指定したツールを実行し、テキスト結果を返却する"""
        if self.server is not None:
            res = await self.server.call_tool(name, arguments)
            if res.get("isError"):
                content = res.get("content", [])
                err_text = content[0].get("text", "エラー") if content else "エラー"
                return f"エラー: {err_text}"
            content = res.get("content", [])
            return str(content[0].get("text", "")) if content else ""

        if not self.server_url:
            return "MCP サーバーが設定されていません。"

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.server_url, json=payload) as resp:
                data = await resp.json()
                call_res = data.get("result", {})
                content = call_res.get("content", [])
                return str(content[0].get("text", "")) if content else ""

    async def verify_claim(self, claim: str, max_results: int = 3) -> str:
        """言説のファクトチェック・裏取り検証を行う"""
        return await self.call_tool("fact_check_verify", {"claim": claim, "max_results": max_results})

    async def search_web(self, query: str, max_results: int = 3) -> str:
        """Web検索を実行する"""
        return await self.call_tool("fact_check_search_web", {"query": query, "max_results": max_results})

    async def extract_context(self, text: str) -> str:
        """URLメタデータ・文脈要約を抽出する"""
        return await self.call_tool("fact_check_extract_context", {"text": text})

    async def detect_action(self, text: str) -> str:
        """ユーザー発話からアクション種別を検出する"""
        return await self.call_tool("fact_check_detect_action", {"text": text})
