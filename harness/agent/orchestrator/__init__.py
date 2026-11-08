"""AIエージェント・ハーネス 1-2. オーケストレーター（フロー制御）パッケージ

5段階内部処理フロー（Context ➔ Thought ➔ Action ➔ Observation ➔ Final Response）
を自律制御・統括するエージェントオーケストレーションエンジンを提供する。
"""
from __future__ import annotations

from .engine import AgentOrchestrator
from .llm_client import DefaultLLMClient, LLMClientProtocol
from .types import AgentState, OrchestratorConfig, OrchestratorResult
from .loops import AgentLoopsMixin
from .monologue import AgentMonologueMixin
from .proactive import (
    AgentProactiveMixin,
    get_other_channel_message_counts,
    is_normal_reply_active,
    mark_normal_reply_active,
    mark_normal_reply_inactive,
    record_other_channel_message,
    reset_other_channel_message_count,
    roll_spontaneous_dice,
    should_respond_in_other_channel,
)
from .reflection import AgentReflectionMixin, safe_close_coroutine
from .agent_mixin import AgentOrchestrationMixin, OllamaChatAgentMixin

__all__ = [
    "AgentOrchestrator",
    "OrchestratorConfig",
    "OrchestratorResult",
    "AgentState",
    "DefaultLLMClient",
    "LLMClientProtocol",
    "AgentLoopsMixin",
    "AgentMonologueMixin",
    "AgentProactiveMixin",
    "AgentReflectionMixin",
    "AgentOrchestrationMixin",
    "OllamaChatAgentMixin",
    "roll_spontaneous_dice",
    "should_respond_in_other_channel",
    "get_other_channel_message_counts",
    "record_other_channel_message",
    "reset_other_channel_message_count",
    "is_normal_reply_active",
    "mark_normal_reply_active",
    "mark_normal_reply_inactive",
    "safe_close_coroutine",
]
