"""
データベース (aiosqlite) 接続・カーソル・クエリ処理に関する共通ユーティリティ。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

__all__ = [
    "open_cursor",
    "fetch_all_dicts",
    "fetch_one_dict",
]


@asynccontextmanager
async def open_cursor(conn: Any, *, commit: bool = False) -> AsyncIterator[aiosqlite.Cursor]:
    """コネクションからカーソルを取得し、ブロック終了時に確実にクローズするコンテキストマネージャ。
    commit=True の場合は正常終了時に conn.commit() を実行する。
    """
    cur: aiosqlite.Cursor = await conn.cursor()
    try:
        yield cur
        if commit:
            await conn.commit()
    finally:
        await cur.close()


async def fetch_all_dicts(cur: aiosqlite.Cursor) -> list[dict[str, Any]]:
    """カーソルから全行を取得し、辞書のリストとして返す。"""
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_one_dict(cur: aiosqlite.Cursor) -> dict[str, Any] | None:
    """カーソルから1行を取得し、辞書として返す。行がなければ None を返す。"""
    row = await cur.fetchone()
    return dict(row) if row is not None else None
