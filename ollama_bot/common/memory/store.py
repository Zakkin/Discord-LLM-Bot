"""
[AI Agent Summary]
MemoryStoreクラスの統合定義モジュール。
This module bundles all memory mixins into the unified MemoryStore class.
"""
from __future__ import annotations

from .base import _MemoryStoreBase
from .core_store import _MemoryCoreMixin
from .interactions import _MemoryInteractionsMixin
from .learning import _MemoryLearningMixin
from .profiles import _MemoryProfilesMixin


class MemoryStore(
    _MemoryCoreMixin,
    _MemoryInteractionsMixin,
    _MemoryLearningMixin,
    _MemoryProfilesMixin,
    _MemoryStoreBase,
):
    """
    非同期 SQLite ベースの記憶・学習・プロファイル管理ストア。
    Asynchronous SQLite-based memory, learning, and user profile management store.
    """
    pass
