"""ユーザーの苛立ち検出とクールダウン管理"""
from __future__ import annotations

import re
import time
from typing import Any

from ollama_bot.common.config_helpers import cfg

_ANGER_PATTERNS = [
    r"💢",
    r"[！!]{3,}",
    r"(?:つってんだ|言ってんだ|言ってるだろ|何回言わせる|いい加減にしろ|ふざけんな)",
    r"(?:だから|ちがう|違う|そうじゃない)[！!]+",
]

_USER_SIGNALS: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
_COOLDOWNS: dict[tuple[int, int, int], float] = {}


def count_anger_markers(text: str) -> int:
    raw = str(text or "")
    count = 0
    for p in _ANGER_PATTERNS:
        matches = re.findall(p, raw)
        count += len(matches)
    return count


def record_user_signal(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    is_correction: bool,
    is_breakdown: bool,
    anger_markers: int,
    now: float | None = None,
) -> bool:
    if not cfg("FRUSTRATION_GUARD_ENABLED", True):
        return False
    ts = now if now is not None else time.time()
    key = (guild_id, channel_id, user_id)
    signals = _USER_SIGNALS.setdefault(key, [])
    # 10分以内のシグナルを維持
    signals[:] = [s for s in signals if (ts - float(s.get("ts", 0.0))) <= 600.0]
    signals.append({
        "ts": ts,
        "is_correction": is_correction,
        "is_breakdown": is_breakdown,
        "anger": anger_markers,
    })

    corr_count = sum(1 for s in signals if s.get("is_correction"))
    total_anger = sum(int(s.get("anger", 0)) for s in signals)
    recent_breakdowns = [bool(s.get("is_breakdown")) for s in signals[-2:]]
    breakdown_consecutive = len(recent_breakdowns) == 2 and all(recent_breakdowns)

    trigger_corr = int(cfg("FRUSTRATION_TRIGGER_CORRECTIONS", 2) or 2)
    cooldown_sec = float(cfg("FRUSTRATION_COOLDOWN_SEC", 600.0) or 600.0)

    if corr_count >= trigger_corr or total_anger >= 4 or breakdown_consecutive:
        _COOLDOWNS[key] = ts + cooldown_sec
        return True
    return False


def is_in_cooldown(*, guild_id: int, channel_id: int, user_id: int, now: float | None = None) -> bool:
    ts = now if now is not None else time.time()
    key = (guild_id, channel_id, user_id)
    exp = _COOLDOWNS.get(key)
    if exp is None:
        return False
    if ts >= exp:
        _COOLDOWNS.pop(key, None)
        return False
    return True


def clear_cooldown(*, guild_id: int, channel_id: int, user_id: int) -> None:
    _COOLDOWNS.pop((guild_id, channel_id, user_id), None)


def clear_all() -> None:
    """テスト用クリア関数"""
    _USER_SIGNALS.clear()
    _COOLDOWNS.clear()
