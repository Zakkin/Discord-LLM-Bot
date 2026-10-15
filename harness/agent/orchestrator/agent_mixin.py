"""AIエージェント・ハーネス オーケストレーター 自律エージェント統合Mixin (orchestrator/agent_mixin.py)

定期ループ、内部モノローグ、自発発話、内省の各Mixinを統合し、外部へ提供する。
"""
from __future__ import annotations

from .loops import AgentLoopsMixin, _AgentLoopsMixin
from .monologue import AgentMonologueMixin, _AgentMonologueMixin
from .proactive import AgentProactiveMixin, _AgentProactiveMixin
from .reflection import AgentReflectionMixin, _AgentReflectionMixin


class AgentOrchestrationMixin(
    AgentMonologueMixin,
    AgentProactiveMixin,
    AgentReflectionMixin,
    AgentLoopsMixin,
):
    """自律エージェント行動、内省、内部欲求更新、自発発話、定期ループを担当する統合Mixin"""
    pass


OllamaChatAgentMixin = AgentOrchestrationMixin
