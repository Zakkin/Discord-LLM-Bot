"""
web_research/rules.py

ウェブリサーチのルールベース判定・意図解析・モード解決を管理するモジュール。
主な責務:
- リサーチ要否判定ルールの定義・評価 (detect_reply_research_rules)
- 直前調査の深掘り追従判定 (detect_deepdive_followup)
- 用語・話題の調査要否分類 (classify_research_need)
- リサーチ実行モード解決 (resolve_research_mode)
- 作品・トピック評判質問の判定 (detect_topic_opinion_inquiry)
"""

from __future__ import annotations

import logging
from typing import Any

from .config import ResearchDispatchMode
from .helpers import (
    classify_research_need,
    detect_deepdive_followup,
    detect_reply_research_rules,
    extract_explicit_research_term,
    extract_named_term_candidates,
    resolve_research_mode,
)
from .topic_investigation import (
    detect_topic_opinion_inquiry,
)

log = logging.getLogger("harness.agent.tool_registry.web_research.rules")


class WebResearchRules:
    """ウェブリサーチ判定ルールのファサードクラス"""

    @staticmethod
    def detect_rules(user_text: str, context: str = "") -> dict[str, Any]:
        """ユーザー入力および会話文脈からリサーチ判定ルールを評価する"""
        return detect_reply_research_rules(user_text, context)

    @staticmethod
    def detect_deepdive(
        user_text: str,
        last_research_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """直前の調査結果に対する深掘り質問かどうかを判定する"""
        return detect_deepdive_followup(user_text, last_research_cache)

    @staticmethod
    def classify(user_text: str, context: str = "") -> dict[str, Any]:
        """リサーチの必要性・モード・検索クエリを分類する"""
        return classify_research_need(user_text, context)

    @staticmethod
    def resolve_mode(
        user_text: str,
        context: str = "",
        mode: ResearchDispatchMode = "auto",
    ) -> ResearchDispatchMode:
        """指定されたモードまたは自動判定からリサーチ実行モードを決定する"""
        return resolve_research_mode(user_text, context=context, mode=mode)

    @staticmethod
    def detect_topic_opinion(user_text: str) -> dict[str, Any]:
        """作品やトピックに対する世間の評価・評判の質問かどうかを判定する"""
        return detect_topic_opinion_inquiry(user_text)


__all__ = [
    "ResearchDispatchMode",
    "WebResearchRules",
    "detect_reply_research_rules",
    "detect_deepdive_followup",
    "classify_research_need",
    "resolve_research_mode",
    "extract_explicit_research_term",
    "extract_named_term_candidates",
    "detect_topic_opinion_inquiry",
]
