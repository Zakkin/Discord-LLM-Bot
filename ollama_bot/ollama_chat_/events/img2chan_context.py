"""img2chan分割Mixinから元モジュールの公開属性へアクセスする互換レイヤー。
テストや外部コードが `img2chan` 上で行うパッチを、分割後の各処理にも反映します。
"""
from __future__ import annotations

from typing import Any


def _events_module() -> Any:
    from . import img2chan

    return img2chan


class _LogProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_events_module().log, name)


log = _LogProxy()


def cfg(name: str, default: Any = None) -> Any:
    return _events_module().cfg(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    return _events_module().cfg_bool(name, default)


def cfg_int(name: str, default: int = 0) -> int:
    return _events_module().cfg_int(name, default)


def cfg_float(name: str, default: float = 0.0) -> float:
    return _events_module().cfg_float(name, default)


async def call_ollama_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _events_module().call_ollama_json(*args, **kwargs)


def json_dumps(*args: Any, **kwargs: Any) -> str:
    return _events_module().json_dumps(*args, **kwargs)


def _get_img2chan_helper() -> Any:
    return _events_module()._get_img2chan_helper()
