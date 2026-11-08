"""
[AI Agent Summary]
Memory Manager内部で利用する互換ユーティリティ（JSON, ISO時刻, ollama_helpers動的解決）。
Internal compatibility utilities for memory manager (JSON parsing/serializing, UTC ISO timestamp, dynamic ollama_helpers lookup).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from typing import Any

try:
    import orjson

    def json_loads(data: str | bytes | bytearray | memoryview) -> Any:
        return orjson.loads(data)

    def json_dumps(value: Any, *, ensure_ascii: bool = False) -> str:
        if not ensure_ascii:
            return orjson.dumps(value).decode("utf-8")
        return json.dumps(value, ensure_ascii=ensure_ascii)

except ImportError:
    def json_loads(data: str | bytes | bytearray | memoryview) -> Any:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return json.loads(data)

    def json_dumps(value: Any, *, ensure_ascii: bool = False) -> str:
        return json.dumps(value, ensure_ascii=ensure_ascii)


def utcnow_iso() -> str:
    """現在時刻のUTC ISO 8601文字列を返す。"""
    return datetime.now(timezone.utc).isoformat()


def get_ollama_helpers() -> Any:
    """テストモックやパッチに追従するため、動的に ollama_helpers を解決する。"""
    for mod_name in (
        "harness.agent.memory_manager.logic",
        "harness.agent.memory_manager.extractor",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "ollama_helpers"):
            candidate = getattr(mod, "ollama_helpers")
            if candidate is not None:
                return candidate
    from ollama_bot.common import ollama_helpers
    return ollama_helpers
