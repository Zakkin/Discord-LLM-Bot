"""§175: 調査タイムアウト時の simple_search フォールバック判定（純粋関数群）。"""
from __future__ import annotations

from typing import Any, Callable

FALLBACK_ENABLED_SETTING: str = "RESEARCH_SIMPLE_SEARCH_FALLBACK_ENABLED"
_TIMEOUT_MARKER: str = "タイムアウト"

cfg_bool: Callable[[str, bool], bool] | None
try:
    from ollama_bot.common.config_helpers import cfg_bool as _cfg_bool_impl
    cfg_bool = _cfg_bool_impl
except Exception:
    cfg_bool = None

def is_research_timeout_error(result: Any) -> bool:
    """調査結果が「タイムアウトによる失敗」かどうかを決定的に判定する。

    result が dict で、その error 文字列に「タイムアウト」を含む場合にのみ True。
    dict でない・error が無い・error が空・別種の error の場合は False。
    """
    if not isinstance(result, dict):
        return False
    raw_error = result.get("error")
    if not isinstance(raw_error, str):
        return False
    error_text = raw_error.strip()
    if not error_text:
        return False
    return _TIMEOUT_MARKER in error_text


def is_research_fallback_enabled() -> bool:
    """RESEARCH_SIMPLE_SEARCH_FALLBACK_ENABLED（既定 True）を読む。取得失敗時は True。"""
    if cfg_bool is None:
        return True
    try:
        return bool(cfg_bool(FALLBACK_ENABLED_SETTING, True))
    except Exception:
        return True
