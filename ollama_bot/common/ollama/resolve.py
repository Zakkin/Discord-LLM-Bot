# ollama_bot/common/ollama/resolve.py
from __future__ import annotations

import sys
from typing import Any, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def resolve_ollama_fn(name: str, fallback: F) -> F:
    """テストパッチや後方互換性のため、ollama_helpers やパッケージ上のオーバーライドを動的解決する。"""
    helpers = sys.modules.get("ollama_bot.common.ollama_helpers")
    if helpers is not None and hasattr(helpers, name):
        val = getattr(helpers, name)
        if val is not None and val is not fallback:
            return cast(F, val)
    pkg = sys.modules.get("ollama_bot.common.ollama")
    if pkg is not None and hasattr(pkg, name):
        val = getattr(pkg, name)
        if val is not None and val is not fallback:
            return cast(F, val)
    return fallback


def resolve_cfg(name: str, default: Any = None) -> Any:
    """_cfgの動的解決。ollama_helpers._cfg へのパッチを透過的に拾う。"""
    helpers = sys.modules.get("ollama_bot.common.ollama_helpers")
    if helpers is not None and hasattr(helpers, "_cfg"):
        mock_fn = getattr(helpers, "_cfg")
        if mock_fn is not None and getattr(mock_fn, "__name__", "") not in ("resolve_cfg", "_cfg_delegate"):
            return mock_fn(name, default)
    pkg = sys.modules.get("ollama_bot.common.ollama")
    if pkg is not None and hasattr(pkg, "_cfg"):
        mock_fn = getattr(pkg, "_cfg")
        if mock_fn is not None and getattr(mock_fn, "__name__", "") not in ("resolve_cfg", "_cfg_delegate"):
            return mock_fn(name, default)
    from ...config_loader import config
    return getattr(config, name, default)
