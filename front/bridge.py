"""AgentBridge: front 層の中間表現と `harness/agent/` を橋渡しするブリッジ。

`AgentTask` を受け取り、`AgentOrchestrator.run(user_prompt=..., system_prompt=...,
context=...)` 互換のランナーへ渡し、結果を `AgentOutcome` / `FrontAction` に変換します。

依存方向を一方向に保つため、このモジュールは `harness` を直接 import せず、
`run(...)` を持つ任意のオブジェクト（`AgentRunner` プロトコル）を受け取ります。
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from front.adapter import PlatformAdapter
from front.ir import AgentOutcome, AgentTask, FrontAction

log = logging.getLogger("front.bridge")


class AgentRunner(Protocol):
    """`AgentOrchestrator.run` 互換の非同期ランナー。"""

    async def run(
        self,
        prompt: str = "",
        context: dict[str, Any] | str | None = None,
        *,
        user_prompt: str = "",
        system_prompt: str = "",
        extra_ctx: dict[str, Any] | None = None,
    ) -> Any:
        ...


def outcome_from_result(task: AgentTask, result: Any) -> AgentOutcome:
    """`OrchestratorResult` 互換オブジェクトを `AgentOutcome` に変換する。"""
    raw_text = getattr(result, "final_response", "")
    text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
    raw_success = getattr(result, "success", None)
    success = raw_success if isinstance(raw_success, bool) else bool(text)
    raw_error = getattr(result, "error", None)
    error = raw_error if isinstance(raw_error, str) and raw_error else None
    raw_turns = getattr(result, "total_turns", 0)
    turns = raw_turns if isinstance(raw_turns, int) else 0
    return AgentOutcome(
        task_id=task.task_id,
        success=success,
        text=text,
        error=error,
        total_turns=turns,
        raw=result,
    )


def default_reply_actions(task: AgentTask, outcome: AgentOutcome) -> list[FrontAction]:
    """結果テキストを発生元へ返信する既定のアクション列を生成する。"""
    text = outcome.text.strip()
    if not text or not task.constraints.allow_post:
        return [FrontAction(platform=task.event.platform, action="noop", target=task.event.location)]
    max_chars = task.constraints.max_chars
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    action = "post_reply" if task.event.platform == "hutaba" else "send_message"
    return [
        FrontAction(
            platform=task.event.platform,
            action=action,
            target=task.event.location,
            payload={"text": text},
            reply_to=task.event.event_id,
        )
    ]


class AgentBridge:
    """`AgentTask` を AgentOrchestrator 互換ランナーで実行するブリッジ。"""

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    async def run_task(self, task: AgentTask) -> AgentOutcome:
        """中間コード（AgentTask）をエージェントへ渡し、正規化した結果を返す。"""
        log.debug(
            "[front.bridge] run_task platform=%s intent=%s task_id=%s",
            task.event.platform,
            task.intent,
            task.task_id,
        )
        result = await self.runner.run(
            user_prompt=task.user_prompt,
            system_prompt=task.system_prompt,
            context=task.orchestrator_context(),
        )
        return outcome_from_result(task, result)

    async def handle(
        self,
        task: AgentTask,
        adapter: PlatformAdapter | None = None,
    ) -> tuple[AgentOutcome, list[FrontAction]]:
        """タスクを実行し、結果とプラットフォーム向けアクション列を返す。"""
        outcome = await self.run_task(task)
        if adapter is not None:
            actions = adapter.build_actions(task, outcome)
        else:
            actions = default_reply_actions(task, outcome)
        return outcome, actions


__all__ = [
    "AgentBridge",
    "AgentRunner",
    "default_reply_actions",
    "outcome_from_result",
]
