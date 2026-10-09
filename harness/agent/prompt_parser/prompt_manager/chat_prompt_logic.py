"""
[AI Agent Summary]
chat_prompt_logicアグリゲーター。実装は harness.agent.prompt_parser.prompt_manager に集約されています。
"""
from __future__ import annotations

from typing import Any

from . import *
import harness.agent.prompt_parser.prompt_manager as _chat_prompt


def __getattr__(name: str) -> Any:
    return getattr(_chat_prompt, name)
