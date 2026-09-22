from __future__ import annotations

import re
import unicodedata
from typing import Any

from .config_helpers import cfg_bool, cfg_float, cfg_int


EMOTION_KEYS = ("joy", "anticipation", "anger", "disgust", "sadness", "surprise", "fear")

EMOTION_LABEL_TO_FACE = {
    "joy": "🤩",
    "anticipation": "😋",
    "anger": "😡",
    "disgust": "😒",
    "sadness": "😢",
    "surprise": "😳",
    "fear": "😨",
    "neutral": "😐",
}

EMOTION_LABEL_TO_JA = {
    "joy": "喜び",
    "anticipation": "期待",
    "anger": "怒り",
    "disgust": "嫌悪",
    "sadness": "悲しみ",
    "surprise": "驚き",
    "fear": "恐れ",
    "neutral": "通常",
}


def _split_context_line(line: str) -> tuple[str, str]:
    raw = str(line or "").strip()
    if not raw:
        return "", ""
    if ":" not in raw:
        return "", raw
    speaker, text = raw.split(":", 1)
    return speaker.strip(), text.strip()


def relabel_emotion_context_line(
    line: str,
    *,
    self_label: str = "あなた",
    self_aliases: set[str] | None = None,
) -> str:
    speaker, text = _split_context_line(line)
    if not text:
        return ""

    aliases = {alias.strip().lower() for alias in (self_aliases or set()) if str(alias or "").strip()}
    speaker_key = speaker.lower()
    speaker_key_without_face = speaker_key.lstrip("".join(EMOTION_LABEL_TO_FACE.values()) + " ")
    if speaker_key == "assistant" or (speaker_key and (speaker_key in aliases or speaker_key_without_face in aliases)):
        return f"{self_label}: {text}"
    if speaker:
        return f"相手({speaker}): {text}"
    return f"相手: {text}"


def build_emotion_evaluation_sections(
    context_lines: list[str],
    latest_user_text: str,
    *,
    self_label: str = "あなた",
    self_aliases: set[str] | None = None,
) -> list[str]:
    parts: list[str] = []
    relabeled_context = [
        relabeled
        for line in (context_lines or [])
        if (relabeled := relabel_emotion_context_line(line, self_label=self_label, self_aliases=self_aliases))
    ]
    if relabeled_context:
        parts.append(f"会話の背景（{self_label}が自分、それ以外は相手）:")
        parts.extend(relabeled_context)
        parts.append("")
    parts.append("最新の相手の発言:")
    parts.append((latest_user_text or "").strip() or "（本文なし）")
    return parts


def emotion_scoring_enabled() -> bool:
    return cfg_bool("EMOTION_SCORING_ENABLED", True)


def emotion_bio_enabled() -> bool:
    return cfg_bool("EMOTION_BIO_ENABLED", True)


