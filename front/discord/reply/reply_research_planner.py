"""Pre-reply Analyzer LLM の意図判定とルールベースのリサーチ決定を調停するプランナーモジュール。

Pre-reply Analyzer LLM が明確に external_research=False（調査不要）と判断した場合、
ルール側の単語一致フラグによる不要な Web 検索を抑止し、通常返答生成フローへ安全にフォールバックさせる。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("front.discord.reply_research_planner")


def should_suppress_research_by_llm_intent(
    intent_info: dict[str, Any] | None,
    research_decision: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Pre-reply Analyzer LLM の判定に基づき、リサーチを抑止すべきかを判定する。

    Args:
        intent_info: Pre-reply Analyzer LLM が返した意図解析辞書
        research_decision: ルールやリサーチ決定モデルが判定したリサーチ決定辞書

    Returns:
        (suppress_research: bool, reason: str)
    """
    if not intent_info or not isinstance(intent_info, dict):
        return False, ""

    decision = research_decision or {}

    # 明示的なURL指定や画像スレッド等の直接URL処理はLLM判定に関わらず許可
    if decision.get("url_present") or decision.get("mode") in {"img_thread", "browser_read_url"}:
        return False, ""

    # 困りごと・意見募集・解決策模索（consultation_flag）は解決ヒントのWeb調査が必要なため抑制しない
    if decision.get("consultation_flag") or "consultation_problem_solving" in (decision.get("rule_reasons") or []):
        return False, ""

    # Pre-reply Analyzer LLM の external_research 判定を検証
    has_external_research = "external_research" in intent_info
    has_needs_external = "needs_external_research" in intent_info

    external_val = intent_info.get("external_research")
    if external_val is None:
        external_val = intent_info.get("needs_external_research")

    # LLM が明確に False（調査不要）と判定している場合
    if (has_external_research or has_needs_external) and external_val is False:
        rule_reasons = decision.get("rule_reasons") or []
        reasons_str = ", ".join(str(r) for r in rule_reasons) if rule_reasons else "keyword match"
        reason = f"Pre-reply Analyzer LLM explicitly determined external_research=False (suppressing rules: {reasons_str})"
        return True, reason

    return False, ""


def reconcile_research_decision(
    research_decision: dict[str, Any],
    intent_info: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Pre-reply Analyzer LLM の判定に基づき、research_decision を調停・更新する。

    Args:
        research_decision: 現行のリサーチ決定辞書
        intent_info: Pre-reply Analyzer LLM が返した意図解析辞書

    Returns:
        (updated_research_decision: dict, needs_research: bool)
    """
    decision = dict(research_decision or {})
    needs_research = bool(decision.get("needs_research"))

    if not needs_research:
        return decision, False

    suppress, reason = should_suppress_research_by_llm_intent(intent_info, decision)
    if suppress:
        log.info(
            "[RESEARCH_PLANNER] Pre-reply Analyzer LLM decided external_research=False; suppressing rule-based research trigger: %s",
            reason,
        )
        decision["needs_research"] = False
        decision["suppressed_by_llm"] = True
        decision["suppression_reason"] = reason
        decision["provisional_reply"] = ""
        return decision, False

    return decision, needs_research
