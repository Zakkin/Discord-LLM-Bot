"""
[AI Agent Summary]
後方互換性のためのchat_prompt_logicアグリゲーター。実装は ollama_bot.common.chat_prompt に移動しました。
Backward-compatibility aggregator for chat_prompt_logic. Implementation is moved to ollama_bot.common.chat_prompt.
"""
from __future__ import annotations

from typing import Any

from .chat_prompt import *
from . import chat_prompt as _chat_prompt


def __getattr__(name: str) -> Any:
    return getattr(_chat_prompt, name)
