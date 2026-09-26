"""
[AI Agent Summary]
後方互換性のためのollama_helpersアグリゲーター。実装は ollama_bot.common.ollama に移動しました。
Backward-compatibility aggregator for ollama_helpers. Implementation is moved to ollama_bot.common.ollama.
"""
from __future__ import annotations

from typing import Any

from .ollama import *
from . import ollama as _ollama


def __getattr__(name: str) -> Any:
    return getattr(_ollama, name)