def clamp_score(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0


def normalize_llm_emotion_scores(scores: dict[str, Any] | None) -> dict[str, float]:
    source = scores or {}
    numeric: dict[str, float] = {}
    for key in EMOTION_KEYS:
        try:
            numeric[key] = float(source.get(key, 0.0))
        except Exception:
            numeric[key] = 0.0

    values = [max(0.0, value) for value in numeric.values()]
    max_value = max(values, default=0.0)
    over_one_count = sum(1 for value in values if value > 1.0)
    looks_like_percentage_scale = max_value > 5.0 or over_one_count >= 2

    normalized = {key: max(0.0, value) for key, value in numeric.items()}
    if looks_like_percentage_scale and max_value <= 100.0:
        normalized = {key: (value / 100.0) for key, value in normalized.items()}

    return {key: clamp_score(value) for key, value in normalized.items()}


def merge_emotion_scores(*score_maps: dict[str, float] | None) -> dict[str, float]:
    merged = {key: 0.0 for key in EMOTION_KEYS}
    for scores in score_maps:
        source = scores or {}
        for key in EMOTION_KEYS:
            merged[key] = max(merged[key], clamp_score(source.get(key, 0.0)))
    return merged


def apply_emotion_persona_weights(scores: dict[str, float] | None) -> dict[str, float]:
    source = scores or {}
    weights = {
        "joy": cfg_float("EMOTION_PERSONA_WEIGHT_JOY", 1.0),
        "anticipation": cfg_float("EMOTION_PERSONA_WEIGHT_ANTICIPATION", 1.0),
        "anger": cfg_float("EMOTION_PERSONA_WEIGHT_ANGER", 0.85),
        "disgust": cfg_float("EMOTION_PERSONA_WEIGHT_DISGUST", 1.0),
        "sadness": cfg_float("EMOTION_PERSONA_WEIGHT_SADNESS", 1.0),
        "surprise": cfg_float("EMOTION_PERSONA_WEIGHT_SURPRISE", 1.0),
        "fear": cfg_float("EMOTION_PERSONA_WEIGHT_FEAR", 1.0),
    }
    adjusted = {key: 0.0 for key in EMOTION_KEYS}
    for key in EMOTION_KEYS:
        adjusted[key] = clamp_score(source.get(key, 0.0) * weights.get(key, 1.0))
    return adjusted


def should_force_deliberation_from_emotions(
    scores: dict[str, float] | None,
    *,
    curiosity: float = 0.0,
    anger_threshold: float = 0.65,
    surprise_threshold: float = 0.60,
    curiosity_threshold: float = 0.68,
) -> tuple[bool, str]:
    anger_score = clamp_score((scores or {}).get("anger", 0.0))
    if anger_score >= clamp_score(anger_threshold):
        return True, "anger"

    surprise_score = clamp_score((scores or {}).get("surprise", 0.0))
    if surprise_score >= clamp_score(surprise_threshold):
        return True, "surprise"

    curiosity_score = clamp_score(curiosity)
    if curiosity_score >= clamp_score(curiosity_threshold):
        return True, "curiosity"

    return False, ""


def should_force_thinking_from_emotions(
    scores: dict[str, float] | None,
    *,
    curiosity: float = 0.0,
    anger_threshold: float = 0.65,
    surprise_threshold: float = 0.60,
    curiosity_threshold: float = 0.68,
) -> tuple[bool, str]:
    return should_force_deliberation_from_emotions(
        scores,
        curiosity=curiosity,
        anger_threshold=anger_threshold,
        surprise_threshold=surprise_threshold,
        curiosity_threshold=curiosity_threshold,
    )


def contains_cjk_non_japanese(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            continue
        if ch in "这那个们了在是和与及被把让对将并且嗎吗，；：【】《》":
            return True
    return False


def looks_like_bad_reason_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if contains_cjk_non_japanese(t):
        return True
    has_predicate = bool(re.search(r"(した|している|していた|なった|感じた|覚えた|見えた|受けた|ため|ので|から|です|ます|だ。|だった)", t))
    if not has_predicate:
        return True
    from lib.config_utils import cfg
    bot_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    patterns = [r"AI.*?うんこ.*?してください"]
    if bot_name:
        patterns.append(re.escape(bot_name))
    for ptn in patterns:
        if re.search(ptn, t):
            return True
    return False


def normalize_reason_text(text: str, max_chars: int) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    t = t.replace('"', "").replace("'", "")
    t = t.strip(" 、。")
    from lib.config_utils import cfg
    bot_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if bot_name:
        t = t.replace(bot_name, "AI")
    t = t.replace("ユーザー", "相手")
    if t and not t.endswith("。"):
        t += "。"
    from . import ollama_helpers
    t = ollama_helpers.truncate_text(t, max_chars)
    return t or "相手の発言に不満を覚えたためです。"


def fallback_reason_text(emotion_key: str) -> str:
    fallback_map = {
        "joy": "相手の発言を前向きに受け取ったためです。",
        "anticipation": "相手の発言の続きや反応を期待したためです。",
        "anger": "相手の発言に苛立ちを覚えたためです。",
        "disgust": "相手の発言に嫌悪感を覚えたためです。",
        "sadness": "相手の発言を受けて気落ちしたためです。",
        "surprise": "相手の発言が予想外だったためです。",
        "fear": "相手の発言に不安を覚えたためです。",
        "neutral": "平常状態です。",
    }
    return fallback_map.get(emotion_key, "平常状態です。")


def adapt_emotion_name(emotion_key: str, reason: str) -> str:
    emotion_name = EMOTION_LABEL_TO_JA.get(emotion_key, "通常")
    if emotion_key == "joy" and reason:
        if any(w in reason for w in ("安心", "ホッと", "落ち着", "共感", "同情", "限界")):
            emotion_name = "安心・和み"
    return emotion_name

def build_emotion_bio_text(emotion_key: str, reason: str) -> str:
    body = normalize_reason_text((reason or "").strip(), cfg_int("EMOTION_BIO_MAX_CHARS", 180)) or "平常状態です。"
    emotion_name = adapt_emotion_name(emotion_key, body)
    return f"現在の感情: {emotion_name}\n理由: {body}"


def build_emotion_status_text(emotion_key: str, reason: str) -> str:
    return adapt_emotion_name(emotion_key, reason)


def _emotion_strength_word(score: float) -> str:
    s = clamp_score(score)
    if s >= 0.82:
        return "非常に強く"
    if s >= 0.65:
        return "かなり強く"
    if s >= 0.48:
        return "はっきり"
    if s >= 0.30:
        return "少し"
    return "ごく薄く"


def _emotion_tone_fragment(emotion_key: str, score: float) -> str:
    strength = _emotion_strength_word(score)
    fragment_map = {
        "joy": f"{strength}明るさがにじむ",
        "anticipation": f"{strength}先を気にする",
        "anger": f"{strength}刺のある",
        "disgust": f"{strength}距離を取りたくなる",
        "sadness": f"{strength}沈み気味の",
        "surprise": f"{strength}戸惑いを含む",
        "fear": f"{strength}警戒が混じる",
    }
    return fragment_map.get(emotion_key, f"{strength}揺れのある")


def _summarize_emotion_mix(scores: dict[str, float]) -> tuple[str, list[tuple[str, float]]]:
    filtered: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        value = clamp_score((scores or {}).get(key, 0.0))
        if value >= 0.18:
            filtered.append((key, value))
    ranked = sorted(filtered, key=lambda x: x[1], reverse=True)
    if not ranked:
        return "大きな感情の偏りはなく、比較的落ち着いた状態です。", []
    top_key, top_score = ranked[0]
    if len(ranked) == 1:
        return f"{_emotion_tone_fragment(top_key, top_score)}空気が前面に出ています。", ranked
    second_key, second_score = ranked[1]
    if second_score >= top_score * 0.72:
        return (
            f"{_emotion_tone_fragment(top_key, top_score)}反応を軸にしつつ、"
            f"{_emotion_tone_fragment(second_key, second_score)}感じも混ざっています。",
            ranked,
        )
    return f"{_emotion_tone_fragment(top_key, top_score)}空気がやや強めです。", ranked


def build_nickname_with_face(base_name: str, emotion_key: str) -> str:
    return f"{EMOTION_LABEL_TO_FACE.get(emotion_key, '😐')} {base_name}".strip()


def build_reply_emotion_guidance(scores: dict[str, float], reason: str) -> str:
    """感情スコア辞書と理由テキストから、LLMへの返答指針テキストを生成する。"""
    from . import ollama_helpers
    compact_reason = ollama_helpers.truncate_text((reason or "").strip(), 120) or "うまく言葉にできない状態です。"
    emotion_summary, ranked = _summarize_emotion_mix(scores)

    if not ranked:
        tone = "通常どおり自然な調子。"
    else:
        tone_bits = [_emotion_tone_fragment(key, score) for key, score in ranked[:2]]
        if len(tone_bits) >= 2:
            tone_core = f"{'、'.join(tone_bits[:-1])}、かつ{tone_bits[-1]}"
        else:
            tone_core = tone_bits[0]
        tone = f"{tone_core}の温度感を自然に反映。"

    return f"現在の感情: {emotion_summary}\n理由: {compact_reason}\n感情の温度感: {tone}"


def emotion_blend_ratio() -> float:
    return cfg_float("EMOTION_BLEND_RATIO", 0.35)


def emotion_decay() -> float:
    return cfg_float("EMOTION_DECAY", 0.92)


EMOTION_REACTION_EMOJIS = {
    "joy": ("❤️", "✨", "🥳"),
    "anticipation": ("👀", "✨"),
    "anger": ("😠",),
    "disgust": ("😒",),
    "sadness": ("💧", "🥺"),
    "surprise": ("😳", "✨"),
    "fear": ("😨", "👀"),
}


def build_memory_emotion_tags(scores: dict[str, float] | None, *, max_tags: int = 2, min_score: float = 0.34) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        score = clamp_score((scores or {}).get(key, 0.0))
        if score >= min_score:
            ranked.append((key, score))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [key for key, _ in ranked[:max(1, int(max_tags))]]


def format_emotion_tags_ja(tags: list[str] | tuple[str, ...] | None) -> str:
    items = [EMOTION_LABEL_TO_JA.get(str(tag), str(tag)) for tag in (tags or []) if str(tag).strip()]
    return "・".join(items)


def choose_emotion_reaction_emojis(
    scores: dict[str, float] | None,
    *,
    threshold: float = 0.80,
    max_emojis: int = 2,
) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        score = clamp_score((scores or {}).get(key, 0.0))
        if score >= threshold:
            ranked.append((key, score))
    ranked.sort(key=lambda item: item[1], reverse=True)

    chosen: list[str] = []
    for key, _ in ranked:
        for emoji in EMOTION_REACTION_EMOJIS.get(key, ()):
            if emoji not in chosen:
                chosen.append(emoji)
            if len(chosen) >= max_emojis:
                return chosen
    return chosen
