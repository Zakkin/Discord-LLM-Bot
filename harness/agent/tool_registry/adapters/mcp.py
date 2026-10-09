"""AIエージェント・ハーネス MCPプロトコル連携アダプター (adapters/mcp.py)

MCP (Model Context Protocol) サーバー・クライアントから動的にツールを取得し、
ToolRegistry にマッピングして実行を仲介するアダプター。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.mcp")


class MCPToolAdapter:
    """MCPプロトコル連携アダプター"""

    @classmethod
    def convert_mcp_tool_to_definition(
        cls,
        mcp_tool: dict[str, Any],
        caller_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, Any]],
        category: ToolCategory = ToolCategory.MCP,
        timeout_sec: float = 45.0,
    ) -> ToolDefinition:
        """MCPツールのメタデータ辞書から ToolDefinition を生成する"""
        name = str(mcp_tool.get("name") or "mcp_tool")
        description = str(mcp_tool.get("description") or f"MCP tool: {name}")
        schema = mcp_tool.get("inputSchema") or mcp_tool.get("parameters") or {}

        parameters: list[ToolParameter] = []
        if isinstance(schema, dict):
            properties = schema.get("properties") or {}
            required_keys = set(schema.get("required") or [])
            if isinstance(properties, dict):
                for p_name, p_val in properties.items():
                    if isinstance(p_val, dict):
                        p_type = str(p_val.get("type") or "string")
                        p_desc = str(p_val.get("description") or "")
                        p_enum = p_val.get("enum")
                        parameters.append(
                            ToolParameter(
                                name=p_name,
                                type=p_type,
                                description=p_desc,
                                required=p_name in required_keys,
                                enum=list(p_enum) if isinstance(p_enum, list) else None,
                            )
                        )

        async def _mcp_handler(**kwargs: Any) -> Any:
            log.info("[tool_registry] Invoking MCP tool=%s arguments=%s", name, kwargs)
            return await caller_fn(name, kwargs)

        return ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=_mcp_handler,
            category=category,
            tags={"mcp", category.value if isinstance(category, ToolCategory) else str(category)},
            timeout_sec=timeout_sec,
        )

    @classmethod
    async def import_tools_from_client(
        cls,
        client: Any,
        registry: Any,
        category: ToolCategory = ToolCategory.MCP,
        timeout_sec: float = 45.0,
    ) -> list[ToolDefinition]:
        """MCPクライアント（list_tools, call_toolを持つオブジェクト）からツールを一括登録する"""
        if not hasattr(client, "list_tools") or not hasattr(client, "call_tool"):
            log.warning(
                "[tool_registry] Object does not implement MCP client interface (list_tools, call_tool)"
            )
            return []

        try:
            raw_tools = await client.list_tools()
            imported: list[ToolDefinition] = []

            async def _caller(tool_name: str, args: dict[str, Any]) -> Any:
                return await client.call_tool(tool_name, args)

            for raw_tool in raw_tools:
                if isinstance(raw_tool, dict):
                    tool_def = cls.convert_mcp_tool_to_definition(
                        mcp_tool=raw_tool,
                        caller_fn=_caller,
                        category=category,
                        timeout_sec=timeout_sec,
                    )
                    registry.register(tool_def)
                    imported.append(tool_def)

            log.info("[tool_registry] Imported %d tools from MCP client", len(imported))
            return imported
        except Exception as e:
            log.exception("[tool_registry] Failed to import tools from MCP client: %s", e)
            return []
