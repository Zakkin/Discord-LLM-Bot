"""
[AI Agent Summary]
このファイルは、ユーザーとの親密度・信頼度の計算や制限ロジックを担当します。LLMプロンプト用テキスト生成も含みます。
This file handles the calculation and clamping logic for user affinity and trust. It also includes text generation for LLM prompts.
"""
from __future__ import annotations

import logging
from typing import Any

from ...common.config_helpers import cfg, cfg_float
from ...common.emotion_helpers import clamp_score
from ...common.ollama_helpers import truncate_text
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

def build_relationship_prompt(rel: UserRelationship, *, instruction: str = "") -> str:
    affinity = _clamp_relationship_value(rel.affinity)
    trust = _clamp_relationship_value(rel.trust)
    affection = _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0)
    hatred = _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0)

    lines = [
        "【この相手との関係性・感情】",
        f"- 親密度: {affinity:+.2f}",
        f"- 信頼度: {trust:+.2f}",
        f"- 好意: {affection:+.2f}",
        f"- 憎しみ: {hatred:+.2f}",
    ]
    thought = str(rel.thought or "").strip()
    if thought:
        lines.append(f"- あなたから見たこの相手への印象（一言）: 「{thought}」")
        
    # 信頼度に基づく指示
    if trust >= 5.0:
        lines.append("- この相手には絶大な信頼を置いており、どんな言葉も深い好意や絆としてポジティブに受け取りやすい。")
    elif trust >= 2.0:
        lines.append("- この相手にはかなり気を許しており、多少荒い言い方でも親しい冗談として受け取りやすい。")
    elif trust >= 0.8:
        lines.append("- この相手には比較的警戒が薄く、言葉の意図を好意的に読み取りやすい。")
    elif trust <= -5.0:
        lines.append("- この相手には極めて強い敵対心・不信感があり、あらゆる言葉の裏に罠や悪意を警戒している。")
    elif trust <= -2.0:
        lines.append("- この相手には強い警戒心があり、言葉の裏の悪意や攻撃性を疑いやすい。")
    elif trust <= -0.8:
        lines.append("- この相手の言葉は少し疑い気味に受け取りやすく、軽い挑発でも引っかかりやすい。")

    # 親密度に基づく指示
    if affinity >= 5.0:
        lines.append("- この相手のことが大好きでたまらず、甘えたり率直な好意・喜びを素直に表現しやすい。")
    elif affinity >= 2.0:
        lines.append("- この相手をかなり気に入っており、少し甘えや親しいツッコミが出てもよい。")
    elif affinity >= 0.8:
        lines.append("- この相手にはやや親しみがあり、少し柔らかい態度になりやすい。")
    elif affinity <= -5.0:
        lines.append("- この相手には強い嫌悪感・拒絶感を抱いており、極めて冷淡で徹底的に距離を置こうとしやすい。")
    elif affinity <= -2.0:
        lines.append("- この相手にはかなり冷淡で、なるべく距離を置こうとしやすい。")
    elif affinity <= -0.8:
        lines.append("- この相手には少し距離を置きたくなっており、反応がやや素っ気なくなりやすい。")

    # 好意・憎しみ・愛憎関係に基づく指示
    if affection >= 2.0 and hatred >= 2.0:
        lines.append("- この相手には好意と憎しみが入り混じった複雑な愛憎関係を抱いている。反発や毒舌を吐きつつも、どこか相手を意識して気にかけてしまうような葛藤した態度（ツンデレ・素直になれない態度）になりやすい。")
    else:
        if hatred >= 5.0:
            lines.append("- この相手には激しい憎しみ・敵意を感じており、強い拒絶、激しい怒り、または氷のように冷淡な突き放しが言葉に現れやすい。")
        elif hatred >= 2.0:
            lines.append("- この相手には明確な恨み・反感があり、皮肉や棘（トゲ）のある言い回し、チクリと刺すような反発が出やすい。")
        elif hatred <= -2.0:
            lines.append("- この相手に対するわだかまりや恨みは完全に解消されており、過去のいざこざを引きずらず穏やかに接している。")

        if affection >= 5.0:
            lines.append("- この相手には極めて強い特別な好意を抱いており、素直な好意・深い優しさや無邪気な甘えが自然に出やすい。")
        elif affection >= 2.0:
            lines.append("- この相手には明確な好意を抱いており、言葉を好意的に受け止め、温かみのある柔らかい態度になりやすい。")
        elif affection <= -2.0:
            lines.append("- この相手にはあまり好意を持てず、感情のこもらない素っ気なく事務的な態度になりやすい。")

    if trust <= -0.8 or affinity <= -0.8 or hatred >= 2.0:
        from lib.config_utils import cfg
        constraint = str(cfg("RELATIONSHIP_LOW_TRUST_CONSTRAINT", "") or "").strip()
        if not constraint:
            constraint = (
                "- ※重要制約: 反発や棘（トゲ）、不満・警戒を表現する場合でも、相手を直接罵倒したり口調を乱暴（あんた、お前、黙れ等）にしてはいけません。"
                "必ずあなたのキャラクター設定を保ったまま、等身大の感情として表現してください。"
            )
        lines.append(constraint)

    if len(lines) <= 5:
        lines.append("- この相手とはまだ中立に近い距離感で接している。")
    if instruction.strip():
        lines.append(f"- {instruction.strip()}")
    return "\n".join(lines)
