"""
[AI Agent Summary]
このファイルは、感情分析に使用するLLMのモデル名取得や出力スキーマ生成、モデル出力スコアの正規化ロジックを担当します。
結果の観察と評価（Observation / Phase 4）に関わるスキーマ定義、スコア正規化、ブレンドパラメータ解決は
harness.agent.observation.emotion_evaluator へ移行され、本モジュールは設定連携および委譲層として機能します。
"""
from __future__ import annotations

from typing import Any

from ...common.config_helpers import cfg, cfg_bool
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    clamp_score,
    normalize_llm_emotion_scores,
)
from harness.agent.observation.emotion_evaluator import (
    EMOTION_REACTION_VALUES,
    EMOTION_REASON_SCHEMA,
    get_emotion_score_schema as _harness_get_emotion_score_schema,
    normalize_emotion_scores_from_model as _harness_normalize_emotion_scores,
    normalize_scored_reaction as _harness_normalize_scored_reaction,
    resolve_emotion_blend_params as _harness_resolve_emotion_blend_params,
)


def _classifier_model_name() -> str:
    return cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))

def _emotion_scorer_model_name() -> str:
    return str(cfg("OLLAMA_MIDDLE_MODEL", _classifier_model_name()) or _classifier_model_name())

def _emotion_score_uses_integer_scale() -> bool:
    return cfg_bool("EMOTION_SCORE_USE_INTEGER_SCALE", False)

def _emotion_score_schema() -> dict[str, Any]:
    """感情スコアリング用のスキーマを生成する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    return _harness_get_emotion_score_schema(integer_scale=_emotion_score_uses_integer_scale())

def _normalize_emotion_scores_from_model(scores: dict[str, Any] | None) -> dict[str, float]:
    """LLMのスコアリング結果を0.0〜1.0へ正規化する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    if not _emotion_score_uses_integer_scale():
        return normalize_llm_emotion_scores(scores)
    return _harness_normalize_emotion_scores(scores, integer_scale=True)

def _normalize_scored_reaction(value: Any) -> str:
    """LLMのリアクション評価値を正規化する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    return _harness_normalize_scored_reaction(value)

def _resolve_emotion_blend_params(incoming_scores: dict[str, float] | None) -> tuple[float, float, bool]:
    """感情スコア群のシグナル強度を観察し、ブレンドパラメータを決定する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    low_signal_peak = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_MAX_PEAK", 0.20) or 0.20))
    low_signal_total = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_MAX_TOTAL", 0.28) or 0.28))
    low_decay = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_DECAY", 0.88) or 0.88))
    low_blend = clamp_score(float(cfg("EMOTION_LOW_SIGNAL_BLEND_RATIO", 0.18) or 0.18))
    normal_decay = clamp_score(float(cfg("EMOTION_DECAY", 0.92) or 0.92))
    normal_blend = clamp_score(float(cfg("EMOTION_BLEND_RATIO", 0.35) or 0.35))
    return _harness_resolve_emotion_blend_params(
        incoming_scores,
        low_signal_peak=low_signal_peak,
        low_signal_total=low_signal_total,
        low_decay=low_decay,
        low_blend=low_blend,
        normal_decay=normal_decay,
        normal_blend=normal_blend,
    )
