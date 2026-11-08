"""AIエージェント・ハーネス MCPプロトコル型定義 (types.py)

Model Context Protocol (MCP) の通信およびツール連携に関する型モデルを定義する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPToolInfo:
    """MCPサーバーから返却されるツール情報"""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPToolInfo:
        schema = data.get("inputSchema") or data.get("parameters") or {}
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            input_schema=dict(schema) if isinstance(schema, dict) else {},
        )


@dataclass
class MCPToolCallResult:
    """MCPツール呼び出しの実行結果"""
    name: str
    success: bool
    content: list[dict[str, Any]] = field(default_factory=list)
    text_content: str = ""
    error: str | None = None
    raw: Any = None
