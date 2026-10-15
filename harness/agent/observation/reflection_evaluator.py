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


@dataclass
class DeepReflectionEvaluation:
    """深層内省（Deep Reflection）観察評価結果モデル"""
    what_happened: str
    interpretation: str
    relationship_change: str
    future_policy: str
    is_valid: bool
    rejection_reason: str = ""
    observation: Observation | None = None


@dataclass
class ReflectionBatchEvaluation:
    """内省信念群の総合観察評価結果モデル"""
    total_candidates: int
    accepted_beliefs: list[dict[str, Any]]
    rejected_beliefs: list[dict[str, Any]]
    is_success: bool
    observation: Observation | None = None


class ReflectionEvaluator:
    """行動ログ・対話ログの内省観察評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def validate_reflection_belief(
        self,
        target_user_name: str,
        belief_text: str,
        evidence_count: int = 1,
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
        # 曖昧・抽象的な主語（ユーザーは/が等）の検知・リジェクトガード
        # （※ただし、明示的なIDタグ [uid:12345] や ユーザー（ID: 12345）等の形式は許可）
        generic_subject_match = re.match(
            r"^(?:\[uid:\d+\]\s*)?(?:(?:対象|この|当該)?ユーザー|(?:会話|対話)?相手|彼|彼女)(?:[（\(]ID:\s*[^）\)]+[）\)])?(?:は|が|の|への|にとって|との|には|に対して|に)",
            text,
        )
        if generic_subject_match:
            matched_prefix = generic_subject_match.group(0)
            has_explicit_id = bool(re.search(r"(?:\[uid:\d+\]|ID:\s*\d+)", matched_prefix))
            if not has_explicit_id:
                # user_name が指定されていない場合は「ユーザー」「相手」いずれも未解決としてリジェクト
                # user_name が指定されている場合でも「ユーザー」単体はリジェクト（個別内省では対象ニックネーム主語が必須）
                is_user_noun = bool(re.search(r"ユーザー", matched_prefix))
                if not user_name or is_user_noun:
                    msg = "Generic user subject not resolved"
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

    def evaluate_deep_reflection_result(
        self,
        raw_result: dict[str, Any] | None,
        *,
        max_policy_chars: int = 220,
    ) -> DeepReflectionEvaluation:
        """深層内省（Deep Reflection）でLLMが出力したJSON結果を観察・評価する。"""
        res = raw_result or {}
        what_happened = str(res.get("what_happened") or "").strip()
        interpretation = str(res.get("interpretation") or "").strip()
        relationship_change = str(res.get("relationship_change") or "").strip()
        future_policy = str(res.get("future_policy") or "").strip()

        if not future_policy:
            return DeepReflectionEvaluation(
                what_happened=what_happened,
                interpretation=interpretation,
                relationship_change=relationship_change,
                future_policy="",
                is_valid=False,
                rejection_reason="Empty future_policy",
            )

        policy_clipped = future_policy[:max_policy_chars]
        # 不健全な方針（暴言受容等）の遮断
        val_eval = self.validate_reflection_belief(
            target_user_name="",
            belief_text=policy_clipped,
            evidence_count=2,
            strict_evidence=False,
        )
        if not val_eval.is_valid:
            return DeepReflectionEvaluation(
                what_happened=what_happened,
                interpretation=interpretation,
                relationship_change=relationship_change,
                future_policy=policy_clipped,
                is_valid=False,
                rejection_reason=val_eval.rejection_reason,
            )

        obs = Observation(
            tool_name="deep_reflection",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=policy_clipped,
            data={
                "what_happened": what_happened,
                "interpretation": interpretation,
                "relationship_change": relationship_change,
                "future_policy": policy_clipped,
            },
        )
        return DeepReflectionEvaluation(
            what_happened=what_happened,
            interpretation=interpretation,
            relationship_change=relationship_change,
            future_policy=policy_clipped,
            is_valid=True,
            observation=obs,
        )

    def evaluate_reflection_beliefs(
        self,
        beliefs: list[dict[str, Any]],
        *,
        target_user_name: str | None = None,
        target_user_id: int | str | None = None,
        max_beliefs: int = 5,
        incompatible_keywords: tuple[str, ...] | list[str] | None = None,
    ) -> ReflectionBatchEvaluation:
        """LLMが出力した定期内省の信念候補リストを観察・評価・フィルタリングする。"""
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for item in (beliefs or [])[:max_beliefs]:
            concept = str(item.get("concept") or "").strip()
            belief = str(item.get("belief") or "").strip()
            if not concept or not belief:
                rejected.append({**item, "rejection_reason": "Missing concept or belief"})
                continue

            evidence_count = int(item.get("evidence_count") or 0)
            importance_score = float(item.get("importance_score") or 0.0)

            # 信念テキストの評価
            val = self.validate_reflection_belief(
                target_user_name=target_user_name or "",
                belief_text=belief,
                evidence_count=evidence_count,
                incompatible_keywords=incompatible_keywords,
                strict_evidence=False,
            )
            if not val.is_valid:
                self.log.info(
                    "[reflection_evaluator] belief rejected: user=%s count=%d reason=%s belief=%s",
                    target_user_name or target_user_id or "channel",
                    evidence_count,
                    val.rejection_reason,
                    belief,
                )
                rejected.append({**item, "rejection_reason": val.rejection_reason})
                continue

            accepted.append({
                "concept": concept[:80],
                "belief": belief[:220],
                "importance_score": importance_score,
                "evidence_count": evidence_count,
            })

        is_success = len(accepted) > 0
        obs = Observation(
            tool_name="reflection_batch",
            success=is_success,
            status=ObservationStatus.SUCCESS if is_success else ObservationStatus.EMPTY_RESULT,
            output=f"Accepted {len(accepted)} / {len(beliefs or [])} beliefs",
            data={"accepted": accepted, "rejected": rejected},
        )
        return ReflectionBatchEvaluation(
            total_candidates=len(beliefs or []),
            accepted_beliefs=accepted,
            rejected_beliefs=rejected,
            is_success=is_success,
            observation=obs,
        )


# グローバル共有インスタンス
_default_reflection_evaluator = ReflectionEvaluator()

# モジュール関数としてのエクスポート
validate_reflection_belief = _default_reflection_evaluator.validate_reflection_belief
evaluate_deep_reflection_result = _default_reflection_evaluator.evaluate_deep_reflection_result
evaluate_reflection_beliefs = _default_reflection_evaluator.evaluate_reflection_beliefs

