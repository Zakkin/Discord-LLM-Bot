"""国内気象情報 MCP クライアント (mcp/weather/client.py)

BaseMCPClient を継承し、気象 MCP サーバーとの通信（ダイレクト実行または HTTP 通信）
およびツール一覧取得・ツール実行を提供する。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..client import BaseMCPClient
from ..types import MCPToolCallResult, MCPToolInfo
from .server import WeatherMCPServer

log = logging.getLogger("harness.agent.tool_registry.mcp.weather.client")


class WeatherMCPClient(BaseMCPClient):
    """国内気象情報 MCP クライアント

    ダイレクト（インプロセス）実行とリモート（HTTP JSON-RPC）実行の両方に対応。
    """

    def __init__(
        self,
        server_url: str | None = None,
        server: WeatherMCPServer | None = None,
        timeout_sec: float = 20.0,
    ) -> None:
        self.server_url = server_url
        self.server = server or (WeatherMCPServer() if not server_url else None)
        self.timeout_sec = timeout_sec

    async def list_tools(self) -> list[dict[str, Any]]:
        """利用可能な気象ツール一覧を取得する"""
        if self.server is not None:
            # ダイレクト実行（インプロセス）
            res = await self.server.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            result_data = res.get("result", {})
            tools = result_data.get("tools", [])
            return list(tools) if isinstance(tools, list) else []

        if not self.server_url:
            return []

        # HTTP通信実行
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.server_url, json=payload) as resp:
                data = await resp.json()
                tools = data.get("result", {}).get("tools", [])
                return list(tools) if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """指定した気象ツールを実行し、テキスト結果を返却する"""
        if self.server is not None:
            # ダイレクト実行
            res = await self.server.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            if "error" in res:
                err_msg = res["error"].get("message", "Unknown error")
                raise RuntimeError(f"Weather MCP error: {err_msg}")

            content = res.get("result", {}).get("content", [])
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    return str(first.get("text") or "")
            return ""

        if not self.server_url:
            raise RuntimeError("WeatherMCPClient has no active server or server_url")

        # HTTP通信実行
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.server_url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Weather MCP HTTP error: {data['error'].get('message')}")
                content = data.get("result", {}).get("content", [])
                if isinstance(content, list) and content:
                    return str(content[0].get("text") or "")
                return ""
