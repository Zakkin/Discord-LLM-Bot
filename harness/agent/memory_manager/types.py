"""
[AI Agent Summary]
Memory Managerの型定義モジュール。
This module defines data structures, TypedDicts, and protocols for the memory management subsystem.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class MemoryRecord(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    memory_type: str
    content: str
    content_hash: str | None
    emotion_tags: str | None
    importance: float
    access_count: int
    source_message_id: int | None
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    distance: float | None
    rank: float | None


class MemoryEpisode(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    summary: str
    context_text: str | None
    key_topics: str | None
    importance: float
    created_at: str


class UserHabitProfile(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    user_id: int
    user_name: str | None
    habits: str
    sample_phrases: str | None
    observed_traits: str | None
    confidence: float
    updated_at: str


class UserInterest(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    topic: str
    detail: str | None
    keywords: str | None
    interest_level: float
    interaction_count: int
    created_at: str
    updated_at: str


class ReflectionBelief(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    scope: str
    belief: str
    evidence_count: int
    confidence: float
    is_active: int
    created_at: str
    updated_at: str


class FuturePromptRecord(TypedDict, total=False):
    id: int
    persona: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    trigger_condition: str
    prompt_text: str
    status: str
    created_at: str
    consumed_at: str | None


class ChannelSummaryRecord(TypedDict, total=False):
    channel_id: int
    persona: str
    summary: str
    topic_labels: str | None
    updated_at: str


@runtime_checkable
class MemoryStoreProtocol(Protocol):
    """MemoryStore のインターフェースプロトコル。"""

    db_path: str
    use_sqlite_vec: bool
    embed_dim: int

    async def init_db(self) -> None: ...
    async def close(self) -> None: ...

    async def add_memory(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        memory_type: str,
        content: str,
        embedding: list[float] | None = None,
        emotion_tags: list[str] | str | None = None,
        importance: float = 0.5,
        source_message_id: int | None = None,
    ) -> int: ...

    async def search_memories(
        self,
        *,
        persona: str,
        query: str,
        query_embedding: list[float] | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
        user_id: int | None = None,
        limit: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict[str, Any]]: ...
