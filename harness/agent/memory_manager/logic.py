"""
[AI Agent Summary]
Memory Managerの統合ロジックファサードモジュール。
Unified logic facade module for Memory Manager, aggregating extractor, retriever, and summarizer.
"""
from __future__ import annotations

from typing import Any

from ollama_bot.common import ollama_helpers
from ollama_bot.common.emotion_helpers import (
    EMOTION_KEYS,
    EMOTION_LABEL_TO_JA,
    build_memory_emotion_tags,
    format_emotion_tags_ja,
)

from .extractor import (
    ALLOWED_MEMORY_TYPES,
    BLOCKED_MEMORY_TYPES,
    MEMORY_EXTRACTION_SCHEMA,
    _ALLOWED_ASSOCIATED_EMOTIONS,
    _ASSOCIATED_EMOTION_ALIASES,
    _build_dialogue_source,
    _extract_content_meaningful_tokens,
    _is_assistant_quote_like,
    _is_fallback_reply,
    _is_hallucinated_emotion_reason_memory,
    _is_ignorable_call_error,
    _is_self_referential_memory,
    _is_single_turn_unsafe_or_low_value,
    _is_swapped_user_preference_memory,
    _is_unsupported_hallucinated_memory,
    _join_context_lines,
    _looks_like_noise_memory,
    _merge_memory_emotion_tags,
    _normalize_associated_emotion,
    _normalize_score,
    _persona_from_cfg,
    normalize_memory_text,
)
from .retriever import (
    _build_fts_query,
    _cfg_bool,
    _cosine_similarity,
    _embed_model_from_cfg,
    _format_memory_line,
    _parse_emotion_tags,
    _relative_time_label,
    _same_id,
)
from .summarizer import (
    HABIT_PROFILE_SCHEMA,
    SUMMARY_SCHEMA,
    _habit_profile_model_name,
    _profile_score,
    format_user_habit_profile_lines,
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
from .post_reply_handler import handle_post_reply_memory_and_summary, maybe_store_episode_memory
from . import extractor, retriever, summarizer, user_identity_resolver, post_reply_handler


async def get_relevant_memories(*args: Any, **kwargs: Any) -> Any:
    return await retriever.get_relevant_memories(*args, **kwargs)


async def extract_and_store_memory(*args: Any, **kwargs: Any) -> Any:
    return await extractor.extract_and_store_memory(*args, **kwargs)


async def update_user_habit_profile(*args: Any, **kwargs: Any) -> Any:
    return await summarizer.update_user_habit_profile(*args, **kwargs)


async def get_user_habit_profile(*args: Any, **kwargs: Any) -> Any:
    return await summarizer.get_user_habit_profile(*args, **kwargs)


async def get_user_habit_profile_lines(*args: Any, **kwargs: Any) -> Any:
    return await summarizer.get_user_habit_profile_lines(*args, **kwargs)


async def maybe_store_training_candidate(*args: Any, **kwargs: Any) -> Any:
    return await summarizer.maybe_store_training_candidate(*args, **kwargs)


async def maybe_update_channel_summary(*args: Any, **kwargs: Any) -> Any:
    return await summarizer.maybe_update_channel_summary(*args, **kwargs)


__all__ = [
    # Schemas & Constants
    "MEMORY_EXTRACTION_SCHEMA",
    "SUMMARY_SCHEMA",
    "HABIT_PROFILE_SCHEMA",
    "ALLOWED_MEMORY_TYPES",
    "BLOCKED_MEMORY_TYPES",
    "_ALLOWED_ASSOCIATED_EMOTIONS",
    "_ASSOCIATED_EMOTION_ALIASES",
    # Helper functions
    "_cosine_similarity",
    "_embed_model_from_cfg",
    "_cfg_bool",
    "_same_id",
    "_normalize_score",
    "_profile_score",
    "_is_self_referential_memory",
    "_looks_like_noise_memory",
    "_is_assistant_quote_like",
    "_is_single_turn_unsafe_or_low_value",
    "_extract_content_meaningful_tokens",
    "_build_dialogue_source",
    "_is_unsupported_hallucinated_memory",
    "_is_hallucinated_emotion_reason_memory",
    "_is_fallback_reply",
    "_is_ignorable_call_error",
    "_normalize_associated_emotion",
    "_merge_memory_emotion_tags",
    "_join_context_lines",
    "_persona_from_cfg",
    "_parse_emotion_tags",
    "_relative_time_label",
    "_format_memory_line",
    "_build_fts_query",
    "_habit_profile_model_name",
    "ollama_helpers",
    # Main logic functions
    "get_relevant_memories",
    "extract_and_store_memory",
    "update_user_habit_profile",
    "get_user_habit_profile",
    "format_user_habit_profile_lines",
    "get_user_habit_profile_lines",
    "maybe_store_training_candidate",
    "maybe_update_channel_summary",
    "handle_post_reply_memory_and_summary",
    "maybe_store_episode_memory",
    "normalize_memory_text",
    "extract_user_id_from_tag",
    "format_user_internal_tag",
    "strip_user_internal_tags",
    "replace_aliases_with_current_name",
    "render_user_facing_text",
    "resolve_user_display_name",
    "get_user_aliases_from_store",
    "EMOTION_KEYS",
    "EMOTION_LABEL_TO_JA",
    "build_memory_emotion_tags",
    "format_emotion_tags_ja",
]
