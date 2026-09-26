# lib/config_utils.py
from __future__ import annotations

from typing import Any, TypeVar

from ollama_bot import config_loader

T = TypeVar("T")


def get_config() -> Any:
    """現在ロードされている設定オブジェクトを取得する。"""
    return getattr(config_loader, "config", None)


def cfg(name: str, default: Any = None) -> Any:
    """設定値を取得する。"""
    conf = get_config()
    return getattr(conf, name, default) if conf is not None else default


def __getattr__(name: str) -> Any:
    """モジュール属性（config等）の動的委譲。"""
    if name == "config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """整数セット設定値を取得する。環境変数等でカンマ区切り文字列が渡された場合も正しく処理する。"""
    raw = cfg(name, []) or []
    # 文字列が渡された場合（環境変数等）はカンマ分割してからパースする
    if isinstance(raw, str):
        raw = [v.strip() for v in raw.split(",") if v.strip()]
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
    cfg_obj = get_config()
    fallback = getattr(cfg_obj, "OLLAMA_CHANNEL_ID", default) if cfg_obj is not None else default
    value = cfg("OLLAMA_CHANNEL_ID", fallback)
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
