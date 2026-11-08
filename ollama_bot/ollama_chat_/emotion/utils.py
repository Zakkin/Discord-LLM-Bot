"""
[AI Agent Summary]
このファイルは、感情モジュール全体の汎用ユーティリティを担当します。
結果の観察と評価（Observation / Phase 4）に関わるコアロジックは harness.agent.observation.emotion_evaluator へ移行され、
本モジュールは後方互換性を保持するためのアダプター・委譲層として機能します。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from lib.text_utils import normalize_text, truncate_text
from ...common.config_helpers import cfg
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    clamp_score,
    contains_cjk_non_japanese,
    fallback_reason_text,
    looks_like_bad_reason_text,
    normalize_reason_text,
)

from harness.agent.observation.emotion_evaluator import (
    blend_emotion_state_score as _harness_blend_emotion_state_score,
    fallback_appraisal_text as _fallback_appraisal_text,
    has_meaningful_emotion_signal as _harness_has_meaningful_emotion_signal,
    is_negative_mitigated_appraisal,
    is_neutral_or_decayed_appraisal,
    is_neutral_or_decayed_reason,
    load_persisted_timestamp as _harness_load_persisted_timestamp,
    looks_like_bad_appraisal_text as _looks_like_bad_appraisal_text,
    normalize_appraisal_text as _harness_normalize_appraisal_text,
    peak_emotion_key as _peak_emotion_key,
    reason_from_appraisal as _reason_from_appraisal,
)

log = logging.getLogger("ollama_bot.ollama_chat")


def _load_persisted_timestamp(
    value: Any,
    *,
    now: float,
    field_name: str,
    default_on_old: float = 0.0,
) -> float:
    """JSONから永続化されたタイムスタンプを安全に復元する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    return _harness_load_persisted_timestamp(
        value,
        now=now,
        field_name=field_name,
        default_on_old=default_on_old,
    )


def _has_meaningful_emotion_signal(scores: dict[str, float] | None, *, min_peak: float = 0.08) -> bool:
    """有意な感情シグナルの有無を判定する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    min_peak_val = clamp_score(float(cfg("EMOTION_SIGNAL_MIN_PEAK", min_peak) or min_peak))
    min_total_val = clamp_score(float(cfg("EMOTION_SIGNAL_MIN_TOTAL", 0.12) or 0.12))
    return _harness_has_meaningful_emotion_signal(
        scores,
        min_peak=min_peak_val,
        min_total=min_total_val,
    )


def _blend_emotion_state_score(current: float, incoming: float, *, decay: float, blend: float) -> float:
    """感情スコアの新旧ブレンドを計算する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    return _harness_blend_emotion_state_score(current, incoming, decay=decay, blend=blend)


def _normalize_appraisal_text(text: str, max_chars: int) -> str:
    """出来事評価テキストを正規化・サニタイズする（harness.agent.observation.emotion_evaluatorへ委譲）。"""
    from lib.config_utils import cfg as lib_cfg
    bot_display = str(lib_cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    return _harness_normalize_appraisal_text(text, max_chars=max_chars, bot_display=bot_display)


__all__ = [
    "_load_persisted_timestamp",
    "_has_meaningful_emotion_signal",
    "_blend_emotion_state_score",
    "_normalize_appraisal_text",
    "_looks_like_bad_appraisal_text",
    "_peak_emotion_key",
    "_fallback_appraisal_text",
    "_reason_from_appraisal",
    "is_neutral_or_decayed_reason",
    "is_neutral_or_decayed_appraisal",
    "is_negative_mitigated_appraisal",
]


