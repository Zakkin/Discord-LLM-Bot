"""
[AI Agent Summary]
このファイルは、ユーザーとの親密度・信頼度の計算や制限ロジックを担当します（LLMプロンプト生成は harness.agent.prompt_parser.prompt_manager.relationship_prompts を利用）。
This file handles the calculation and clamping logic for user affinity and trust.
"""
from __future__ import annotations

import logging
from typing import Any

from ...common.config_helpers import cfg, cfg_float
from ...common.emotion_helpers import clamp_score
from ...common.ollama_helpers import truncate_text
from harness.agent.prompt_parser.prompt_manager.relationship_prompts import build_relationship_prompt
from ..ollama_chat_types import UserRelationship

log = logging.getLogger("ollama_bot.ollama_chat")


def _relationship_abs_max() -> float:
    return max(float(cfg("RELATIONSHIP_ABS_MAX", 10.0) or 10.0), 0.5)

def _clamp_relationship_value(value: float) -> float:
    limit = _relationship_abs_max()
    try:
        numeric = float(value)
    except Exception:
        numeric = 0.0
    return max(-limit, min(limit, numeric))

def _copy_relationship(rel: UserRelationship | None) -> UserRelationship:
    if rel is None:
        return UserRelationship()
    return UserRelationship(
        affinity=_clamp_relationship_value(rel.affinity),
        trust=_clamp_relationship_value(rel.trust),
        affection=_clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0),
        hatred=_clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0),
        thought=str(rel.thought or "").strip(),
        last_interaction_ts=float(rel.last_interaction_ts or 0.0),
        last_thought_generated_ts=float(getattr(rel, "last_thought_generated_ts", 0.0) or 0.0),
        interaction_count=int(getattr(rel, "interaction_count", 0) or 0),
        is_lover=bool(getattr(rel, "is_lover", False)),
    )

def _relationship_delta_from_scores(scores: dict[str, float] | None) -> tuple[float, float, float, float]:
    source = scores or {}
    joy = clamp_score(source.get("joy", 0.0))
    anticipation = clamp_score(source.get("anticipation", 0.0))
    anger = clamp_score(source.get("anger", 0.0))
    disgust = clamp_score(source.get("disgust", 0.0))
    fear = clamp_score(source.get("fear", 0.0))

    affinity_delta = (
        joy * cfg_float("RELATIONSHIP_AFFINITY_JOY_WEIGHT", 0.18)
        + anticipation * cfg_float("RELATIONSHIP_AFFINITY_ANTICIPATION_WEIGHT", 0.06)
        - anger * cfg_float("RELATIONSHIP_AFFINITY_ANGER_WEIGHT", 0.22)
        - disgust * cfg_float("RELATIONSHIP_AFFINITY_DISGUST_WEIGHT", 0.12)
    )
    trust_delta = (
        joy * cfg_float("RELATIONSHIP_TRUST_JOY_WEIGHT", 0.08)
        + anticipation * cfg_float("RELATIONSHIP_TRUST_ANTICIPATION_WEIGHT", 0.05)
        - fear * cfg_float("RELATIONSHIP_TRUST_FEAR_WEIGHT", 0.22)
        - disgust * cfg_float("RELATIONSHIP_TRUST_DISGUST_WEIGHT", 0.15)
        - anger * cfg_float("RELATIONSHIP_TRUST_ANGER_WEIGHT", 0.08)
    )
    affection_delta = (
        joy * cfg_float("RELATIONSHIP_AFFECTION_JOY_WEIGHT", 0.20)
        + anticipation * cfg_float("RELATIONSHIP_AFFECTION_ANTICIPATION_WEIGHT", 0.08)
        - disgust * cfg_float("RELATIONSHIP_AFFECTION_DISGUST_WEIGHT", 0.22)
        - anger * cfg_float("RELATIONSHIP_AFFECTION_ANGER_WEIGHT", 0.18)
    )
    hatred_delta = (
        anger * cfg_float("RELATIONSHIP_HATRED_ANGER_WEIGHT", 0.25)
        + disgust * cfg_float("RELATIONSHIP_HATRED_DISGUST_WEIGHT", 0.20)
        + fear * cfg_float("RELATIONSHIP_HATRED_FEAR_WEIGHT", 0.08)
        - joy * cfg_float("RELATIONSHIP_HATRED_JOY_WEIGHT", 0.15)
    )
    return affinity_delta, trust_delta, affection_delta, hatred_delta

def _apply_relationship_delta(rel: UserRelationship, scores: dict[str, float] | None) -> UserRelationship:
    decay = max(float(cfg("RELATIONSHIP_DECAY", 1.0) or 1.0), 0.0)
    affinity_delta, trust_delta, affection_delta, hatred_delta = _relationship_delta_from_scores(scores)
    rel.affinity = _clamp_relationship_value((rel.affinity * decay) + affinity_delta)
    rel.trust = _clamp_relationship_value((rel.trust * decay) + trust_delta)
    rel.affection = _clamp_relationship_value((getattr(rel, "affection", 0.0) * decay) + affection_delta)
    rel.hatred = _clamp_relationship_value((getattr(rel, "hatred", 0.0) * decay) + hatred_delta)
    return rel

