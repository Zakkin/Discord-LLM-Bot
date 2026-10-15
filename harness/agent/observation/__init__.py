"""AIエージェント・ハーネス 結果の観察と評価（Observation）パッケージ

フェーズ4. 結果の観察と評価（Observation）を担当し、
・ツール実行結果の精査・正規化（ObservationParser / ObservationProcessor）
・短期記憶・スクラッチパッド更新（ScratchpadManager）
・目的達成度判定・再思考指示生成（ObservationEvaluator）
・エラーループ・同一呼び出し抑止（LoopDetector）
・状態遷移・次フェーズルーティング（ObservationRouter）
・統合オーケストレーションファサード（ObservationFacade）
・コンテキスト再合成（ObservationSynthesizer）
・各種ドメイン評価器（Web, FactCheck, Reply, Feedback, Reflection, Emotion）
を提供する。
"""
from __future__ import annotations

from .emotion_evaluator import (
    ActionPolicyEvaluation,
    EmotionEvaluation,
    EmotionObservationEvaluator,
    EmotionScoringEvaluation,
    blend_emotion_state_score,
    clamp_score,
    evaluate_action_policy,
    fallback_appraisal_text,
    get_emotion_score_schema,
    has_meaningful_emotion_signal,
    is_negative_mitigated_appraisal,
    is_neutral_or_decayed_appraisal,
    is_neutral_or_decayed_reason,
    is_time_decay_text,
    load_persisted_timestamp,
    looks_like_bad_appraisal_text,
    mitigate_wordplay_paranoid_anger,
    normalize_appraisal_text,
    normalize_emotion_scores_from_model,
    normalize_scored_reaction,
    peak_emotion_key,
    pick_dominant_emotion,
    reason_from_appraisal,
    repair_post_conversation_emotion_text,
    resolve_emotion_blend_params,
    resolve_low_signal_reuse,
)
from .evaluator import ObservationEvaluator
from .facade import ObservationFacade, ObservationManager
from .fact_evaluator import FactCheckEvaluation, FactCheckEvaluator, FactCheckVerdict
from .feedback_observer import FeedbackObservation, FeedbackObserver
from .loop_detector import LoopDetector
from .memory import ObservationMemory, ScratchpadManager
from .observer import ObservationProcessor
from .parser import ObservationParser
from .reflection_evaluator import (
    DeepReflectionEvaluation,
    ReflectionBatchEvaluation,
    ReflectionEvaluation,
    ReflectionEvaluator,
    evaluate_deep_reflection_result,
    evaluate_reflection_beliefs,
    validate_reflection_belief,
)
from .reply_evaluator import ReplyObservationEvaluator, ReplyObservationResult
from .router import ObservationRouter
from .synthesizer import ObservationSynthesizer
from .types import (
    EvaluationResult,
    EvaluationVerdict,
    LoopDetectionResult,
    NextStepPhase,
    Observation,
    ObservationRoutingResult,
    ObservationStatus,
    ScratchpadEntry,
)
from .web_evaluator import WebObservationEvaluator

__all__ = [
    # データモデル
    "Observation",
    "ObservationStatus",
    "EvaluationVerdict",
    "EvaluationResult",
    "LoopDetectionResult",
    "NextStepPhase",
    "ObservationRoutingResult",
    "ScratchpadEntry",
    "FactCheckVerdict",
    "FactCheckEvaluation",
    "FeedbackObservation",
    "ReplyObservationResult",
    "ReflectionEvaluation",
    "DeepReflectionEvaluation",
    "ReflectionBatchEvaluation",
    "EmotionEvaluation",
    "EmotionScoringEvaluation",
    "ActionPolicyEvaluation",
    # コンポーネント (Core Phase 4)
    "ObservationFacade",
    "ObservationManager",
    "ObservationParser",
    "ObservationProcessor",
    "ScratchpadManager",
    "ObservationMemory",
    "ObservationRouter",
    "ObservationEvaluator",
    "LoopDetector",
    "ObservationSynthesizer",
    # ドメイン別評価器
    "WebObservationEvaluator",
    "FactCheckEvaluator",
    "FeedbackObserver",
    "ReplyObservationEvaluator",
    "ReflectionEvaluator",
    "EmotionObservationEvaluator",
    # 評価ヘルパー関数
    "validate_reflection_belief",
    "evaluate_deep_reflection_result",
    "evaluate_reflection_beliefs",
    "clamp_score",
    "looks_like_bad_appraisal_text",
    "normalize_appraisal_text",
    "fallback_appraisal_text",
    "is_neutral_or_decayed_reason",
    "is_neutral_or_decayed_appraisal",
    "is_negative_mitigated_appraisal",
    "is_time_decay_text",
    "resolve_low_signal_reuse",
    "repair_post_conversation_emotion_text",
    "reason_from_appraisal",
    "has_meaningful_emotion_signal",
    "peak_emotion_key",
    "blend_emotion_state_score",
    "load_persisted_timestamp",
    "normalize_emotion_scores_from_model",
    "normalize_scored_reaction",
    "resolve_emotion_blend_params",
    "get_emotion_score_schema",
    "pick_dominant_emotion",
    "mitigate_wordplay_paranoid_anger",
    "evaluate_action_policy",
]
