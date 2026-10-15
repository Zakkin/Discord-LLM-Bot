"""AIエージェント・ハーネス ツール登録デコレータ (decorators.py)

Python関数を手軽に ToolDefinition 化・レジストリ登録するためのデコレータを提供する。
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from .registry import ToolRegistry
from .types import ToolCategory, ToolDefinition, ToolParameter

F = TypeVar("F", bound=Callable[..., Any])

_GLOBAL_REGISTRY: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    """グローバル共有のデフォルト ToolRegistry を取得する"""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ToolRegistry()
    if not _GLOBAL_REGISTRY.has("search_web"):
        try:
            from .adapters.web_search import WebSearchToolAdapter
            _GLOBAL_REGISTRY.register(WebSearchToolAdapter.create_tool())
        except Exception:
            pass
    return _GLOBAL_REGISTRY


def set_default_registry(registry: ToolRegistry) -> None:
    """グローバル共有のデフォルト ToolRegistry を設定する"""
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = registry


def tool(
    name: str | None = None,
    description: str | None = None,
    category: ToolCategory = ToolCategory.CUSTOM,
    timeout_sec: float = 30.0,
    parameters: list[ToolParameter] | None = None,
    tags: set[str] | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[[F], F]:
    """関数を ToolDefinition として定義し、指定レジストリに自動登録するデコレータ。

    使用例:
        @tool(description="足し算を実行する")
        def add(a: int, b: int) -> int:
            return a + b
    """
    def decorator(func: F) -> F:
        target_registry = registry or get_default_registry()
        tool_name = name or func.__name__
        tool_desc = description or (func.__doc__ or "").strip() or f"Execute {tool_name}"

        if parameters is not None:
            tool_def = ToolDefinition(
                name=tool_name,
                description=tool_desc,
                parameters=parameters,
                handler=func,
                category=category,
                tags=tags or set(),
                timeout_sec=timeout_sec,
            )
            target_registry.register(tool_def)
        else:
            tool_def = target_registry.register_function(
                func=func,
                name=tool_name,
                description=tool_desc,
                category=category,
                timeout_sec=timeout_sec,
                tags=tags,
            )

        # 関数自体にツール定義属性を付与
        setattr(func, "__tool_definition__", tool_def)
        return func

    return decorator
