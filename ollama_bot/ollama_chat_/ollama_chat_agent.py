"""自律エージェント行動、内省、内部欲求更新、定期発話を担当するMixin（互換レイヤー）。

ロジックはすべて `harness.agent.orchestrator` へ完全移行されました。
"""
from __future__ import annotations

from harness.agent.orchestrator.agent_mixin import (
    AgentOrchestrationMixin as OllamaChatAgentMixin,
    _AgentLoopsMixin,
    _AgentMonologueMixin,
    _AgentProactiveMixin,
    _AgentReflectionMixin,
)
from harness.agent.orchestrator.monologue import (
    _AGENT_MONOLOGUE_SCHEMA,
    _AGENT_TOPIC_SCHEMA,
    _CHAT_SPECIAL_TOKEN_RE,
    _clean_agent_monologue,
    _clean_agent_topic,
    _normalize_agent_topic_text,
    _topic_compare_key,
    _topic_matches_memory,
)

__all__ = [
    "OllamaChatAgentMixin",
    "_AgentMonologueMixin",
    "_AgentProactiveMixin",
    "_AgentReflectionMixin",
    "_AgentLoopsMixin",
    "_AGENT_TOPIC_SCHEMA",
    "_AGENT_MONOLOGUE_SCHEMA",
    "_CHAT_SPECIAL_TOKEN_RE",
    "_normalize_agent_topic_text",
    "_topic_compare_key",
    "_topic_matches_memory",
    "_clean_agent_topic",
    "_clean_agent_monologue",
]
