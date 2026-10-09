"""AIエージェント・ハーネス 観察合成器 (synthesizer.py)

フェーズ4. 結果の観察と評価（Observation）の出力部として、
複数の観察結果（Observation）や評価結果（EvaluationResult）を集約・要約し、
フェーズ2（推論・計画 Thought へのフィードバック）または
フェーズ5（最終応答 Final Response 生成プロンプト）へ注入する形式に合成する。
"""
from __future__ import annotations

import logging
from typing import Sequence

from .types import EvaluationResult, Observation

log = logging.getLogger("harness.agent.observation.synthesizer")


class ObservationSynthesizer:
    """観察結果の集約・要約・プロンプト合成器"""

    def __init__(self, max_total_chars: int = 6000, logger: logging.Logger | None = None) -> None:
        self.max_total_chars = max_total_chars
        self.log = logger or log

    def synthesize_for_thought(
        self,
        observations: Sequence[Observation],
        last_evaluation: EvaluationResult | None = None,
    ) -> str:
        """ステップ2（推論・計画 Thought）への差し戻しフィードバック用コンテキストを合成"""
        if not observations:
            return ""

        parts: list[str] = ["【これまでのツール実行結果と観察 (Observation)】"]
        for idx, obs in enumerate(observations, 1):
            parts.append(f"\n--- [実行 #{idx}: {obs.tool_name}] ---")
            parts.append(obs.to_prompt_context(max_length=1500))

        if last_evaluation and last_evaluation.rethink_prompt:
            parts.append("\n【評価器からの指示・再検討ポイント】")
            parts.append(last_evaluation.rethink_prompt)

        result = "\n".join(parts)
        if len(result) > self.max_total_chars:
            result = result[: self.max_total_chars] + "\n... (観察履歴が長すぎるため一部省略)"

        return result

    def synthesize_for_final_response(
        self,
        observations: Sequence[Observation],
        user_intent: str = "",
    ) -> str:
        """ステップ5（最終応答 Final Response）生成用の参照事実・知見ブロックを合成"""
        successful_obs = [obs for obs in observations if obs.success]
        if not successful_obs:
            return ""

        parts: list[str] = [
            "【ツールから取得した最新の外部情報 (Verified Facts)】",
            "※以下の確定事実に基づき、ユーザーの質問や指示に正確に応答してください。",
        ]

        for obs in successful_obs:
            parts.append(f"\n▼ [{obs.tool_name} の結果]")
            parts.append(obs.output.strip())

        result = "\n".join(parts)
        if len(result) > self.max_total_chars:
            result = result[: self.max_total_chars] + "\n... (一部省略)"

        return result
