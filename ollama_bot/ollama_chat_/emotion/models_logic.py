"""
[AI Agent Summary]
このファイルは、感情分析に使用するLLMのモデル名取得や出力スキーマ生成、モデル出力スコアの正規化ロジックを担当します。
This file handles getting LLM model names, generating output schemas, and normalizing score outputs for emotion analysis.
"""
from __future__ import annotations

from typing import Any

from ...common.config_helpers import cfg, cfg_bool
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    clamp_score,
    normalize_llm_emotion_scores,
)

EMOTION_REASON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string"}},
    "required": ["reason"],
    "additionalProperties": False,
}
EMOTION_REACTION_VALUES = ("GOOD", "BAD", "NONE")

def _classifier_model_name() -> str:
    return cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))

def _emotion_scorer_model_name() -> str:
    return str(cfg("OLLAMA_MIDDLE_MODEL", _classifier_model_name()) or _classifier_model_name())

def _emotion_score_uses_integer_scale() -> bool:
    return cfg_bool("EMOTION_SCORE_USE_INTEGER_SCALE", False)

def _emotion_score_schema() -> dict[str, Any]:
    if _emotion_score_uses_integer_scale():
        item_schema = {"type": "integer", "minimum": 0, "maximum": 10}
    else:
        item_schema = {"type": "number", "minimum": 0.0, "maximum": 1.0}
    return {
        "type": "object",
        "properties": {
            "appraisal": {"type": "string"},
            "reaction": {"type": "string", "enum": list(EMOTION_REACTION_VALUES)},
            **{key: dict(item_schema) for key in EMOTION_KEYS},
        },
        "required": ["appraisal", "reaction", *EMOTION_KEYS],
        "additionalProperties": False,
    }

def _normalize_emotion_scores_from_model(scores: dict[str, Any] | None) -> dict[str, float]:
    if not _emotion_score_uses_integer_scale():
        return normalize_llm_emotion_scores(scores)
    source = scores or {}
    normalized: dict[str, float] = {}
    for key in EMOTION_KEYS:
        try:
            normalized[key] = clamp_score(float(source.get(key, 0.0)) / 10.0)
        except Exception:
            normalized[key] = 0.0
    return normalized

def _normalize_scored_reaction(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in EMOTION_REACTION_VALUES else "NONE"

def _resolve_emotion_blend_params(incoming_scores: dict[str, float] | None) -> tuple[float, float, bool]:
    values = sorted((clamp_score((incoming_scores or {}).get(key, 0.0)) for key in EMOTION_KEYS), reverse=True)
    peak = values[0] if values else 0.0
    top_two_total = sum(values[:2])
    low_signal_peak = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_MAX_PEAK", 0.12) or 0.12))
    low_signal_total = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_MAX_TOTAL", 0.18) or 0.18))
    if peak <= low_signal_peak and top_two_total <= low_signal_total:
        decay = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_DECAY", 0.88) or 0.88))
        blend = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_BLEND_RATIO", 0.18) or 0.18))
        return decay, blend, True
    decay = clamp_score(float(cfg("EMOTION_DECAY", 0.92) or 0.92))
    blend = clamp_score(float(cfg("EMOTION_BLEND_RATIO", 0.35) or 0.35))
    return decay, blend, False
