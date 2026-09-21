"""LLM生成文の内部指示漏れ、思考漏れ、オウム返し、Markdown残骸を検出・除去する安全モジュール。"""
from __future__ import annotations

from .cleaner import (
    _normalize_compare_text,
    _normalize_topic_echo_text,
    _normalize_unintended_watashi,
    _opening_echo_fragment,
    _strip_leading_list_marker,
    _strip_self_addressed_thanks,
    extract_effective_first_clause,
    strip_leading_interjections,
    strip_markdown_artifacts,
    strip_trailing_reasoning_notes,
    strip_unprompted_nerd_emoji,
)
from .control import (
    _extract_labeled_user_facing_line,
    _looks_like_analysis_label,
    _looks_like_control_line,
    _looks_like_conversational_reply_excerpt,
    _looks_like_fragmentary_meta_summary,
    _looks_like_instruction_leak_line,
    _looks_like_labeled_analysis_line,
    _looks_like_meta_reply_preface,
    _looks_like_structured_analysis_block,
    _looks_like_style_tag_outline_before_reply,
    _strip_leading_control_prefixes,
    _strip_leading_non_user_facing_paragraph,
    _strip_leading_style_tag_outline,
)
from .patterns import (
    _PROMPT_LEAK_PATTERNS,
    _REASONING_LEAK_PATTERNS,
    looks_like_multi_turn_output,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
)
from .salvage import (
    _looks_like_duplicate_think_echo,
    _looks_like_pre_think_leak,
    _strip_think_blocks,
    salvage_reply_from_think_block,
)
from .sanitizer import (
    extract_first_user_facing_reply,
    sanitize_generated_reply,
)
from .validation import (
    _is_compliance_or_acceptance_clause,
    _is_validation_ending_clause,
    _looks_like_opening_topic_echo,
    _looks_too_similar_to_previous_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_non_japanese_reply,
    looks_like_parrot_reply,
    looks_like_similar_to_previous_reply,
    looks_like_truncated_reply,
    looks_like_unusable_assistant_reply,
    looks_like_time_of_day_contradiction,
    looks_like_character_deviation,
)

__all__ = [
    # patterns
    "_PROMPT_LEAK_PATTERNS",
    "_REASONING_LEAK_PATTERNS",
    "looks_like_prompt_leak",
    "looks_like_reasoning_leak",
    "looks_like_multi_turn_output",
    # cleaner
    "strip_markdown_artifacts",
    "strip_trailing_reasoning_notes",
    "_strip_leading_list_marker",
    "_strip_self_addressed_thanks",
    "_normalize_unintended_watashi",
    "strip_unprompted_nerd_emoji",
    "_normalize_compare_text",
    "_normalize_topic_echo_text",
    "_opening_echo_fragment",
    "extract_effective_first_clause",
    "strip_leading_interjections",
    # control
    "_looks_like_analysis_label",
    "_looks_like_labeled_analysis_line",
    "_looks_like_structured_analysis_block",
    "_looks_like_instruction_leak_line",
    "_looks_like_control_line",
    "_extract_labeled_user_facing_line",
    "_strip_leading_control_prefixes",
    "_looks_like_style_tag_outline_before_reply",
    "_looks_like_meta_reply_preface",
    "_looks_like_conversational_reply_excerpt",
    "_looks_like_fragmentary_meta_summary",
    "_strip_leading_non_user_facing_paragraph",
    "_strip_leading_style_tag_outline",
    # salvage
    "salvage_reply_from_think_block",
    "_strip_think_blocks",
    "_looks_like_duplicate_think_echo",
    "_looks_like_pre_think_leak",
    # sanitizer
    "sanitize_generated_reply",
    "extract_first_user_facing_reply",
    # validation
    "_is_compliance_or_acceptance_clause",
    "_is_validation_ending_clause",
    "_looks_like_opening_topic_echo",
    "_looks_too_similar_to_previous_reply",
    "looks_like_parrot_reply",
    "looks_like_similar_to_previous_reply",
    "looks_like_truncated_reply",
    "looks_like_non_japanese_reply",
    "looks_like_unusable_assistant_reply",
    "looks_like_abnormal_assistant_reply",
    "looks_like_time_of_day_contradiction",
    "looks_like_character_deviation",
]
