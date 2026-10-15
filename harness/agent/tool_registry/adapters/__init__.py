"""AIエージェント・ハーネス ツールアダプターパッケージ (adapters)"""
from __future__ import annotations

from .fact_check import FactCheckToolAdapter
from .mcp import MCPToolAdapter
from .special_action import (
    SpecialActionAdapter,
    SpecialActionToolAdapter,
    fallback_extract_keywords,
    is_special_action_parrot,
    is_special_action_query_hijacked,
    sanitize_special_action_query_hallucinations,
)
from .weather import WeatherToolAdapter
from .web_search import WebSearchToolAdapter

__all__ = [
    "WebSearchToolAdapter",
    "WeatherToolAdapter",
    "FactCheckToolAdapter",
    "MCPToolAdapter",
    "SpecialActionToolAdapter",
    "SpecialActionAdapter",
    "is_special_action_parrot",
    "is_special_action_query_hijacked",
    "sanitize_special_action_query_hallucinations",
    "fallback_extract_keywords",
]

