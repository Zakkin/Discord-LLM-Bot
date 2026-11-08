from __future__ import annotations

import sys
from typing import Any


def get_public_attr(module_name: str, name: str, default: Any = None) -> Any:
    """ロード済みモジュールから安全に属性を取得する。テスト時のモンキーパッチや循環参照回避用。"""
    module = sys.modules.get(module_name)
    return getattr(module, name, default) if module is not None else default
