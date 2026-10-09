"""AIエージェント・ハーネス 内省観察評価器 (reflection_evaluator.py)

フェーズ4. 結果の観察と評価（Observation）において、
過去の対話ログ・行動結果を観察し、エージェント自身の行動や対話相手との関係性の評価（内省・信念形成）を行う。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .types import Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.reflection_evaluator")


@dataclass
class ReflectionEvaluation:
    """内省評価結果モデル"""
    target_user_name: str
    is_valid: bool
    summary: str
    rejection_reason: str = ""
    observation: Observation | None = None


class ReflectionEvaluator:
    """行動ログ・対話ログの内省観察評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def validate_reflection_belief(
        self,
        target_user_name: str,
        belief_text: str,
        evidence_count: int,
        incompatible_keywords: tuple[str, ...] | list[str] | None = None,
        *,
        strict_evidence: bool = True,
    ) -> ReflectionEvaluation:
        """内省によって形成された信念が妥当か観察・評価する。

        AGENTS.md 項目30: 定期内省における第三者名呼称誤認防止・バリデーション
        """
        import re

        text = (belief_text or "").strip()
        user_name = (target_user_name or "").strip()

        if not text:
            return ReflectionEvaluation(
                target_user_name=user_name,
                is_valid=False,
                summary="",
                rejection_reason="Empty belief text",
            )

        # 非互換キーワード
        if incompatible_keywords:
            if any(k in text for k in incompatible_keywords if k):
                return ReflectionEvaluation(
                    target_user_name=user_name,
                    is_valid=False,
                    summary=text,
                    rejection_reason="Contains incompatible keyword",
                )

        # 暴言・罵倒・誹謗中傷を受容・肯定・親密さの象徴とする不健全な信念を遮断
        if re.search(r"(?:罵倒|暴言|誹謗中傷|悪口|侮辱|挑発|煽り|攻撃|からかい).*(?:受け入れ|象徴|親密|仲良|好まし|自然|絆|愛着|深める|親愛|証)", text):
            msg = "Unhealthy belief: accepts or normalizes abuse/hostility"
            self.log.info("[reflection_evaluator] %s: %s", msg, text)
            return ReflectionEvaluation(
                target_user_name=user_name,
                is_valid=False,
                summary=text,
                rejection_reason=msg,
            )

        # 呼び方・呼称に関するルールの検証
        is_addressing_rule = bool(re.search(r"(?:呼ぶ|呼び名|呼称|あだ名|ニックネーム|呼び方|と名乗)", text))

        # 単発エビデンス（evidence_count < 2）の検証
        # strict_evidence=True: 全ての信念で単発エビデンスを却下
        # strict_evidence=False: 呼称ルールの場合のみ単発エビデンスを却下（一般的な文化・方針は1件でも許可）
        if evidence_count < 2 and (strict_evidence or is_addressing_rule):
            msg = f"Evidence count too low ({evidence_count} < 2); rejecting belief"
            self.log.info("[reflection_evaluator] %s", msg)
            return ReflectionEvaluation(
                target_user_name=user_name,
                is_valid=False,
                summary=text,
                rejection_reason=msg,
            )

        if is_addressing_rule and user_name:
            m = re.search(r"(?:相手|ユーザー|彼|彼女)?(?:を|の呼び方を)?(?:「([^」]+)」|『([^』]+)』|\"([^\"]+)\")と呼ぶ", text)
            if m:
                extracted_name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
                norm_extracted = re.sub(r"[\s・さんちゃん様君くん氏]", "", extracted_name).lower()
                norm_target = re.sub(r"[\s・さんちゃん様君くん氏]", "", user_name).lower()
                if norm_extracted and norm_target:
                    if norm_extracted not in norm_target and norm_target not in norm_extracted:
                        msg = f"Addressing rule misattribution: target={user_name!r} vs extracted={extracted_name!r}"
                        self.log.info("[reflection_evaluator] %s", msg)
                        return ReflectionEvaluation(
                            target_user_name=user_name,
                            is_valid=False,
                            summary=text,
                            rejection_reason=msg,
                        )



        obs = Observation(
            tool_name="reflection",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=text,
            data={"target_user": user_name, "evidence_count": evidence_count},
        )

        return ReflectionEvaluation(
            target_user_name=user_name,
            is_valid=True,
            summary=text,
            observation=obs,
        )

