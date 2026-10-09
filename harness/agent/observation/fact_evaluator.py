"""AIエージェント・ハーネス ファクトチェック観察評価器 (fact_evaluator.py)

フェーズ4. 結果の観察と評価（Observation）において、
ファクトチェックやURL展開・裏取りツールの結果を観察し、
事実性の判定（True / False / Unverifiable / Opinion）および信頼度スコアを評価する。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .types import EvaluationVerdict, Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.fact_evaluator")


class FactCheckVerdict(str, Enum):
    """ファクトチェック判定区分"""
    TRUE = "true"                        # 事実・正確
    FALSE = "false"                      # 誤り・デマ・虚偽
    PARTIALLY_TRUE = "partially_true"    # 一部事実だが文脈相違や誇張あり
    UNVERIFIABLE = "unverifiable"        # 情報不足・裏取り不能
    OPINION_OR_SPECULATION = "opinion"   # 個人の主観・憶測


@dataclass
class FactCheckEvaluation:
    """ファクトチェック結果の評価モデル"""
    claim: str
    verdict: FactCheckVerdict
    confidence: float
    summary: str
    sources: list[str]
    observation: Observation


class FactCheckEvaluator:
    """ファクトチェック結果の観察・評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def evaluate_fact_check_result(
        self,
        claim: str,
        research_output: str,
        sources: list[str] | None = None,
    ) -> FactCheckEvaluation:
        """調査結果を観察し、ファクトチェック判定を評価・導出する。"""
        srcs = list(sources or [])
        text = (research_output or "").strip()

        # ルールベースでの簡易判定シグナル
        is_empty = not text or "見つかりませんでした" in text
        if is_empty:
            obs = Observation(
                tool_name="fact_check",
                success=True,
                status=ObservationStatus.EMPTY_RESULT,
                output="十分な検証情報が見つかりませんでした。",
            )
            return FactCheckEvaluation(
                claim=claim,
                verdict=FactCheckVerdict.UNVERIFIABLE,
                confidence=0.0,
                summary="裏付けとなる情報源が見つかりませんでした。",
                sources=srcs,
                observation=obs,
            )

        # 真偽キーワードの検出
        lower = text.lower()
        if re.search(r"(?:誤り|デマ|虚偽|事実ではない|嘘|デマ情報|フェイク)", text):
            verdict = FactCheckVerdict.FALSE
            confidence = 0.85
        elif re.search(r"(?:事実|公式発表|確認された|正しい|真実|事実と一致)", text):
            verdict = FactCheckVerdict.TRUE
            confidence = 0.85
        elif re.search(r"(?:推測|見方もある|意見|個人の感想)", text):
            verdict = FactCheckVerdict.OPINION_OR_SPECULATION
            confidence = 0.7
        else:
            verdict = FactCheckVerdict.PARTIALLY_TRUE
            confidence = 0.6

        obs = Observation(
            tool_name="fact_check",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=text,
            data={"claim": claim, "verdict": verdict.value, "confidence": confidence},
        )

        return FactCheckEvaluation(
            claim=claim,
            verdict=verdict,
            confidence=confidence,
            summary=text[:500],
            sources=srcs,
            observation=obs,
        )
