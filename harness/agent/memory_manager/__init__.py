"""
[AI Agent Summary]
ハーネス 1-3. メモリマネージャー (Memory Manager) パッケージのエントリポイント。
Entry point for Harness 1-3 Memory Manager, exporting MemoryStore, logic, and reflection utilities.
"""
from __future__ import annotations

from .extractor import (
    ALLOWED_MEMORY_TYPES,
    BLOCKED_MEMORY_TYPES,
    MEMORY_EXTRACTION_SCHEMA,
    extract_and_store_memory,
    normalize_memory_text,
)
from .reflection import (
    DEEP_REFLECTION_SYSTEM_PROMPT,
    DEFAULT_REFLECTION_SYSTEM_PROMPT,
    build_deep_reflection_prompt,
    build_reflection_memory_lines,
    build_reflection_prompt,
    is_reflection_belief_valid,
    merge_reflection_rows,
    normalize_reflection_belief,
)
from .retriever import get_relevant_memories
from .store import MemoryStore
from .summarizer import (
    HABIT_PROFILE_SCHEMA,
    SUMMARY_SCHEMA,
    format_user_habit_profile_lines,
    get_user_habit_profile,
    get_user_habit_profile_lines,
    maybe_store_training_candidate,
    maybe_update_channel_summary,
    update_user_habit_profile,
)
from .user_identity_resolver import (
    extract_user_id_from_tag,
    format_user_internal_tag,
    get_user_aliases_from_store,
    render_user_facing_text,
    replace_aliases_with_current_name,
    resolve_user_display_name,
    strip_user_internal_tags,
)
from .types import (
    ChannelSummaryRecord,
    FuturePromptRecord,
    MemoryEpisode,
    MemoryRecord,
    MemoryStoreProtocol,
    ReflectionBelief,
    UserHabitProfile,
    UserInterest,
)

__all__ = [
    # Store
    "MemoryStore",
    "MemoryStoreProtocol",
    # User Identity Resolver
    "extract_user_id_from_tag",
    "format_user_internal_tag",
    "strip_user_internal_tags",
    "replace_aliases_with_current_name",
    "render_user_facing_text",
    "resolve_user_display_name",
    "get_user_aliases_from_store",
    # Types
    "MemoryRecord",
    "MemoryEpisode",
    "UserHabitProfile",
    "UserInterest",
    "ReflectionBelief",
    "FuturePromptRecord",
    "ChannelSummaryRecord",
    # Logic & Pipeline
    "get_relevant_memories",
    "extract_and_store_memory",
    "update_user_habit_profile",
    "get_user_habit_profile",
    "format_user_habit_profile_lines",
    "get_user_habit_profile_lines",
    "maybe_store_training_candidate",
    "normalize_memory_text",
    "normalize_reflection_belief",
    # Reflection
    "DEFAULT_REFLECTION_SYSTEM_PROMPT",
    "DEEP_REFLECTION_SYSTEM_PROMPT",
    "build_reflection_prompt",
    "build_deep_reflection_prompt",
    "build_reflection_memory_lines",
    "is_reflection_belief_valid",
    "merge_reflection_rows",
    # Schemas & Types
    "MEMORY_EXTRACTION_SCHEMA",
    "SUMMARY_SCHEMA",
    "HABIT_PROFILE_SCHEMA",
    "ALLOWED_MEMORY_TYPES",
    "BLOCKED_MEMORY_TYPES",
]
