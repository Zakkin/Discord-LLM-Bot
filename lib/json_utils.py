"""
JSON の安全な読み込みおよびアトミック書き込みに関するユーティリティ。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "save_json_atomic",
    "save_json_atomic_async",
    "load_json",
    "load_json_async",
]


def save_json_atomic(filepath: str | Path, data: Any, *, indent: int = 2) -> None:
    """一時ファイルを経由してアトミックにJSONを書き込む (flush -> fsync -> os.replace)。"""
    target = Path(filepath).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(f"{target.suffix}.tmp")
    try:
        with open(tmp_target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_target, target)
    except Exception:
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except Exception:
                pass
        raise


async def save_json_atomic_async(filepath: str | Path, data: Any, *, indent: int = 2) -> None:
    """save_json_atomic を非同期 (asyncio.to_thread) で実行する。"""
    await asyncio.to_thread(save_json_atomic, filepath, data, indent=indent)


def load_json(filepath: str | Path, *, default: Any = None) -> Any:
    """JSONファイルを安全に読み込む。ファイルが存在しないか壊れている場合は default を返す。"""
    target = Path(filepath)
    if not target.is_file():
        return default
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("JSON read failed for %s: %r", target, e)
        return default


async def load_json_async(filepath: str | Path, *, default: Any = None) -> Any:
    """load_json を非同期 (asyncio.to_thread) で実行する。"""
    return await asyncio.to_thread(load_json, filepath, default=default)
