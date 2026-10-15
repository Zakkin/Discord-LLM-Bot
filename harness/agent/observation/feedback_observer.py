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
_POSITIVE_EMOJIS = {
    "👍", "❤️", "🥰", "🎉", "👏", "✨", "😊", "💖", "🙏", "🌸",
    "😻", "🤩", "💯", "🤝", "🫶", "🆒", "⭕", "🙆", "😆", "🤤",
    "😋", "💕", "😍", "🙌", "🔥", "⭐", "🌟",
}
_NEGATIVE_EMOJIS = {
    "👎", "😡", "😠", "💢", "🙄", "🤮", "❌", "💔", "🤬", "💩",
    "🤡", "👿", "🙅", "💀", "🤦", "😒", "🤢", "🖕", "🆖", "🚫",
}
_CONFUSED_EMOJIS = {
    "❓", "❔", "🤔", "🧐", "😵‍💫", "🤨", "😶‍🌫️", "😳", "🥺", "😐",
}

# 絵文字テキスト名（カスタム絵文字やテキストエイリアス用）のマッピング
_POSITIVE_EMOJI_NAMES = {"thumbsup", "good", "heart", "smile", "joy", "clap", "tada", "sparkles", "ok_hand"}
_NEGATIVE_EMOJI_NAMES = {"thumbsdown", "bad", "angry", "rage", "dislike", "poop", "vomit", "cross_mark"}
_CONFUSED_EMOJI_NAMES = {"question", "thinking", "confused", "face_with_raised_eyebrow"}


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
    "事実と違う",
    "事実誤認",
    "嘘",
    "デマ",
    "間違って",
    "間違い",
    "誤り",
    "長すぎる",
    "長文すぎる",
    "くどい",
    "短くして",
    "タメ口",
    "馴れ馴れしい",
    "敬語",
    "同じこと",
    "オウム返し",
    "繰り返さないで",
    "繰り返すな",
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
        """フィードバックテキストから指摘の種別（不自然な日本語、口調、長文、オウム返し、事実誤認等）を分類・推論する。"""
        t = (text or "").strip()
        if "事実" in t or "嘘" in t or "デマ" in t or "間違い" in t or "間違っ" in t or "誤り" in t:
            return "fact_error"
        if "不自然" in t or "日本語" in t or "もっと自然" in t:
            return "unnatural_japanese"
        if "やめて" in t or "その言い方" in t or "その表現" in t or "タメ口" in t or "敬語" in t or "馴れ馴れしい" in t:
            return "style_problem"
        if "長い" in t or "くどい" in t or "長文" in t:
            return "too_long"
        if "短すぎる" in t or "短くして" in t:
            return "too_short"
        if "繰り返" in t or "オウム返し" in t or "同じこと" in t:
            return "parrot"
        return "general_feedback"

    def classify_emoji(self, emoji: str) -> tuple[str, float]:
        """絵文字からフィードバック種別（positive/negative/confusion/neutral）とセンチメント変化量を判定する。"""
        emoji_str = str(emoji).strip()
        norm_name = emoji_str.strip(":").lower()

        if emoji_str in _POSITIVE_EMOJIS or norm_name in _POSITIVE_EMOJI_NAMES:
            return "positive", 0.1
        elif emoji_str in _NEGATIVE_EMOJIS or norm_name in _NEGATIVE_EMOJI_NAMES:
            return "negative", -0.15
        elif emoji_str in _CONFUSED_EMOJIS or norm_name in _CONFUSED_EMOJI_NAMES:
            return "confusion", -0.05
        else:
            return "neutral", 0.0

    def observe_reaction(
        self,
        user_id: int,
        emoji: str,
        message_id: int,
    ) -> FeedbackObservation:
        """付与されたリアクションを観察し、評価データを生成する。"""
        return self.record_reaction_feedback(
            user_id=user_id,
            emoji=emoji,
            message_id=message_id,
            is_add=True,
        )

    def record_reaction_feedback(
        self,
        user_id: int,
        emoji: str,
        message_id: int | None = None,
        channel_id: int | None = None,
        guild_id: int | None = None,
        user_text: str | None = None,
        assistant_text: str | None = None,
        is_add: bool = True,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FeedbackObservation:
        """付与または削除されたリアクションを観察・記録し、感情変化やフィードバック評価データを生成する。

        Args:
            user_id: リアクションを行ったユーザーID
            emoji: 付与/削除された絵文字（Unicodeまたはカスタム絵文字文字列表現）
            message_id: リアクションが付与されたメッセージID
            channel_id: チャンネルID
            guild_id: ギルド（サーバー）ID
            user_text: 関連するユーザー発言テキスト
            assistant_text: 関連するアシスタント発言テキスト
            is_add: リアクション付与時はTrue、削除時はFalse
            metadata: 追加メタデータ
            **kwargs: 互換性拡張用引数
        """
        emoji_str = str(emoji).strip()
        f_type, base_delta = self.classify_emoji(emoji_str)

        # リアクション削除（is_add=False）の場合はセンチメント変化を反転
        delta = base_delta if is_add else -base_delta

        meta = dict(metadata or {})
        if kwargs:
            meta.update(kwargs)
        if channel_id is not None:
            meta["channel_id"] = channel_id
        if guild_id is not None:
            meta["guild_id"] = guild_id
        if user_text is not None:
            meta["user_text"] = user_text
        if assistant_text is not None:
            meta["assistant_text"] = assistant_text
        meta["is_add"] = is_add

        action_desc = "added" if is_add else "removed"
        obs = Observation(
            tool_name="discord_reaction",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=f"Reaction {emoji_str} {action_desc} by user {user_id}",
            data={
                "user_id": user_id,
                "emoji": emoji_str,
                "message_id": message_id,
                "feedback_type": f_type,
                "is_add": is_add,
                **meta,
            },
            metadata=meta,
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
        delta = -0.1 if f_type in ("unnatural_japanese", "style_problem", "parrot", "fact_error") else -0.05

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

    def is_reaction_blocked_error(self, exc: Exception) -> bool:
        """Discordの例外がリアクションブロック（Forbidden 90001等）か安全に判定する。"""
        code = getattr(exc, "code", None)
        if code == 90001:
            return True
        exc_str = str(exc)
        return "Reaction blocked" in exc_str or "reaction blocked" in exc_str

    def should_suppress_negative_reaction(self, user_text: str) -> bool:
        """親しみのある挨拶、短い相槌、言葉遊び等の文脈でBAD（ネガティブ）リアクションを抑制すべきか判定する。"""
        t = (user_text or "").strip().lower()
        if not t:
            return False

        # 挨拶系
        greetings = ("おはよう", "こんにちは", "こんばんは", "おやすみ", "お疲れ", "おつかれ", "やあ", "ハロ", "hello")
        if any(g in t for g in greetings):
            return True

        # 短い相槌・感嘆
        interjections = ("うん", "へー", "ほー", "そう", "なるほど", "わかった", "了解", "ok", "草", "笑")
        if t in interjections or (len(t) <= 5 and any(t.startswith(i) for i in ("うん", "そう", "へー"))):
            return True

        # 言葉遊び・クイズ
        wordplay = ("しりとり", "クイズ", "なぞなぞ", "じゃんけん")
        if any(w in t for w in wordplay):
            return True

        return False

    def build_feedback_record(
        self,
        persona: str,
        user_id: int,
        feedback_text: str,
        guild_id: int | None = None,
        channel_id: int | None = None,
        original_user_text: str | None = None,
        assistant_text: str | None = None,
        corrected_assistant_text: str | None = None,
        feedback_type: str | None = None,
        accepted: bool = False,
    ) -> dict[str, Any]:
        """メモリ永続化層（add_feedback_item）へ渡す正規化辞書を生成するヘルパー。"""
        resolved_type = feedback_type or self.infer_feedback_type(feedback_text)
        return {
            "persona": persona,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "original_user_text": original_user_text,
            "assistant_text": assistant_text,
            "feedback_text": feedback_text,
            "corrected_assistant_text": corrected_assistant_text,
            "feedback_type": resolved_type,
            "accepted": accepted,
        }


