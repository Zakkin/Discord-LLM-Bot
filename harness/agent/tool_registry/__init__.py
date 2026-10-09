"""ハーネス 1-4. ツールレジストリ(フェーズ1/3に関係)

AIエージェント・ハーネスの実行基盤（エージェント本体）における
・利用可能なツールカタログ（スキーマ）の管理（ToolRegistry / ToolDefinition）
・Function Callingの実処理へのルーティング・実行制御（ToolExecutor / Action）
・外部プロトコル連携（Web検索, 天気, MCPアダプター等）
を集約・提供する。
"""
from __future__ import annotations

import logging
from typing import Any

from .adapters import MCPToolAdapter, WeatherToolAdapter, WebSearchToolAdapter
from .catalog_formatter import CatalogFormat, ToolCatalogFormatter, ToolProtocol
from .decorators import get_default_registry, set_default_registry, tool
from .executor import ToolExecutor
from .mcp import BaseMCPClient, MCPToolCallResult, MCPToolInfo, WeatherMCPClient, WeatherMCPServer
from .prompt_integrator import SystemPromptBuilder, bind_tools_to_system_prompt
from .game_pool_manager import GamePoolManager
from . import singing_helpers, umigame_helpers, weather_helpers, web_research
from .registry import ToolRegistry
from .types import (
    ToolCall,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolResult,
)

log = logging.getLogger("harness.agent.tool_registry")


def register_builtin_tools(
    registry: ToolRegistry | None = None,
    enable_web_search: bool = True,
    enable_weather: bool = True,
    enable_japan_weather_mcp: bool = False,
) -> ToolRegistry:
    """ビルトインの主要ツール（Web検索、天気等）を指定レジストリに登録する。"""
    reg = registry or get_default_registry()

    if enable_web_search and not reg.has("search_web"):
        web_tool = WebSearchToolAdapter.create_tool()
        reg.register(web_tool)
        log.info("[tool_registry] Registered builtin tool: search_web")

    if enable_weather and not reg.has("get_weather"):
        weather_tool = WeatherToolAdapter.create_tool()
        reg.register(weather_tool)
        log.info("[tool_registry] Registered builtin tool: get_weather")

    if enable_japan_weather_mcp:
        import asyncio
        client = WeatherMCPClient()
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                loop.run_until_complete(
                    MCPToolAdapter.import_tools_from_client(client, reg, category=ToolCategory.WEATHER)
                )
        except Exception as e:
            log.warning("[tool_registry] Failed to auto-register weather MCP tools: %s", e)

    return reg


async def register_weather_mcp_tools(
    registry: ToolRegistry | None = None,
    client: WeatherMCPClient | None = None,
) -> list[ToolDefinition]:
    """国内気象情報 MCP ツール群（気象庁＆ウェザーニュース）をレジストリに非同期登録する。"""
    reg = registry or get_default_registry()
    mcp_client = client or WeatherMCPClient()
    tools = await MCPToolAdapter.import_tools_from_client(
        client=mcp_client,
        registry=reg,
        category=ToolCategory.WEATHER,
    )
    log.info("[tool_registry] Registered %d Japan Weather MCP tools to registry", len(tools))
    return tools


__all__ = [
    # Core Facades
    "ToolRegistry",
    "ToolExecutor",
    # Data Models
    "ToolDefinition",
    "ToolParameter",
    "ToolCall",
    "ToolResult",
    "ToolCategory",
    # Decorators & Registry Helpers
    "tool",
    "get_default_registry",
    "set_default_registry",
    "register_builtin_tools",
    "register_weather_mcp_tools",
    # Dynamic Catalog & System Prompt Integration
    "ToolCatalogFormatter",
    "CatalogFormat",
    "ToolProtocol",
    "bind_tools_to_system_prompt",
    "SystemPromptBuilder",
    # Adapters & Protocols
    "WebSearchToolAdapter",
    "WeatherToolAdapter",
    "MCPToolAdapter",
    "BaseMCPClient",
    "MCPToolInfo",
    "MCPToolCallResult",
    "WeatherMCPClient",
    "WeatherMCPServer",
    # Game & Action Helpers
    "GamePoolManager",
    "singing_helpers",
    "umigame_helpers",
    "weather_helpers",
    "web_research",
]
