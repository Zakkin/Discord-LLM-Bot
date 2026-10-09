"""AIエージェント・ハーネス フィードバック観察器 (feedback_observer.py)

フェーズ4. 結果の観察と評価（Observation）において、
外部環境（Discordユーザーのリアクションや評価アクション等）からのフィードバックを観察し、
エージェントの行動評価スコア（好意・親愛・不快・警告）を算出する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .types import Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.feedback_observer")

# ポジティブ・ネガティブリアクションの定義
_POSITIVE_EMOJIS = {"👍", "❤️", "🥰", "🎉", "👏", "✨", "😊", "💖", "🙏"}
_NEGATIVE_EMOJIS = {"👎", "😡", "😠", "💢", "🙄", "🤮", "❌"}
_CONFUSED_EMOJIS = {"❓", "❔", "🤔", "🧐", "😵‍💫"}


FEEDBACK_KEYWORDS = (
    "不自然",
    "日本語がおかしい",
    "日本語おかしい",
    "日本語を",
    "その表現",
    "その言い方",
    "その返し",
    "今の返し変",
    "変だ",
    "多用しすぎ",
    "おかしい",
    "やめて",
    "言い換え",
    "訂正",
    "直して",
    "もっと自然",
)


@dataclass
class FeedbackObservation:
    """ユーザーからのフィードバック観察結果"""
    user_id: int
    emoji: str
    sentiment_delta: float  # -1.0 〜 +1.0
    feedback_type: str      # positive / negative / confusion / neutral / style_problem / unnatural_japanese / etc.
    observation: Observation


class FeedbackObserver:
    """外部リアクション・フィードバックの観察評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def looks_like_feedback_text(self, text: str) -> bool:
        """テキストがBotへの指摘・フィードバック（訂正・不自然指摘等）か観察・判定する。"""
        t = (text or "").strip()
        return bool(t) and any(k in t for k in FEEDBACK_KEYWORDS)

    def infer_feedback_type(self, text: str) -> str:
        """フィードバックテキストから指摘の種別（不自然な日本語、口調、長文、オウム返し等）を分類・推論する。"""
        t = (text or "").strip()
        if "不自然" in t or "日本語" in t or "もっと自然" in t:
            return "unnatural_japanese"
        if "やめて" in t or "その言い方" in t or "その表現" in t:
            return "style_problem"
        if "長い" in t or "くどい" in t:
            return "too_long"
        if "繰り返" in t or "オウム返し" in t:
            return "parrot"
        return "general_feedback"

    def observe_reaction(
        self,
        user_id: int,
        emoji: str,
        message_id: int,
    ) -> FeedbackObservation:
        """付与されたリアクションを観察し、評価データを生成する。"""
        emoji_str = str(emoji).strip()

        if emoji_str in _POSITIVE_EMOJIS:
            delta = 0.1
            f_type = "positive"
        elif emoji_str in _NEGATIVE_EMOJIS:
            delta = -0.15
            f_type = "negative"
        elif emoji_str in _CONFUSED_EMOJIS:
            delta = -0.05
            f_type = "confusion"
        else:
            delta = 0.0
            f_type = "neutral"

        obs = Observation(
            tool_name="discord_reaction",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=f"Reaction {emoji_str} observed from user {user_id}",
            data={
                "user_id": user_id,
                "emoji": emoji_str,
                "message_id": message_id,
                "feedback_type": f_type,
            },
        )

        return FeedbackObservation(
            user_id=user_id,
            emoji=emoji_str,
            sentiment_delta=delta,
            feedback_type=f_type,
            observation=obs,
        )

    def observe_text_feedback(
        self,
        user_id: int,
        feedback_text: str,
        message_id: int | None = None,
    ) -> FeedbackObservation:
        """テキストメッセージによるフィードバックを観察し、評価データを生成する。"""
        f_type = self.infer_feedback_type(feedback_text)
        delta = -0.1 if f_type in ("unnatural_japanese", "style_problem", "parrot") else -0.05

        obs = Observation(
            tool_name="text_feedback",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=f"Text feedback observed: type={f_type}, text={feedback_text[:100]}",
            data={
                "user_id": user_id,
                "message_id": message_id,
                "feedback_type": f_type,
                "feedback_text": feedback_text,
            },
        )

        return FeedbackObservation(
            user_id=user_id,
            emoji="",
            sentiment_delta=delta,
            feedback_type=f_type,
            observation=obs,
        )

