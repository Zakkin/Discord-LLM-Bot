"""AIエージェント・ハーネス 観察ルーター (router.py)

フェーズ4. 結果の観察と評価（Observation）における状態遷移（ルーティング）を担当。
ツール実行結果と評価結果（EvaluationResult）、ループ検知結果を総合的に分析し、
- 情報不足 / エラーのためフェーズ2（Thought）へ巻き戻す (RETRY_WITH_THOUGHT)
- 情報が揃ったためフェーズ5（Final Response）へ進む (PROCEED_TO_FINAL)
- 修復不能なエラー / ループ検出のため中断する (FATAL_ERROR)
の安全な遷移制御と、無限ループの未然防止を提供する。
"""
from __future__ import annotations

import logging
from typing import Sequence

from .types import (
    EvaluationResult,
    EvaluationVerdict,
    NextStepPhase,
    Observation,
    ObservationRoutingResult,
    ObservationStatus,
)

log = logging.getLogger("harness.agent.observation.router")


class ObservationRouter:
    """観察結果から次フェーズへの状態遷移を安全に決定するルーター"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def route(
        self,
        observations: Sequence[Observation],
        evaluations: Sequence[EvaluationResult],
        current_turn: int = 1,
        max_turns: int = 5,
    ) -> ObservationRoutingResult:
        """観察結果と評価結果を総合し、次フェーズへの状態遷移を決定する。

        Args:
            observations: 実行された全ツールの観察結果
            evaluations: 各ツールの評価結果
            current_turn: 現在の実行ターン数
            max_turns: セッションの最大許可ターン数

        Returns:
            ObservationRoutingResult: ルーティング先およびフィードバック情報
        """
        obs_list = list(observations)
        eval_list = list(evaluations)

        # ツール呼び出しが全くなかった場合 ➔ 直接フェーズ5へ
        if not obs_list:
            self.log.info("[router] No observations present. Proceeding to final response.")
            return ObservationRoutingResult(
                next_phase=NextStepPhase.PROCEED_TO_FINAL,
                observations=obs_list,
                evaluations=eval_list,
                all_goals_achieved=True,
            )

        # 1. 致命的エラー・安全停止の最優先チェック
        for ev in eval_list:
            if ev.verdict == EvaluationVerdict.FATAL_ERROR:
                reason = ev.reason or "エラーループまたは修復不能なエラーが検知されました。"
                self.log.warning("[router] Fatal condition triggered: %s", reason)
                return ObservationRoutingResult(
                    next_phase=NextStepPhase.FATAL_ERROR,
                    observations=obs_list,
                    evaluations=eval_list,
                    all_goals_achieved=False,
                    feedback_prompt=ev.rethink_prompt,
                    error_message=reason,
                    is_loop_detected=ev.error_loop_detected,
                )

        # 2. 最大ターン数到達チェック（無限ループ強制遮断）
        if current_turn >= max_turns:
            msg = f"最大試行ターン数（{max_turns}ターン）に達したため、ツール実行を安全に終了します。"
            self.log.warning("[router] %s", msg)
            return ObservationRoutingResult(
                next_phase=NextStepPhase.PROCEED_TO_FINAL,  # これ以上ループせず、現在情報で回答へ
                observations=obs_list,
                evaluations=eval_list,
                all_goals_achieved=False,
                error_message=msg,
                is_loop_detected=True,
            )

        # 3. 目標達成度の判定
        all_goals_achieved = True
        rethink_prompts: list[str] = []
        partial_successes: list[str] = []
        failed_tools: list[str] = []

        for obs, ev in zip(obs_list, eval_list):
            if ev.is_goal_achieved and obs.success:
                partial_successes.append(f"ツール '{obs.tool_name}' は正常に実行完了しました。")
            else:
                all_goals_achieved = False
                failed_tools.append(obs.tool_name)
                if ev.rethink_prompt:
                    rethink_prompts.append(ev.rethink_prompt)

        # 4. 全ての目標が達成された場合 ➔ フェーズ5（Final Response）へ進行
        if all_goals_achieved:
            self.log.info(
                "[router] All %d goals achieved in turn %d. Routing to Phase 5 (Final Response)",
                len(obs_list),
                current_turn,
            )
            return ObservationRoutingResult(
                next_phase=NextStepPhase.PROCEED_TO_FINAL,
                observations=obs_list,
                evaluations=eval_list,
                all_goals_achieved=True,
            )

        # 5. 未達成またはエラーがある場合 ➔ フェーズ2（Thought）へ巻き戻し再試行
        self.log.info(
            "[router] Goals not satisfied (failed tools: %s). Routing back to Phase 2 (Thought)",
            ", ".join(failed_tools),
        )

        feedback_parts: list[str] = []
        if partial_successes and failed_tools:
            feedback_parts.append("【一部ツールの実行成功】\n" + "\n".join(partial_successes))
        if rethink_prompts:
            feedback_parts.append("【再検討・修正指示】\n" + "\n".join(rethink_prompts))
        else:
            feedback_parts.append(
                f"ツール {', '.join(failed_tools)} の結果が不十分です。引数や検索クエリを調整して再試行してください。"
            )

        combined_feedback = "\n\n".join(feedback_parts)
        is_loop = any(ev.error_loop_detected for ev in eval_list)

        return ObservationRoutingResult(
            next_phase=NextStepPhase.RETRY_WITH_THOUGHT,
            observations=obs_list,
            evaluations=eval_list,
            all_goals_achieved=False,
            feedback_prompt=combined_feedback,
            is_loop_detected=is_loop,
        )
