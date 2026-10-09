"""
[AI Agent Summary]
MemoryStoreクラスの統合定義モジュール。
This module bundles all memory storage mixins into the unified MemoryStore class.
"""
from __future__ import annotations

from .storage.base import _MemoryStoreBase
from .storage.core_store import _MemoryCoreMixin
from .storage.interactions import _MemoryInteractionsMixin
from .storage.learning import _MemoryLearningMixin
from .storage.profiles import _MemoryProfilesMixin


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
    pass


__all__ = ["MemoryStore"]