def _fallback_user_thought(rel: UserRelationship, *, base_name: str = "") -> str:
    if getattr(rel, "is_lover", False):
        return "お互いに想いを通わせ合った、最愛で特別な恋人！"
    affinity = _clamp_relationship_value(rel.affinity)
    trust = _clamp_relationship_value(rel.trust)
    affection = _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0)

    if hatred >= 5.0 and affection <= -2.0:
        return "強い憎しみと嫌悪感があり、二度と顔も見たくない相手。"
    if affection >= 3.0 and hatred >= 3.0:
        return "腹が立つことも多いけれど、なぜか放っておけない複雑な相手。"
    if hatred >= 3.0:
        return "何かと棘を感じていて、強い反感を抱いている。"
    if affection >= 5.0 and trust >= 4.5:
        return "かけがえのない無二の存在で、心から信頼できる大切な相棒！"
    if affinity >= 5.0 and trust >= 4.5:
        return "かけがえのない無二の存在で、心から信頼できる大切な相棒！"
    if affinity >= 2.0 and trust >= 2.0:
        return "すごく気が合って頼りになる、大好きな相棒！"
    if affection >= 2.0 and trust >= 1.0:
        return "とても好印象で、一緒に話していて心から楽しい相手。"
    if affinity >= 1.0 and trust >= 0.8:
        return "いつも気軽に話せて楽しい、親しい仲間。"
    if affinity >= 0.8 and trust < -0.5:
        return "話すのは楽しいけど、まだ少し警戒してるかも。"
    if affinity >= 0.8:
        return "好意的で話しやすい人だなと思っている。"
    if trust >= 1.5 and affinity >= 0.0:
        return "真面目でとても信頼できる人。"
    if affinity <= -5.0 and trust <= -4.5:
        return "完全な敵対心と拒絶感があり、全く関わりたくない相手。"
    if affinity <= -2.0 and trust <= -2.0:
        return "強い警戒心があって、あまり不用意には近づきたくない相手。"
    if affinity <= -1.0 and trust <= -0.5:
        return "少し刺々しさを感じていて、距離を置いて接したい。"
    if affinity <= -1.0 and trust >= 0.0:
        return "少し素っ気なく接しがちだけど、悪意はなさそう。"
    if trust <= -1.2:
        return "何か裏があるんじゃないかと少し疑いながら見ている。"
    if affinity <= -0.5:
        return "まだ少し打ち解けられていない感じがする。"
    return "まだ知り合って間もない、普通のお知り合い。"

def _relationship_rank_info(rel: UserRelationship) -> tuple[str, str, int]:
    """(ランク名, 簡単な解説, Embed用カラーコード) を返す。"""
    if getattr(rel, "is_lover", False):
        return (
            "💍 恋人・最愛のパートナー",
            "お互いに愛を誓い合った特別な恋人関係です。何よりも大切で愛おしい存在です。",
            0xFF69B4,  # HotPink
        )

    affinity = _clamp_relationship_value(rel.affinity)
    trust = _clamp_relationship_value(rel.trust)
    affection = _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0)
    total = affinity + trust

    if hatred >= 6.0 and affection <= -3.0:
        return "💀 敵対・絶対的拒絶", "強い憎悪と完全な拒絶感を抱いており、関わりを一切拒絶したい状態です。", 0x191970  # MidnightBlue
    if affection >= 3.0 and hatred >= 3.0:
        return "🌪️ 愛憎葛藤・複雑な感情", "好意と反発が激しく入り混じり、気になりつつも素直になれない状態です。", 0x9370DB  # MediumPurple
    if affinity >= 6.0 and trust >= 5.0:
        return "💖 親愛無二・無二の相棒", "無条件の信頼と深い絆で結ばれており、かけがえのない大切な存在です。", 0xFF1493  # DeepPink
    if affinity >= 2.0 and trust >= 1.5:
        return "🌟 親密・強い信頼", "深く心を許しており、とても大切に思っている相手です。", 0xFF69B4  # HotPink
    if total >= 3.0:
        return "💖 良好・親愛", "好意的で親しみを感じており、楽しく会話できる相手です。", 0xFFB6C1  # LightPink
    if total >= 1.0:
        return "🌿 友好的・好感", "好印象を持っており、穏やかに接することができます。", 0x98FB98  # PaleGreen
    if affinity <= -6.0 and trust <= -5.0:
        return "💀 敵対・絶対的拒絶", "強い敵対心と完全な拒絶感を抱いており、関わりを一切拒絶したい状態です。", 0x191970  # MidnightBlue
    if total <= -4.0 or hatred >= 4.0:
        return "❄️ 拒絶・強い警戒", "強い警戒心と距離感を抱いており、警戒を緩めていません。", 0x4682B4  # SteelBlue
    if total <= -1.5 or hatred >= 1.5:
        return "⚠️ 警戒・距離感あり", "少し身構えており、言葉の意図を慎重に見極めています。", 0xFFA07A  # LightSalmon
    return "⚖️ 中立・様子見", "まだ距離を測っている段階で、フラットに接しています。", 0xB0C4DE  # LightSteelBlue

def _render_relationship_bar(value: float, min_val: float = -10.0, max_val: float = 10.0, total_blocks: int = 10) -> str:
    """数値をゲージバー [████████░░] 形式で描画する。"""
    clamped = max(min_val, min(max_val, value))
    ratio = (clamped - min_val) / (max_val - min_val) if max_val > min_val else 0.5
    filled_blocks = int(round(ratio * total_blocks))
    filled_blocks = max(0, min(total_blocks, filled_blocks))
    empty_blocks = total_blocks - filled_blocks
    return f"[`{'█' * filled_blocks}{'░' * empty_blocks}`]"

