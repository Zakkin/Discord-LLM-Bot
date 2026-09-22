"""
[AI Agent Summary]
分割された各 Agent Mixin クラスを束ね、外部から統合された `OllamaChatAgentMixin` として利用するためのエントリーポイント。
This module bundles all the separated agent mixin classes and provides the unified `OllamaChatAgentMixin` entry point.
"""
from __future__ import annotations

from .loops import _AgentLoopsMixin
from .monologue import _AgentMonologueMixin
from .proactive import _AgentProactiveMixin
from .reflection import _AgentReflectionMixin


class OllamaChatAgentMixin(
    _AgentMonologueMixin,
    _AgentProactiveMixin,
    _AgentReflectionMixin,
    _AgentLoopsMixin,
):
    """自律エージェント行動、内省、内部欲求更新、自発発話、定期ループを担当する統合Mixin。"""
    pass
