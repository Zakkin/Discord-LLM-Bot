# lib/date_utils.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
