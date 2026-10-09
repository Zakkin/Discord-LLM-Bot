"""AIエージェント・ハーネス ツールコンテキスト動的構築モジュール (tool_context.py)

1. コンテキストの構築（知覚・記憶の引き出し）：
ユーザーからのプロンプトを受け取ると、会話履歴やRAG記憶に加えて、
「現在利用可能なツールの説明書（スキーマ・カタログ）」を外部MCPサーバー等から動的にかき集め、
システムプロンプトに結合してLLMに渡す準備を整える。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger("harness.agent.prompt_parser.tool_context")

# ツール利用・推論計画プロトコルの基本指示文
TOOL_USE_PLANNING_GUIDELINES = """### ツール利用と推論・計画立案プロトコル (Tool Use & Planning Protocol)
1. **推論と計画 (Thought & Planning)**:
   - ユーザーの目的を達成するために外部情報の検索やツールの実行が必要と判断した場合、まずはその思考理由（Thought）を整理してください。
   - 目的を達成するために「今何をすべきか」を推論し、利用可能なツール一覧から最適なツールを選び、「このツールを、この引数で実行する」という具体的な計画を出力してください。
2. **呼び出し形式 (Action Plan Format)**:
   ツールを呼び出す際は、以下のJSON形式で出力してください:
   ```json
   {
     "thought": "目的達成のための推論・計画（例: 最新の気象情報を確認するためにまず〇〇ツールで検索する）",
     "tool": "ツール名",
     "arguments": {
       "引数名": "値"
     }
   }
   ```
   ※ツール呼び出しを行わない通常の雑談・相槌・回答時は、JSONを出力せず通常のテキストで応答してください。
3. **正確な引数の指定**:
   定義されたスキーマに従い、必須パラメータおよび型制約を満たした引数を指定してください。
"""


class ToolContextManager:
    """ツールカタログの動的取得・MCP連携・プロンプト結合を司るマネージャー"""

    def __init__(self, registry: Any = None) -> None:
        self.registry = registry

    @classmethod
    def get_tool_registry(cls, registry: Any = None) -> Any:
        """指定されたレジストリ、またはデフォルトのToolRegistryを取得する"""
        if registry is not None:
            return registry
        try:
            from harness.agent.tool_registry import ToolRegistry, get_default_registry
            return get_default_registry()
        except ImportError:
            log.debug("[prompt-manager] ToolRegistry is not available in environment")
            return None

    @classmethod
    async def sync_mcp_tools(
        cls,
        registry: Any = None,
        mcp_client: Any = None,
        category: Any = None,
    ) -> list[Any]:
        """外部MCPサーバー（mcp/）に接続し、利用可能なツールカタログを動的にインポートする"""
        reg = cls.get_tool_registry(registry)
        if reg is None:
            log.warning("[prompt-manager] Cannot sync MCP tools: registry is None")
            return []

        try:
            from harness.agent.tool_registry.adapters import MCPToolAdapter
            from harness.agent.tool_registry.types import ToolCategory
        except ImportError:
            log.warning("[prompt-manager] Cannot sync MCP tools: tool_registry module not importable")
            return []

        client = mcp_client
        if client is None:
            # デフォルトで WeatherMCPClient / Server が利用可能であれば接続試行
            try:
                from harness.agent.tool_registry.mcp import WeatherMCPClient, WeatherMCPServer
                client = WeatherMCPClient(server=WeatherMCPServer())
            except Exception as e:
                log.warning("[prompt-manager] Default WeatherMCPClient initialization failed: %s", e)
                return []

        cat = category or ToolCategory.MCP
        imported = await MCPToolAdapter.import_tools_from_client(
            client=client,
            registry=reg,
            category=cat,
        )
        log.info(
            "[prompt-manager] Successfully synced %d tools from MCP server into registry",
            len(imported),
        )
        return imported

    @classmethod
    def render_tool_catalog(
        cls,
        tools_or_registry: Any,
        format_name: str = "markdown",
        include_guidelines: bool = True,
    ) -> str:
        """ツール定義一覧またはレジストリから、プロンプト結合用のカタログ文字列を生成する"""
        if tools_or_registry is None:
            return ""

        tools: list[Any] = []
        if hasattr(tools_or_registry, "list_tools"):
            tools = tools_or_registry.list_tools()
        elif hasattr(tools_or_registry, "tools"):
            tools = list(tools_or_registry.tools.values())
        elif isinstance(tools_or_registry, Iterable):
            tools = list(tools_or_registry)

        enabled_tools = [t for t in tools if getattr(t, "enabled", True)]
        if not enabled_tools:
            return ""

        # tool_registry の ToolCatalogFormatter が利用可能なら活用
        try:
            from harness.agent.tool_registry.catalog_formatter import (
                CatalogFormat,
                ToolCatalogFormatter,
                ToolProtocol,
            )
            # 独自フォーマットの指定
            catalog_text = ToolCatalogFormatter.format_tools(
                tools=enabled_tools,
                format=format_name,
                protocol=ToolProtocol.JSON_CALL,
                include_guidelines=False,
            )
        except ImportError:
            # フォールバックの簡易Markdownレンダラー
            lines = ["### 利用可能なツール一覧 (Available Tools)", ""]
            for t in enabled_tools:
                t_name = getattr(t, "name", "unknown")
                t_desc = getattr(t, "description", "")
                lines.append(f"#### `{t_name}`")
                lines.append(f"- **概要**: {t_desc}")
                params = getattr(t, "parameters", [])
                if params:
                    lines.append("- **パラメータ**:")
                    for p in params:
                        p_name = getattr(p, "name", "")
                        p_type = getattr(p, "type", "string")
                        p_req = "必須" if getattr(p, "required", True) else "任意"
                        p_desc = getattr(p, "description", "")
                        lines.append(f"  - `{p_name}` ({p_type}, {p_req}): {p_desc}")
                else:
                    lines.append("- **パラメータ**: なし")
                lines.append("")
            catalog_text = "\n".join(lines).strip()

        if include_guidelines:
            return f"{catalog_text}\n\n{TOOL_USE_PLANNING_GUIDELINES}".strip()
        return catalog_text.strip()

    @classmethod
    def bind_to_system_prompt(
        cls,
        base_system_prompt: str,
        tools_or_registry: Any,
        format_name: str = "markdown",
    ) -> str:
        """システムプロンプトにツールカタログと推論・計画ガイドラインを動的結合する"""
        catalog = cls.render_tool_catalog(
            tools_or_registry=tools_or_registry,
            format_name=format_name,
            include_guidelines=True,
        )
        if not catalog:
            return base_system_prompt

        tool_section = f"\n\n【利用可能なツール（外部連携・MCP機能）】\n{catalog}\n"

        if "{TOOL_CATALOG}" in base_system_prompt:
            return base_system_prompt.replace("{TOOL_CATALOG}", catalog)

        # プレースホルダーがない場合は末尾（または返答形式の前）に結合
        insert_marker = "【返答の形式】"
        if insert_marker in base_system_prompt:
            parts = base_system_prompt.split(insert_marker, 1)
            return f"{parts[0]}{tool_section}\n{insert_marker}{parts[1]}"

        return f"{base_system_prompt.rstrip()}{tool_section}"
