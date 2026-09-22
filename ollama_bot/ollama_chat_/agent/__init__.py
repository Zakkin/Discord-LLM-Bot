"""
[AI Agent Summary]
agent ディレクトリは、自律エージェント行動、内省、内部欲求更新、自発発話、定期ループなどを管理するモジュール群です。
The agent directory is a collection of modules managing autonomous agent behaviors, inner monologue/reflection, need updates, proactive speech, and background loops.
"""
from __future__ import annotations

from .core_mixin import OllamaChatAgentMixin
from .loops import _AgentLoopsMixin
from .monologue import (
    _AGENT_MONOLOGUE_SCHEMA,
    _AGENT_TOPIC_SCHEMA,
    _CHAT_SPECIAL_TOKEN_RE,
    _AgentMonologueMixin,
    _clean_agent_monologue,
    _clean_agent_topic,
    _normalize_agent_topic_text,
    _topic_compare_key,
    _topic_matches_memory,
)
from .proactive import _AgentProactiveMixin
from .reflection import _AgentReflectionMixin

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
