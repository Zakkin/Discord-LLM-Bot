"""
[AI Agent Summary]
emotion ディレクトリは、感情スコア推論、関係性ベクトル、内的状態の減衰、Discordプレゼンス更新などを管理するモジュール群です。
The emotion directory is a collection of modules managing emotion score inference, relationship vectors, internal state decay, and Discord presence updates.
"""
from .core_mixin import OllamaChatEmotionMixin
from ...common import memory_logic
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    EMOTION_LABEL_TO_FACE,
    EMOTION_LABEL_TO_JA,
    apply_emotion_persona_weights,
    build_memory_emotion_tags,
    build_emotion_evaluation_sections,
    build_emotion_bio_text,
    build_emotion_status_text,
    build_nickname_with_face,
    build_reply_emotion_guidance,
    clamp_score,
    contains_cjk_non_japanese,
    emotion_bio_enabled,
    emotion_scoring_enabled,
    fallback_reason_text,
    looks_like_bad_reason_text,
    merge_emotion_scores,
    normalize_llm_emotion_scores,
    normalize_reason_text,
)
from ...common.ollama_helpers import call_ollama_json, truncate_lines, truncate_text

__all__ = [
    "OllamaChatEmotionMixin",
    "memory_logic",
    "EMOTION_KEYS",
    "EMOTION_LABEL_TO_FACE",
    "EMOTION_LABEL_TO_JA",
    "apply_emotion_persona_weights",
    "build_memory_emotion_tags",
    "build_emotion_evaluation_sections",
    "build_emotion_bio_text",
    "build_emotion_status_text",
    "build_nickname_with_face",
    "build_reply_emotion_guidance",
    "clamp_score",
    "contains_cjk_non_japanese",
    "emotion_bio_enabled",
    "emotion_scoring_enabled",
    "fallback_reason_text",
    "looks_like_bad_reason_text",
    "merge_emotion_scores",
    "normalize_llm_emotion_scores",
    "normalize_reason_text",
    "call_ollama_json",
    "truncate_lines",
    "truncate_text",
]

