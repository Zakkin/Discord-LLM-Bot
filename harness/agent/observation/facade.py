"""AIエージェント・ハーネス 観察・再試行ファサード (facade.py)

フェーズ4. 結果の観察と評価（Observation）の統合エントリーポイント。
オーケストレーターと Observation パッケージ内部の各コンポーネント（Parser, Memory, Evaluator,
LoopDetector, Router, Synthesizer）間の結合度を下げ、Facade パターンにより
一元的かつクリーンなインターフェースを提供する。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from .evaluator import ObservationEvaluator
from .loop_detector import LoopDetector
from .memory import ScratchpadManager
from .parser import ObservationParser
from .router import ObservationRouter
from .synthesizer import ObservationSynthesizer
from .types import (
    EvaluationResult,
    NextStepPhase,
    Observation,
    ObservationRoutingResult,
)

log = logging.getLogger("harness.agent.observation.facade")


class ObservationFacade:
    """フェーズ4（観察と再試行）の全責務を統括・実行するファサードクラス"""

    def __init__(
        self,
        parser: ObservationParser | None = None,
        scratchpad: ScratchpadManager | None = None,
        loop_detector: LoopDetector | None = None,
        evaluator: ObservationEvaluator | None = None,
        router: ObservationRouter | None = None,
        synthesizer: ObservationSynthesizer | None = None,
        max_turns: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or log
        self.parser = parser or ObservationParser()
        self.scratchpad = scratchpad or ScratchpadManager()
        self.loop_detector = loop_detector or LoopDetector(max_total_turns=max_turns)
        self.evaluator = evaluator or ObservationEvaluator(loop_detector=self.loop_detector)
        self.router = router or ObservationRouter()
        self.synthesizer = synthesizer or ObservationSynthesizer()

    def reset(self) -> None:
        """新規セッション開始時の状態リセット"""
        self.loop_detector.reset()
        self.scratchpad.reset()

    def handle_turn_observations(
        self,
        executed_results: Sequence[tuple[Any, Any]],  # [(ParsedToolCall | Any, ToolResult | Any), ...]
        messages: list[dict[str, Any]],
        observations_history: list[Observation],
        user_intent: str = "",
        turn_index: int = 1,
        max_turns: int = 5,
    ) -> ObservationRoutingResult:
        """1ターンのツール実行結果群を観察・記録・評価し、次フェーズへのルーティングを決定する。

        処理ステップ:
        1. [Parser] 各ツール実行結果を堅牢にパースして Observation を生成
        2. [Memory] スクラッチパッドへの記録、および対話履歴(messages)へ tool メッセージ追記
        3. [Evaluator] 観察結果の評価・ループ検知
        4. [Router] 次フェーズへの遷移判定 (PROCEED_TO_FINAL / RETRY_WITH_THOUGHT / FATAL_ERROR)
        5. [Memory] 再試行が必要な場合、フィードバックプロンプトを対話履歴へ追記

        Args:
            executed_results: ツール呼び出し情報と実行結果のタプルリスト
            messages: エージェントの対話履歴（OpenAI形式のリスト、直接更新される）
            observations_history: セッション全体の観察履歴リスト
            user_intent: ユーザーの要求・プロンプト
            turn_index: 現在のターン番号
            max_turns: 最大ターン数

        Returns:
            ObservationRoutingResult: ルーティング判定結果
        """
        turn_observations: list[Observation] = []
        turn_evaluations: list[EvaluationResult] = []

        self.log.info(
            "[facade] Processing turn #%d observations (total tool results: %d)",
            turn_index,
            len(executed_results),
        )

        for call_info, res in executed_results:
            call_id = getattr(call_info, "call_id", "") or ""
            tool_name = getattr(call_info, "name", "") or getattr(res, "name", "") or "unknown_tool"
            arguments = getattr(call_info, "arguments", {}) or {}

            # 1. パース (Parser)
            obs = self.parser.parse(
                raw_result=res,
                tool_name=tool_name,
                call_id=call_id,
            )
            turn_observations.append(obs)
            observations_history.append(obs)

            # 2. 短期記憶・メッセージ更新 (Memory)
            self.scratchpad.append_observation_to_messages(messages, obs)

            # 3. 評価 (Evaluator)
            eval_res = self.evaluator.evaluate(
                observation=obs,
                user_intent=user_intent,
                tool_arguments=arguments,
                current_turn=turn_index,
            )
            turn_evaluations.append(eval_res)

            # スクラッチパッドへのエントリ記録
            self.scratchpad.record_entry(
                turn=turn_index,
                tool_name=tool_name,
                arguments=arguments,
                observation=obs,
                verdict=eval_res.verdict,
            )

        # 4. ルーティング (Router)
        routing_res = self.router.route(
            observations=turn_observations,
            evaluations=turn_evaluations,
            current_turn=turn_index,
            max_turns=max_turns,
        )

        # 5. 再試行時のフィードバック注入 (Memory)
        if routing_res.next_phase == NextStepPhase.RETRY_WITH_THOUGHT and routing_res.feedback_prompt:
            self.scratchpad.append_feedback_to_messages(
                messages=messages,
                rethink_prompts=[routing_res.feedback_prompt],
            )

        return routing_res

    def synthesize_for_final_response(
        self,
        observations: Sequence[Observation],
        user_intent: str = "",
    ) -> str:
        """フェーズ5（最終応答生成）用コンテキスト合成"""
        return self.synthesizer.synthesize_for_final_response(observations, user_intent)

    def synthesize_for_thought(
        self,
        observations: Sequence[Observation],
        last_evaluation: EvaluationResult | None = None,
    ) -> str:
        """フェーズ2（推論・計画 Thought）用コンテキスト合成"""
        return self.synthesizer.synthesize_for_thought(observations, last_evaluation)


# 後方互換性エイリアス
ObservationManager = ObservationFacade
