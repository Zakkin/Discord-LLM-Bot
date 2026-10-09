"""img.2chan.net helper facade (互換エイリアス).

実装本体および分割パーツ（img2channet_parts/）の読み込みは
harness.agent.tool_registry.mcp.chrome.img2chan に集約されています。
モジュール属性およびテスト用内部関数への完全な後方互換アクセスのため、
img2chan から全シンボル（内部関数含む）を透過的に再エクスポートします。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "AGENTS.md").is_file() or (p / ".git").is_dir():
            return p
    return start.parents[5]


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from . import img2chan as _img2chan_mod
except (ImportError, ValueError):
    import harness.agent.tool_registry.mcp.chrome.img2chan as _img2chan_mod

for _k, _v in _img2chan_mod.__dict__.items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v

__all__ = [name for name in dir(_img2chan_mod) if not name.startswith("__")]
