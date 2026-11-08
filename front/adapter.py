"""プラットフォームアダプターのインターフェース定義。

各プラットフォーム（`front/discord`, `front/hutaba`, `front/x`）は、
プラットフォーム固有オブジェクトと中間表現（`front.ir`）の相互変換を担う
アダプターを提供します。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from front.ir import AgentOutcome, AgentTask, FrontAction


@runtime_checkable
class PlatformAdapter(Protocol):
    """front 層のプラットフォームアダプターが満たすべきプロトコル。"""

    platform: str

    def build_actions(self, task: AgentTask, outcome: AgentOutcome) -> list[FrontAction]:
        """エージェントの実行結果をプラットフォーム向けアクションへ変換する。"""
        ...

    async def execute(self, action: FrontAction) -> Any:
        """アクションを実際のプラットフォームへ出力する。"""
        ...


__all__ = ["PlatformAdapter"]
