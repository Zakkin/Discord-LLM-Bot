# lib/date_utils.py
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc


def now_jst() -> datetime:
    """現在の日時（JST / Asia/Tokyo）を取得する。"""
    return datetime.now(JST)


def now_utc() -> datetime:
    """現在の日時（UTC）を取得する。"""
    return datetime.now(UTC)


def now_ts() -> float:
    """現在のUnixタイムスタンプ（秒）を取得する。"""
    return time.time()


def from_timestamp_jst(ts: float) -> datetime:
    """UnixタイムスタンプからJST datetimeオブジェクトを生成する。"""
    return datetime.fromtimestamp(ts, tz=JST)


def format_jst_datetime(ts_or_dt: float | datetime, fmt: str = "%Y/%m/%d %H:%M:%S") -> str:
    """UnixタイムスタンプまたはdatetimeをJST文字列表現にフォーマットする。"""
    if isinstance(ts_or_dt, (int, float)):
        dt = from_timestamp_jst(float(ts_or_dt))
    elif isinstance(ts_or_dt, datetime):
        dt = ts_or_dt.astimezone(JST) if ts_or_dt.tzinfo else ts_or_dt.replace(tzinfo=JST)
    else:
        return ""
    return dt.strftime(fmt)


def is_in_quiet_window(now: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """指定時刻が静かな時間帯（start_hhmm〜end_hhmm）に含まれるかを判定する純粋関数。

    - start_hhmm / end_hhmm は 'HH:MM' 形式（例: '01:00', '07:00'）。
    - 日跨ぎ（例: '23:00'〜'06:00'）に完全対応すること。
    - 開始時刻ちょうどは True（含む）、終了時刻ちょうどは False（含まない）（半開区間 [start, end)）。
    - start_hhmm == end_hhmm の場合は終日無効として常に False を返す。
    - 形式不正時は ValueError を送出せず False を返すこと（フォールバック安全）。
    """
    if not start_hhmm or not end_hhmm or start_hhmm == end_hhmm:
        return False
    try:
        sh, sm = (int(x) for x in start_hhmm.strip().split(":", 1))
        eh, em = (int(x) for x in end_hhmm.strip().split(":", 1))
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            logger.warning("Quiet window hours/minutes out of range (%r, %r)", start_hhmm, end_hhmm)
            return False
    except Exception as e:
        logger.warning("Failed to parse quiet window times (%r, %r): %s", start_hhmm, end_hhmm, e)
        return False

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes == end_minutes:
        return False

    # JSTへの正規化
    target_dt = now.astimezone(JST) if now.tzinfo else now.replace(tzinfo=JST)
    cur_minutes = target_dt.hour * 60 + target_dt.minute

    if start_minutes < end_minutes:
        # 同日内（例: 01:00 〜 07:00）
        return start_minutes <= cur_minutes < end_minutes
    else:
        # 日跨ぎ（例: 23:00 〜 06:00）
        return cur_minutes >= start_minutes or cur_minutes < end_minutes

