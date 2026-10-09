"""AIエージェント・ハーネス ツールレジストリ (registry.py)

1-4. ツールレジストリ（フェーズ1/3に関係）の中核コンポーネント。
利用可能なツールカタログ（スキーマ）を一元管理し、
Function Callingの実処理へのルーティングと実行を提供する。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from .executor import ToolExecutor
from .types import ToolCall, ToolCategory, ToolDefinition, ToolParameter, ToolResult

log = logging.getLogger("harness.agent.tool_registry.registry")


class ToolRegistry:
    """ツールカタログおよび実行ルーティングを管理するレジストリクラス"""

    def __init__(
        self,
        executor: ToolExecutor | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.executor = executor or ToolExecutor(logger=logger)
        self.log = logger or log

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        """ツール定義をレジストリに登録する。"""
        if tool.name in self._tools:
            self.log.info(
                "[tool_registry] Overwriting registered tool: name=%s category=%s",
                tool.name,
                tool.category.value if isinstance(tool.category, ToolCategory) else tool.category,
            )
        else:
            self.log.info(
                "[tool_registry] Registered new tool: name=%s category=%s params=%d",
                tool.name,
                tool.category.value if isinstance(tool.category, ToolCategory) else tool.category,
                len(tool.parameters),
            )
        self._tools[tool.name] = tool
        return tool

    def register_tool(self, tool: ToolDefinition) -> ToolDefinition:
        """register のエイリアス"""
        return self.register(tool)

    def register_function(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        category: ToolCategory = ToolCategory.CUSTOM,
        timeout_sec: float = 30.0,
        tags: set[str] | None = None,
    ) -> ToolDefinition:
        """Python関数から自動的にToolDefinitionを構築して登録する。"""
        tool_name = name or func.__name__
        tool_desc = description or (func.__doc__ or "").strip() or f"Execute {tool_name}"

        # 引数シグネチャの解析
        sig = inspect.signature(func)
        parameters: list[ToolParameter] = []

        type_map: dict[Any, str] = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "list": "array",
            "dict": "object",
        }

        for p_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            param_type = "string"
            ann = param.annotation
            if ann != inspect.Parameter.empty:
                if ann in type_map:
                    param_type = type_map[ann]
                elif hasattr(ann, "__name__") and ann.__name__ in type_map:
                    param_type = type_map[ann.__name__]
            elif param.default != inspect.Parameter.empty and type(param.default) in type_map:
                param_type = type_map[type(param.default)]

            is_required = param.default == inspect.Parameter.empty
            default_val = None if is_required else param.default

            parameters.append(
                ToolParameter(
                    name=p_name,
                    type=param_type,
                    description=f"Parameter {p_name}",
                    required=is_required,
                    default=default_val,
                )
            )

        tool = ToolDefinition(
            name=tool_name,
            description=tool_desc,
            parameters=parameters,
            handler=func,
            category=category,
            tags=tags or set(),
            timeout_sec=timeout_sec,
        )
        return self.register(tool)

    def unregister(self, name: str) -> bool:
        """ツール登録を解除する。"""
        if name in self._tools:
            del self._tools[name]
            self.log.info("[tool_registry] Unregistered tool: name=%s", name)
            return True
        return False

    def get(self, name: str) -> ToolDefinition | None:
        """指定名のツール定義を取得する。"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """ツールが登録されているか判定する。"""
        return name in self._tools

    def list_tools(
        self,
        category: ToolCategory | str | None = None,
        enabled_only: bool = True,
        tag: str | None = None,
    ) -> list[ToolDefinition]:
        """条件に合致するツールのリストを取得する。"""
        result: list[ToolDefinition] = []
        target_category = category.value if isinstance(category, ToolCategory) else category

        for tool in self._tools.values():
            if enabled_only and not tool.enabled:
                continue
            if target_category is not None:
                tool_cat = tool.category.value if isinstance(tool.category, ToolCategory) else tool.category
                if tool_cat != target_category:
                    continue
            if tag is not None and tag not in tool.tags:
                continue
            result.append(tool)
        return result

    def enable(self, name: str) -> bool:
        """ツールを有効化する。"""
        tool = self._tools.get(name)
        if tool:
            tool.enabled = True
            self.log.info("[tool_registry] Enabled tool: name=%s", name)
            return True
        return False

    def disable(self, name: str) -> bool:
        """ツールを無効化する。"""
        tool = self._tools.get(name)
        if tool:
            tool.enabled = False
            self.log.info("[tool_registry] Disabled tool: name=%s", name)
            return True
        return False

    def get_schemas(
        self,
        category: ToolCategory | str | None = None,
        enabled_only: bool = True,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """LLM呼び出し用のツールカタログ（OpenAI / Ollama 互換スキーマ群）を生成する。"""
        tools = self.list_tools(category=category, enabled_only=enabled_only, tag=tag)
        schemas = [t.to_schema() for t in tools]
        self.log.debug(
            "[tool_registry] Exported schemas count=%d category=%s",
            len(schemas),
            category,
        )
        return schemas

    def render_catalog(
        self,
        format: str = "markdown",
        protocol: str = "json_call",
        include_guidelines: bool = True,
        category: ToolCategory | str | None = None,
        tag: str | None = None,
    ) -> str:
        """登録されたツールのカタログテキストを動的に生成する（LLMプロンプト向け）。"""
        from .catalog_formatter import ToolCatalogFormatter

        tools = self.list_tools(category=category, enabled_only=True, tag=tag)
        return ToolCatalogFormatter.format_tools(
            tools=tools,
            format=format,
            protocol=protocol,
            include_guidelines=include_guidelines,
        )

    def bind_to_system_prompt(
        self,
        base_system_prompt: str,
        format: str = "markdown",
        protocol: str = "json_call",
        placeholder: str = "{TOOL_CATALOG}",
        include_guidelines: bool = True,
        section_header: str = "\n\n## 🛠️ 外部ツール連携 (Tool Integrations)\n",
        category: ToolCategory | str | None = None,
        tag: str | None = None,
    ) -> str:
        """システムプロンプトに動的ツールカタログ情報を結合してLLMに渡す準備を整える。"""
        from .prompt_integrator import bind_tools_to_system_prompt

        return bind_tools_to_system_prompt(
            base_system_prompt=base_system_prompt,
            tools=self,
            format=format,
            protocol=protocol,
            placeholder=placeholder,
            include_guidelines=include_guidelines,
            section_header=section_header,
            category=category,
            tag=tag,
        )

    async def execute(
        self,
        tool_call_or_name: ToolCall | str,
        arguments: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> ToolResult:
        """ツールの実行リクエストをルーティングして処理する。"""
        if isinstance(tool_call_or_name, str):
            call = ToolCall(name=tool_call_or_name, arguments=arguments or {})
        else:
            call = tool_call_or_name

        tool = self.get(call.name)
        if tool is None:
            err_msg = f"Tool '{call.name}' not found in registry."
            self.log.error("[tool_registry] Routing error: %s", err_msg)
            return ToolResult(
                name=call.name,
                call_id=call.call_id,
                success=False,
                error=err_msg,
            )

        return await self.executor.execute(tool, call, timeout_sec=timeout_sec)
