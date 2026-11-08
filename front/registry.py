"""front 層のアダプター / ドメインヘルパー登録レジストリ。

- **アダプター**: `PlatformAdapter` 実装をプラットフォーム名で登録・取得します。
- **ドメインヘルパー**: `harness/agent/tool_registry` が利用する x.com / ふたばの
  解析・取得ヘルパーモジュールを提供します（tool_registry 側の依存注入ポイントから呼ばれます）。
"""
from __future__ import annotations

import importlib
from typing import Any

from front.adapter import PlatformAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {}

DOMAIN_HELPER_MODULES: dict[str, str] = {
    "x": "front.x.parser",
    "hutaba": "front.hutaba.client.img2chan",
}
"""プラットフォーム名 → ドメインヘルパーモジュールの完全修飾名。"""


def register_adapter(adapter: PlatformAdapter) -> None:
    """アダプターを登録する（同一プラットフォームは上書き）。"""
    _ADAPTERS[str(adapter.platform)] = adapter


def get_adapter(platform: str) -> PlatformAdapter | None:
    """登録済みアダプターを返す。未登録なら None。"""
    return _ADAPTERS.get(platform)


def clear_adapters() -> None:
    """登録済みアダプターをすべて削除する（主にテスト用）。"""
    _ADAPTERS.clear()


def load_domain_helper(platform: str) -> Any:
    """プラットフォームのドメインヘルパーモジュールを import して返す。"""
    try:
        module_name = DOMAIN_HELPER_MODULES[platform]
    except KeyError as e:
        raise KeyError(f"no domain helper registered for platform={platform!r}") from e
    return importlib.import_module(module_name)


__all__ = [
    "DOMAIN_HELPER_MODULES",
    "clear_adapters",
    "get_adapter",
    "load_domain_helper",
    "register_adapter",
]
