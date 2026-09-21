"""
[AI Agent Summary]
このファイルは、感情モジュール全体の汎用ユーティリティ（タイムスタンプ復元、スコア制限、テキスト正規化など）を担当します。
This file provides general utilities for the emotion module (timestamp restoration, score clamping, text normalization, etc.).
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

log = logging.getLogger("ollama_bot.ollama_chat")



def _load_persisted_timestamp(
    value: Any,
    *,
    now: float,
    field_name: str,
    default_on_old: float = 0.0,
) -> float:
    """JSONから永続化されたタイムスタンプを安全に復元する。

    Args:
        value: JSONから読み込んだ値。
        now: 現在の time.time() 値。
        field_name: ログ出力用のフィールド名。
        default_on_old: 古いmonotonicタイムスタンプ（< 100000000）だった場合の返却値。
            - クールダウン系フィールド（last_proactive_post_ts 等）は 0.0 を指定し、
              「十分前に実行済み＝クールダウン不要」として扱う。
            - elapsed計算用フィールド（last_agent_tick_ts）は now を指定し、
              「直近にtickした」として elapsed=0 からカウントを開始させる。
    """
    try:
        ts = float(value or 0.0)
    except Exception:
        return 0.0
    if ts <= 0.0:
        return 0.0

    # 古いmonotonicタイムスタンプ（Unixエポックより小さい値）を検出し、
    # フィールドの用途に合ったデフォルト値に差し替える
    if ts < 100000000.0:
        log.warning(
            "old monotonic timestamp detected, using default_on_old: "
            "field=%s value=%.3f now=%.3f default_on_old=%.3f",
            field_name,
            ts,
            now,
            default_on_old,
        )
        return default_on_old

    # 未来のタイムスタンプ(1日以上未来)になっている場合は now にリセット(時計の巻き戻り等への安全対策)
    if ts > now + 86400.0:
        log.warning(
            "reset future timestamp from emotion state: field=%s value=%.3f now=%.3f",
            field_name,
            ts,
            now,
        )
        return now
    return ts

def _has_meaningful_emotion_signal(scores: dict[str, float] | None, *, min_peak: float = 0.08) -> bool:
    source = scores or {}
    values = sorted((clamp_score(source.get(key, 0.0)) for key in EMOTION_KEYS), reverse=True)
    peak_threshold = clamp_score(float(cfg("EMOTION_SIGNAL_MIN_PEAK", min_peak) or min_peak))
    total_threshold = clamp_score(float(cfg("EMOTION_SIGNAL_MIN_TOTAL", 0.12) or 0.12))
    peak = values[0] if values else 0.0
    top_two_total = sum(values[:2])
    return peak >= peak_threshold or top_two_total >= total_threshold

def _blend_emotion_state_score(current: float, incoming: float, *, decay: float, blend: float) -> float:
    current_score = clamp_score(current)
    incoming_score = clamp_score(incoming)
    blended = clamp_score((current_score * decay) + (incoming_score * blend))
    if incoming_score >= current_score:
        return clamp_score(max(incoming_score, blended))
    return blended

def _normalize_appraisal_text(text: str, max_chars: int) -> str:
    cleaned = normalize_text(text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    from lib.config_utils import cfg
    bot_display = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if bot_display:
        cleaned = cleaned.replace(bot_display, "こちら")
        no_ai = re.sub(r"^(?:AI|bot|ボット)\s*", "", bot_display, flags=re.IGNORECASE).strip()
        if no_ai:
            cleaned = re.sub(rf"AI\s*{re.escape(no_ai)}[斯子]?", "こちら", cleaned)
    cleaned = cleaned.replace("ユーザー", "相手")
    cleaned = cleaned.strip(" 、。")
    if cleaned and not cleaned.endswith(("。", "！", "？")):
        cleaned += "。"
    return truncate_text(cleaned, max_chars)

def _looks_like_bad_appraisal_text(text: str) -> bool:
    cleaned = str(text or "").strip()
    if len(cleaned) < 4:
        return True
    if re.fullmatch(r"[\.\u3002…・\-\s]+", cleaned):
        return True
    if contains_cjk_non_japanese(cleaned):
        return True
    for pattern in (
        r"雑菌要求",
        r"AI.*?うんこ.*?してください",
        r"^json",
        r"^\{",
    ):
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            return True
    return False

def _peak_emotion_key(scores: dict[str, float] | None, *, min_score: float = 0.18) -> str:
    source = scores or {}
    best_key = max(EMOTION_KEYS, key=lambda key: clamp_score(source.get(key, 0.0)), default="neutral")
    if clamp_score(source.get(best_key, 0.0)) < min_score:
        return "neutral"
    return best_key

def _fallback_appraisal_text(emotion_key: str) -> str:
    fallback_map = {
        "joy": "好意的に受け取った出来事だと感じる。",
        "anticipation": "この先の反応や続きが気になる出来事だと感じる。",
        "anger": "少し突っかかられたように感じる。",
        "disgust": "距離を取りたくなる言い方だと感じる。",
        "sadness": "気持ちが沈む受け取り方になった。",
        "surprise": "予想外で少し戸惑う出来事だと感じる。",
        "fear": "少し身構えたくなる言い方だと感じる。",
        "neutral": "大きな出来事とは受け取っていない。",
    }
    return fallback_map.get(emotion_key, "大きな出来事とは受け取っていない。")

def _reason_from_appraisal(appraisal: str | None, emotion_key: str, *, max_chars: int) -> str:
    base = str(appraisal or "").strip().strip("。")
    if not base or _looks_like_bad_appraisal_text(str(appraisal or "").strip()):
        return fallback_reason_text(emotion_key)
    reason = normalize_reason_text(f"{base}からです。", max_chars)
    if looks_like_bad_reason_text(reason):
        return fallback_reason_text(emotion_key)
    return reason
