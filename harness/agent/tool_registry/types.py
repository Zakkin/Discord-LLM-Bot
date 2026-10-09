"""AIエージェント・ハーネス ツール型定義 (types.py)

1-4. ツールレジストリ および フェーズ3. ツール選択と実行（Action）で用いられる
ツール定義、パラメータスキーマ、ツール呼び出し、実行結果の型モデルを定義する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine


class ToolCategory(str, Enum):
    """ツールの分類カテゴリ"""
    WEB = "web"
    MCP = "mcp"
    WEATHER = "weather"
    SYSTEM = "system"
    CUSTOM = "custom"


@dataclass
class ToolParameter:
    """ツールのパラメータ定義（JSON Schema準拠）"""
    name: str
    type: str  # "string", "number", "integer", "boolean", "array", "object"
    description: str = ""
    required: bool = True
    enum: list[Any] | None = None
    default: Any | None = None
    items: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None

    def to_schema(self) -> dict[str, Any]:
        """JSON Schema property定義に変換"""
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.default is not None:
            schema["default"] = self.default
        if self.items is not None:
            schema["items"] = dict(self.items)
        if self.properties is not None:
            schema["properties"] = dict(self.properties)
        return schema


@dataclass
class ToolDefinition:
    """ツールのカタログエントリ定義"""
    name: str
    description: str
    parameters: list[ToolParameter] = field(default_factory=list)
    handler: Callable[..., Any] | None = None
    category: ToolCategory = ToolCategory.CUSTOM
    tags: set[str] = field(default_factory=set)
    timeout_sec: float = 30.0
    enabled: bool = True

    def to_schema(self) -> dict[str, Any]:
        """OpenAI / Ollama 互換の Function Calling スキーマを生成"""
        properties: dict[str, Any] = {}
        required_params: list[str] = []

        for param in self.parameters:
            properties[param.name] = param.to_schema()
            if param.required:
                required_params.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_params,
                },
            },
        }

    def get_parameter(self, name: str) -> ToolParameter | None:
        """指定名のパラメータを取得"""
        for param in self.parameters:
            if param.name == name:
                return param
        return None


@dataclass
class ToolCall:
    """LLMから指示されたツール呼び出し（Action）モデル"""
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""

    @classmethod
    def from_ollama(cls, raw: dict[str, Any], default_call_id: str = "") -> ToolCall:
        """Ollama / OpenAI形式の tool_calls 辞書からパース"""
        fn = raw.get("function") or {}
        name = str(fn.get("name") or raw.get("name") or "")
        call_id = str(raw.get("id") or default_call_id or "")

        raw_args = fn.get("arguments") or raw.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {"raw_input": raw_args}
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = {}

        return cls(name=name, arguments=args, call_id=call_id)


@dataclass
class ToolResult:
    """ツール実行結果（Action結果 / Observation入力）モデル"""
    name: str
    success: bool
    output: str = ""
    error: str | None = None
    data: Any = None
    call_id: str = ""
    execution_time_sec: float = 0.0

    def to_observation_message(self) -> dict[str, Any]:
        """フェーズ4（Observation）用のメッセージ辞書を生成"""
        content = self.output if self.success else f"Error: {self.error or 'Tool execution failed'}"
        msg: dict[str, Any] = {
            "role": "tool",
            "name": self.name,
            "content": content,
        }
        if self.call_id:
            msg["tool_call_id"] = self.call_id
        return msg

    def to_dict(self) -> dict[str, Any]:
        """シリアライズ用辞書"""
        return {
            "name": self.name,
            "call_id": self.call_id,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "execution_time_sec": self.execution_time_sec,
        }
