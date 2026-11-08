"""
[AI Agent Summary]
MemoryStoreクラスの統合定義モジュール。
This module bundles all memory storage mixins into the unified MemoryStore class.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .storage.base import _MemoryStoreBase
from .storage.core_store import _MemoryCoreMixin
from .storage.interactions import _MemoryInteractionsMixin
from .storage.learning import _MemoryLearningMixin
from .storage.profiles import _MemoryProfilesMixin

if TYPE_CHECKING:
    from .slang import SlangEntry


class MemoryStore(
    _MemoryCoreMixin,
    _MemoryInteractionsMixin,
    _MemoryLearningMixin,
    _MemoryProfilesMixin,
    _MemoryStoreBase,
):
    """
    非同期 SQLite / sqlite-vec / FTS5 ベースの記憶・学習・プロファイル統合ストア。
    Unified asynchronous SQLite, sqlite-vec, and FTS5 based memory, learning, and profile store.
    """

    async def seed_default_slangs(self) -> int:
        """memory.db の bbs_slangs テーブルにデフォルトスラング辞書を一括登録する。"""
        from .slang import seed_default_slangs
        return await seed_default_slangs(self)

    async def upsert_slang(self, entry: SlangEntry, source: str = "manual") -> bool:
        """スラングエントリを bbs_slangs テーブルに新規追加または更新する。"""
        from .slang import upsert_slang
        return await upsert_slang(self, entry, source=source)

    async def load_slangs_from_db(self) -> dict[str, SlangEntry]:
        """DBからスラング一覧をロードし、インメモリキャッシュを同期する。"""
        from .slang import load_slangs_from_db
        return await load_slangs_from_db(self)


__all__ = ["MemoryStore"]
