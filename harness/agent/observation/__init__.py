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

from .emotion_evaluator import EmotionEvaluation, EmotionObservationEvaluator
from .evaluator import ObservationEvaluator
from .facade import ObservationFacade, ObservationManager
from .fact_evaluator import FactCheckEvaluation, FactCheckEvaluator, FactCheckVerdict
from .feedback_observer import FeedbackObservation, FeedbackObserver
from .loop_detector import LoopDetector
from .memory import ObservationMemory, ScratchpadManager
from .observer import ObservationProcessor
from .parser import ObservationParser
from .reflection_evaluator import ReflectionEvaluation, ReflectionEvaluator
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
    "EmotionEvaluation",
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
]
