"""AIエージェント・ハーネス システムプロンプト結合エンジン (prompt_integrator.py)

1-4. ツールレジストリのカタログ情報を動的に取得し、
LLM（推論エンジン）に渡すシステムプロンプトにシームレスに結合・注入する。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from .catalog_formatter import CatalogFormat, ToolCatalogFormatter, ToolProtocol
from .types import ToolCategory, ToolDefinition

log = logging.getLogger("harness.agent.tool_registry.prompt_integrator")

DEFAULT_PLACEHOLDERS = [
    "{TOOL_CATALOG}",
    "{{tools}}",
    "{{tool_catalog}}",
    "{{tool_definitions}}",
    "<TOOL_CATALOG>",
]


def bind_tools_to_system_prompt(
    base_system_prompt: str,
    tools: Iterable[ToolDefinition] | Any,
    format: CatalogFormat | str = CatalogFormat.MARKDOWN,
    protocol: ToolProtocol | str = ToolProtocol.JSON_CALL,
    placeholder: str = "{TOOL_CATALOG}",
    include_guidelines: bool = True,
    section_header: str = "\n\n## 🛠️ 外部ツール連携 (Tool Integrations)\n",
    category: ToolCategory | str | None = None,
    tag: str | None = None,
) -> str:
    """システムプロンプトにツールカタログ情報を動的に結合する。

    プレースホルダーが存在する場合はその位置に展開し、
    存在しない場合はプロンプトの末尾にセクションとして追加する。
    """
    # ToolRegistry オブジェクトが渡された場合は list_tools を呼び出す
    if hasattr(tools, "list_tools"):
        tool_list = tools.list_tools(category=category, tag=tag, enabled_only=True)
    elif isinstance(tools, Iterable):
        tool_list = list(tools)
        if category is not None:
            cat_val = category.value if isinstance(category, ToolCategory) else category
            tool_list = [
                t for t in tool_list
                if (t.category.value if isinstance(t.category, ToolCategory) else t.category) == cat_val
            ]
        if tag is not None:
            tool_list = [t for t in tool_list if tag in t.tags]
    else:
        tool_list = []

    # カタログテキストの生成
    catalog_text = ToolCatalogFormatter.format_tools(
        tools=tool_list,
        format=format,
        protocol=protocol,
        include_guidelines=include_guidelines,
    )

    log.debug(
        "[tool_registry] Binding %d tools to system prompt (format=%s)",
        len(tool_list),
        format,
    )

    # 1. 指定されたプレースホルダーの確認
    if placeholder in base_system_prompt:
        return base_system_prompt.replace(placeholder, catalog_text)

    # 2. 代表的なプレースホルダーの自動フォールバック確認
    for ph in DEFAULT_PLACEHOLDERS:
        if ph in base_system_prompt:
            return base_system_prompt.replace(ph, catalog_text)

    # 3. プレースホルダーがなければ末尾にセクションとして追加
    return f"{base_system_prompt.rstrip()}{section_header}{catalog_text}\n"


class SystemPromptBuilder:
    """動的ツールカタログ結合を支援するシステムプロンプトビルダー"""

    def __init__(self, base_prompt: str = "") -> None:
        self._base_prompt = base_prompt
        self._tools: list[ToolDefinition] = []
        self._registry: Any = None
        self._format: CatalogFormat | str = CatalogFormat.MARKDOWN
        self._protocol: ToolProtocol | str = ToolProtocol.JSON_CALL
        self._placeholder: str = "{TOOL_CATALOG}"
        self._include_guidelines: bool = True
        self._category: ToolCategory | str | None = None
        self._tag: str | None = None
        self._extra_instructions: list[str] = []

    def with_base_prompt(self, base_prompt: str) -> SystemPromptBuilder:
        """ベースとなるシステムプロンプトを設定"""
        self._base_prompt = base_prompt
        return self

    def with_registry(self, registry: Any) -> SystemPromptBuilder:
        """ToolRegistry を設定"""
        self._registry = registry
        return self

    def with_tools(self, tools: Iterable[ToolDefinition]) -> SystemPromptBuilder:
        """ツール一覧を直接設定"""
        self._tools = list(tools)
        return self

    def with_format(self, format: CatalogFormat | str) -> SystemPromptBuilder:
        """カタログ出力フォーマットを設定"""
        self._format = format
        return self

    def with_protocol(self, protocol: ToolProtocol | str) -> SystemPromptBuilder:
        """ツール利用プロトコルを設定"""
        self._protocol = protocol
        return self

    def with_placeholder(self, placeholder: str) -> SystemPromptBuilder:
        """置換先プレースホルダーを設定"""
        self._placeholder = placeholder
        return self

    def with_guidelines(self, include: bool = True) -> SystemPromptBuilder:
        """ガイドライン付与の有無を設定"""
        self._include_guidelines = include
        return self

    def with_category_filter(self, category: ToolCategory | str | None) -> SystemPromptBuilder:
        """ツールのカテゴリフィルタを設定"""
        self._category = category
        return self

    def with_tag_filter(self, tag: str | None) -> SystemPromptBuilder:
        """ツールのタグフィルタを設定"""
        self._tag = tag
        return self

    def add_instruction(self, instruction: str) -> SystemPromptBuilder:
        """追加指示文をアペンド"""
        self._extra_instructions.append(instruction)
        return self

    def build(self) -> str:
        """システムプロンプトを構築して返却する"""
        source = self._registry if self._registry is not None else self._tools
        prompt = bind_tools_to_system_prompt(
            base_system_prompt=self._base_prompt,
            tools=source,
            format=self._format,
            protocol=self._protocol,
            placeholder=self._placeholder,
            include_guidelines=self._include_guidelines,
            category=self._category,
            tag=self._tag,
        )

        if self._extra_instructions:
            extra_block = "\n\n### 追加運用ルール:\n" + "\n".join(
                f"- {ins}" for ins in self._extra_instructions
            )
            prompt = f"{prompt.rstrip()}{extra_block}\n"

        return prompt
