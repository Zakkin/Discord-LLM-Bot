"""AIエージェント・ハーネス オーケストレーター型定義 (types.py)

1-2. オーケストレーター（フロー制御）において用いられる
エージェント状態モデル、設定、実行結果の型を定義する。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..observation.types import Observation


@dataclass
class OrchestratorConfig:
    """オーケストレーターの実行制御パラメータ"""
    max_turns: int = 5
    timeout_sec: float = 60.0
    think: bool = False
    model_name: str = ""
    temperature: float = 0.4
    enable_telemetry: bool = True
    auto_store_memory: bool = True


@dataclass
class AgentState:
    """エージェント実行セッションの動的コンテキスト状態"""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_prompt: str = ""
    system_prompt: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    current_turn: int = 0
    observations: list[Observation] = field(default_factory=list)
    is_finished: bool = False
    final_response: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str, **kwargs: Any) -> dict[str, Any]:
        """対話履歴にメッセージを追加"""
        msg: dict[str, Any] = {"role": role, "content": content, **kwargs}
        self.messages.append(msg)
        return msg


@dataclass
class OrchestratorResult:
    """オーケストレーターによる5段階フロー完了結果"""
    success: bool
    final_response: str
    total_turns: int
    observations: list[Observation] = field(default_factory=list)
    trace_id: str = ""
    error: str | None = None
    telemetry_summary: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
