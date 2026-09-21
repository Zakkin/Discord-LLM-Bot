"""
[AI Agent Summary]
後方互換性のためのMemoryStoreアグリゲーター。実装は ollama_bot.common.memory パッケージに移動しました。
Backward-compatibility aggregator for MemoryStore. Implementations are moved to ollama_bot.common.memory package.
"""
from __future__ import annotations

from .memory.base import (
    _ConnectionLease,
    _MemoryStoreBase,
    _SQLITE_VEC_AVAILABLE,
    sqlite_vec,
    utcnow_iso,
)
from .memory.core_store import _MemoryCoreMixin
from .memory.interactions import _MemoryInteractionsMixin
from .memory.learning import _MemoryLearningMixin
from .memory.profiles import _MemoryProfilesMixin
from .memory.store import MemoryStore

__all__ = [
    "MemoryStore",
    "_ConnectionLease",
    "_MemoryStoreBase",
    "_MemoryCoreMixin",
    "_MemoryInteractionsMixin",
    "_MemoryLearningMixin",
    "_MemoryProfilesMixin",
    "_SQLITE_VEC_AVAILABLE",
    "sqlite_vec",
    "utcnow_iso",
]
