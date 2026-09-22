from __future__ import annotations

import logging

from ..config_helpers import cfg
from ..discord_helpers import (
    author_id,
    channel_id,
    collect_reply_chain,
    content,
    display_name,
    format_message_reactions,
    find_reply_parent_from_cache,
    prune_reply_chain_from_cache,
    recent_context_items,
)
from ..emotion_helpers import format_emotion_tags_ja
from .. import memory_logic
from .. import prospective_memory
from .. import user_profile_logic
from ..working_memory import (
    build_working_context,
    format_working_context_for_prompt,
    needs_specific_recall,
)
from ..memory_store import MemoryStore
from .. import ollama_helpers
from ...ollama_chat_.ollama_chat_types import EmotionState, UserRelationship
from ...ollama_chat_.ollama_chat_texts import (
    INTENT_ANALYZER_SYSTEM_PROMPT,
    MEMORY_RELEVANCE_SELECTOR_SYSTEM_PROMPT,
    PRE_REPLY_ANALYZER_SYSTEM_PROMPT,
    USER_REPLY_BASE_PARTS,
    append_chat_reply_output_suffix,
    build_memory_selector_tail_parts,
    build_topic_carryover_guard_parts,
    build_user_prompt_extra_rule_parts,
    build_user_prompt_target_is_ai_parts,
)

from .budget import (
    _build_recent_turns,
    _clean_analyzer_keyword,
    _clean_analyzer_note,
    _clean_selected_memory_indices,
    _context_max_age_sec,
    _extract_prompt_keywords,
    _filter_prompt_context_lines,
    _format_episode_prompt_line,
    _get_relevant_episode_lines,
    _line_to_turn,
    _looks_like_noisy_analyzer_text,
    _looks_like_style_tag_keyword,
    _sanitize_loop_string,
    _trim_prompt_inputs,
    build_agent_persona_parts,
)
from .analyzer import (
    _EXTERNAL_URL_RE,
    _PRE_REPLY_ANALYZER_SCHEMA,
    _analyze_pre_reply_context,
    _classifier_model_name,
    _infer_user_intent_without_llm,
    _looks_like_external_research_context_request,
    _looks_like_x_timeline_research_request,
    _should_skip_intent_analysis,
    _should_skip_memory_selection,
    _trim_pre_reply_analyzer_inputs,
    detect_third_party_hearsay,
)
from .builder import _build_user_prompt
from .thread_tracker import ConversationThread, ThreadTracker

log = logging.getLogger("ollama_bot.common.chat_prompt")

__all__ = [
    # budget
    "_context_max_age_sec",
    "_line_to_turn",
    "_build_recent_turns",
    "_extract_prompt_keywords",
    "_format_episode_prompt_line",
    "_get_relevant_episode_lines",
    "_sanitize_loop_string",
    "_looks_like_noisy_analyzer_text",
    "_looks_like_style_tag_keyword",
    "_clean_analyzer_keyword",
    "_clean_analyzer_note",
    "_clean_selected_memory_indices",
    "build_agent_persona_parts",
    "_trim_prompt_inputs",
    "_filter_prompt_context_lines",
    # analyzer
    "_classifier_model_name",
    "_EXTERNAL_URL_RE",
    "_looks_like_x_timeline_research_request",
    "_looks_like_external_research_context_request",
    "_should_skip_intent_analysis",
    "_infer_user_intent_without_llm",
    "_should_skip_memory_selection",
    "_PRE_REPLY_ANALYZER_SCHEMA",
    "_trim_pre_reply_analyzer_inputs",
    "_analyze_pre_reply_context",
    "detect_third_party_hearsay",
    # builder
    "_build_user_prompt",
    # thread_tracker
    "ConversationThread",
    "ThreadTracker",
    # external modules & helpers for backwards compatibility
    "cfg",
    "author_id",
    "channel_id",
    "collect_reply_chain",
    "content",
    "display_name",
    "format_message_reactions",
    "find_reply_parent_from_cache",
    "prune_reply_chain_from_cache",
    "recent_context_items",
    "format_emotion_tags_ja",
    "memory_logic",
    "prospective_memory",
    "user_profile_logic",
    "build_working_context",
    "format_working_context_for_prompt",
    "needs_specific_recall",
    "MemoryStore",
    "ollama_helpers",
    "EmotionState",
    "UserRelationship",
    "INTENT_ANALYZER_SYSTEM_PROMPT",
    "MEMORY_RELEVANCE_SELECTOR_SYSTEM_PROMPT",
    "PRE_REPLY_ANALYZER_SYSTEM_PROMPT",
    "USER_REPLY_BASE_PARTS",
    "append_chat_reply_output_suffix",
    "build_memory_selector_tail_parts",
    "build_topic_carryover_guard_parts",
    "build_user_prompt_extra_rule_parts",
    "build_user_prompt_target_is_ai_parts",
]
