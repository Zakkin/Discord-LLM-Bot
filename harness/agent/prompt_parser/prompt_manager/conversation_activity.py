"""会話活動レジストリ（§133）。

チャンネルごとの最終活動時刻（人間の発言・Bot の返信）を保持し、
「いずれかのチャンネルで直近 window_sec 秒以内に活動があったか」を返す。
プロセス内メモリのみで保持し、永続化しない。
"""
from __future__ import annotations

import time

_LAST_ACTIVITY_BY_CHANNEL: dict[int, float] = {}


def mark_channel_activity(channel_id: int, *, now: float | None = None) -> None:
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return
    _LAST_ACTIVITY_BY_CHANNEL[cid] = time.time() if now is None else float(now)


def last_activity_ts() -> float:
    if not _LAST_ACTIVITY_BY_CHANNEL:
        return 0.0
    return max(_LAST_ACTIVITY_BY_CHANNEL.values())


def is_conversation_active(*, window_sec: float, now: float | None = None) -> bool:
    last = last_activity_ts()
    if last <= 0.0:
        return False
    current = time.time() if now is None else float(now)
    return (current - last) <= max(float(window_sec), 0.0)


def select_analyzer_model(
    *,
    default_model: str,
    main_model: str,
    enabled: bool,
    window_sec: float,
    now: float | None = None,
) -> tuple[str, str]:
    """(model_name, reason) を返す。

    reason は "conversation_active" / "default" / "disabled" / "no_main_model" のいずれか。
    """
    if not enabled:
        return default_model, "disabled"
    main = str(main_model or "").strip()
    if not main:
        return default_model, "no_main_model"
    if is_conversation_active(window_sec=window_sec, now=now):
        return main, "conversation_active"
    return default_model, "default"
