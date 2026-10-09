"""AIエージェント・ハーネス ツールカタログ動的フォーマッタ (catalog_formatter.py)

1-4. ツールレジストリに登録されたツール定義から、
LLM（推論エンジン）のシステムプロンプトに注入・提示するための
カタログテキストを動的・構造的に生成する。
"""
from __future__ import annotations

from enum import Enum
import json
import logging
from typing import Any, Iterable

from .types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.catalog_formatter")


class CatalogFormat(str, Enum):
    """カタログテキストの出力フォーマット"""
    MARKDOWN = "markdown"  # Markdownリスト・表形式（推奨・汎用）
    XML = "xml"            # <tools><tool>...</tool></tools> 形式（推論モデル向け）
    JSON = "json"          # JSON Schema 配列形式
    COMPACT = "compact"    # 1行要約形式（コンテキスト長節約）


class ToolProtocol(str, Enum):
    """LLMに対するツール実行指示プロトコル"""
    REACT = "react"                # Action: tool_name \n Action Input: {...}
    JSON_CALL = "json_call"        # ```json\n{"tool": "name", "arguments": {...}}\n```
    FUNCTION_CALL = "function_call" # OpenAI / Ollama ネイティブ Function Calling 案内
    NONE = "none"                  # プロトコル指示なし（定義のみ提示）


class ToolCatalogFormatter:
    """ツールカタログをシステムプロンプト向けテキストに変換するフォーマッタ"""

    @classmethod
    def format_tools(
        cls,
        tools: Iterable[ToolDefinition],
        format: CatalogFormat | str = CatalogFormat.MARKDOWN,
        protocol: ToolProtocol | str = ToolProtocol.JSON_CALL,
        include_guidelines: bool = True,
    ) -> str:
        """ツール定義一覧からシステムプロンプト用カタログ文字列を動的生成する"""
        fmt = CatalogFormat(format) if isinstance(format, str) else format
        prot = ToolProtocol(protocol) if isinstance(protocol, str) else protocol
        tool_list = [t for t in tools if t.enabled]

        if not tool_list:
            return "（利用可能なツールはありません）"

        # 1. カタログ本体のレンダリング
        if fmt == CatalogFormat.MARKDOWN:
            catalog_body = cls._render_markdown(tool_list)
        elif fmt == CatalogFormat.XML:
            catalog_body = cls._render_xml(tool_list)
        elif fmt == CatalogFormat.JSON:
            catalog_body = cls._render_json(tool_list)
        elif fmt == CatalogFormat.COMPACT:
            catalog_body = cls._render_compact(tool_list)
        else:
            catalog_body = cls._render_markdown(tool_list)

        # 2. ツール利用ガイドライン（プロトコル指示）
        if not include_guidelines or prot == ToolProtocol.NONE:
            return catalog_body

        guidelines = cls._render_guidelines(prot)
        return f"{catalog_body}\n\n{guidelines}"

    @classmethod
    def _render_markdown(cls, tools: list[ToolDefinition]) -> str:
        """Markdown形式でレンダリング"""
        lines: list[str] = [
            "### 利用可能なツール一覧 (Available Tools)",
            "以下のツールが現在利用可能です。必要に応じて呼び出してください。",
            "",
        ]

        for t in tools:
            lines.append(f"#### `{t.name}`")
            lines.append(f"- **概要**: {t.description}")
            cat_val = t.category.value if isinstance(t.category, ToolCategory) else str(t.category)
            lines.append(f"- **カテゴリ**: `{cat_val}`")

            if t.parameters:
                lines.append("- **パラメータ (引数)**:")
                for p in t.parameters:
                    req_mark = "必須" if p.required else "任意"
                    default_desc = f", 初期値: {p.default!r}" if p.default is not None else ""
                    enum_desc = f", 選択肢: {p.enum}" if p.enum else ""
                    lines.append(
                        f"  - `{p.name}` ({p.type}, {req_mark}{default_desc}{enum_desc}): {p.description or '説明なし'}"
                    )
            else:
                lines.append("- **パラメータ**: なし")
            lines.append("")

        return "\n".join(lines).strip()

    @classmethod
    def _render_xml(cls, tools: list[ToolDefinition]) -> str:
        """XML形式でレンダリング（小型推論モデルやClaudeに効果的）"""
        lines: list[str] = ["<tools>"]
        for t in tools:
            lines.append(f'  <tool name="{t.name}">')
            lines.append(f"    <description>{t.description}</description>")
            if t.parameters:
                lines.append("    <parameters>")
                for p in t.parameters:
                    req_attr = ' required="true"' if p.required else ' required="false"'
                    lines.append(
                        f'      <parameter name="{p.name}" type="{p.type}"{req_attr}>{p.description}</parameter>'
                    )
                lines.append("    </parameters>")
            lines.append("  </tool>")
        lines.append("</tools>")
        return "\n".join(lines)

    @classmethod
    def _render_json(cls, tools: list[ToolDefinition]) -> str:
        """JSON Schema 配列でレンダリング"""
        schemas = [t.to_schema() for t in tools]
        return json.dumps(schemas, ensure_ascii=False, indent=2)

    @classmethod
    def _render_compact(cls, tools: list[ToolDefinition]) -> str:
        """1行要約のコンパクト形式"""
        lines: list[str] = ["【利用可能ツール一覧】"]
        for t in tools:
            params_str = ", ".join(
                f"{p.name}: {p.type}{'*' if p.required else ''}" for p in t.parameters
            )
            lines.append(f"- {t.name}({params_str}): {t.description}")
        return "\n".join(lines)

    @classmethod
    def _render_guidelines(cls, protocol: ToolProtocol) -> str:
        """LLM向けのツール実行ルールとフォーマット指示を生成"""
        header = "### ツール利用ガイドライン (Tool Use Protocol)\n"
        general_rules = (
            "1. **ツールの選択**: ユーザーの質問や指示に応えるために外部情報や計算が必要な場合のみツールを使用してください。\n"
            "2. **正確な引数指定**: 定義されたパラメータの型および必須要件を満たした引数を指定してください。\n"
            "3. **観察と回答**: ツール実行結果（Observation）が返されたら、その結果を十分に確認した上で最終的な回答をユーザーに伝えてください。\n"
        )

        if protocol == ToolProtocol.JSON_CALL:
            fmt_desc = (
                "4. **呼び出し形式**: ツールを呼び出す際は、以下のJSONブロックを出力してください:\n"
                "```json\n"
                "{\n"
                '  "tool": "ツール名",\n'
                '  "arguments": {\n'
                '    "引数名": "値"\n'
                "  }\n"
                "}\n"
                "```\n"
                "ツールを呼び出さない通常の会話時は、JSONを出力せず通常のテキストで応答してください。"
            )
            return f"{header}{general_rules}{fmt_desc}"

        elif protocol == ToolProtocol.REACT:
            fmt_desc = (
                "4. **呼び出し形式 (ReAct)**:\n"
                "Thought: ツールの必要性や次にすべき思考\n"
                "Action: ツール名\n"
                'Action Input: {"引数名": "値"}\n'
            )
            return f"{header}{general_rules}{fmt_desc}"

        elif protocol == ToolProtocol.FUNCTION_CALL:
            fmt_desc = (
                "4. **呼び出し形式**: Function Calling インターフェースに従ってツール呼び出しを発行してください。"
            )
            return f"{header}{general_rules}{fmt_desc}"

        return f"{header}{general_rules}"
