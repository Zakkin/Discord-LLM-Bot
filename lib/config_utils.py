# lib/config_utils.py
from __future__ import annotations

from typing import Any, TypeVar

from ollama_bot.config_loader import config

T = TypeVar("T")


def cfg(name: str, default: Any = None) -> Any:
    """設定値を取得する。"""
    return getattr(config, name, default)


def cfg_str(name: str, default: str = "") -> str:
    """文字列設定値を安全に取得しトリムして返す。None時はdefaultを返す。"""
    value = cfg(name, default)
    return str(default if value is None else value).strip()


def cfg_bool(name: str, default: bool = False) -> bool:
    """真偽値設定値を取得する。"""
    value = cfg(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def cfg_int(name: str, default: int = 0) -> int:
    """整数設定値を取得する。変換失敗時はdefaultを返す。"""
    try:
        return int(cfg(name, default))
    except (TypeError, ValueError):
        return default


def cfg_float(name: str, default: float = 0.0) -> float:
    """浮動小数点設定値を取得する。変換失敗時はdefaultを返す。"""
    try:
        return float(cfg(name, default))
    except (TypeError, ValueError):
        return default


def cfg_int_set(name: str) -> set[int]:
    """整数セット設定値を取得する。"""
    raw = cfg(name, []) or []
    result: set[int] = set()
    for value in raw:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def model_supports_thinking() -> bool:
    """現在のモデルが思考タグ出力をサポートするかを判定する。"""
    model = str(cfg("OLLAMA_MODEL", "") or "").lower()
    non_thinking_markers = (
        "-instruct",
        ":instruct",
        "instruct-",
        "non-thinking",
    )
    return not any(marker in model for marker in non_thinking_markers)


def cfg_primary_channel_id(default: int = 0) -> int:
    """プライマリチャットチャンネルIDを取得する。"""
    value = cfg("OLLAMA_CHANNEL_ID", getattr(config, "Ollama_CHANNEL_ID", default))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def cfg_managed_channel_ids() -> set[int]:
    """Botが管理対象とするすべてのチャンネルIDのセットを取得する。"""
    result = set(cfg_int_set("OTHER_CHANNEL_IDS"))
    primary_id = cfg_primary_channel_id(0)
    if primary_id > 0:
        result.add(primary_id)
    return {int(v) for v in result if int(v) > 0}
