"""ハーネス 1-1. パーサー (フェーズ2)

LLM出力からの構造化データ抽出（JSONパース・スキーマ検証）、
思考タグ・制御行・内部指示漏れの除去・サニタイズ、
および出力品質・安全性検証を担当する。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("harness.agent.prompt_parser.parser")

from .actions import (
    ParsedToolCall,
    execute_action,
    extract_thought_and_action,
    is_mcp_tool,
    parse_tool_calls,
)

from .cleaner import (
    _normalize_compare_text,
    _normalize_topic_echo_text,
    _normalize_unintended_watashi,
    _opening_echo_fragment,
    _strip_leading_list_marker,
    _strip_self_addressed_thanks,
    sanitize_partner_misaddressing,
    strip_bot_self_mentions,
    extract_effective_first_clause,
    strip_leading_interjections,
    strip_markdown_artifacts,
    strip_trailing_reasoning_notes,
    strip_unprompted_nerd_emoji,
    strip_mimicked_kaomoji,
    strip_hanging_tail_echo,
    sanitize_false_style_accusation,
    is_kaomoji,
    extract_kaomoji_candidates,
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
    looks_like_copied_log_appraisal,
    looks_like_placeholder_thought,
    sanitize_generated_reply,
    sanitize_unknown_denial_for_known_subject,
    sanitize_abnormal_rejection,
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
    looks_like_abnormal_rejection,
    looks_like_unknown_or_incomprehension_reply,
    should_skip_unknown_reply,
    looks_like_mimicked_kaomoji_or_hanging_tail,
    looks_like_false_style_accusation,
    looks_like_self_contradicting_reply,
    looks_like_unknown_word_incomprehension,
    extract_unknown_incomprehension_word,
)
from .structured import (
    call_ollama_json,
    _validate_json_schema_subset,
    _looks_like_truncated_json_text,
    _json_schema_type_matches,
)


class OutputParser:
    """ハーネス 1-1. パーサー (フェーズ2)
    
    LLM生成出力の構造化パース、サニタイズ、バリデーションを統合的に実行するファサード。
    """

    def sanitize(
        self,
        text: str,
        *,
        bot_user_id: int | None = None,
        replacement_name: str = "",
        target_name: str = "",
        other_names: list[str] | set[str] | None = None,
    ) -> str:
        """思考タグや制御行を除去し、平文化したセリフを抽出する。"""
        cleaned = extract_first_user_facing_reply(
            sanitize_generated_reply(
                text,
                bot_user_id=bot_user_id,
                replacement_name=replacement_name,
                target_name=target_name,
                other_names=other_names,
            )
        )
        log.info(
            "[prompt-manager] OutputParser.sanitize: raw_len=%d -> cleaned_len=%d (cleaned=%r)",
            len(text or ""),
            len(cleaned),
            cleaned[:50],
        )
        return cleaned

    def salvage(self, raw_text: str) -> str:
        """思考タグが閉じないまま切断されたテキストから完成したセリフを救出する。"""
        salvaged = salvage_reply_from_think_block(raw_text)
        if salvaged:
            log.info(
                "[prompt-manager] OutputParser.salvage: salvaged %d chars from raw_len=%d (text=%r)",
                len(salvaged),
                len(raw_text or ""),
                salvaged[:50],
            )
        else:
            log.debug(
                "[prompt-manager] OutputParser.salvage: no salvageable text from raw_len=%d",
                len(raw_text or ""),
            )
        return salvaged

    def validate_safety(
        self,
        reply: str,
        *,
        user_text: str = "",
        previous_reply: str = "",
        first_person: str = "",
    ) -> tuple[bool, str]:
        """出力の安全性・品質を多角的に検証する。問題がなければ (True, "") を返す。"""
        if not reply or not reply.strip():
            reason = "empty_reply"
        elif looks_like_prompt_leak(reply):
            reason = "prompt_leak"
        elif looks_like_reasoning_leak(reply):
            reason = "reasoning_leak"
        elif looks_like_abnormal_assistant_reply(reply):
            reason = "abnormal_reply"
        elif looks_like_truncated_reply(reply):
            reason = "truncated_reply"
        elif looks_like_unusable_assistant_reply(reply):
            reason = "unusable_reply"
        elif looks_like_non_japanese_reply(reply):
            reason = "non_japanese_reply"
        elif previous_reply and looks_like_similar_to_previous_reply(reply, previous_reply):
            reason = "similar_to_previous"
        elif previous_reply and looks_like_self_contradicting_reply(reply, last_assistant_text=previous_reply, user_text=user_text):
            reason = "self_contradiction"
        elif user_text and looks_like_parrot_reply(user_text, reply):
            reason = "parrot_reply"
        elif looks_like_abnormal_rejection(reply):
            reason = "abnormal_rejection"
        elif looks_like_character_deviation(reply, user_text=user_text):
            reason = "character_deviation"
        else:
            reason = ""

        if reason:
            log.warning(
                "[prompt-manager] OutputParser.validate_safety rejected: reason=%s (reply=%r)",
                reason,
                reply[:60],
            )
            return False, reason

        log.info(
            "[prompt-manager] OutputParser.validate_safety passed: length=%d (reply=%r)",
            len(reply),
            reply[:60],
        )
        return True, ""

    @staticmethod
    def parse_json(raw_text: str, schema: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """LLM出力からJSONを抽出し、指定スキーマに合致するか検証する。"""
        log.info(
            "[prompt-manager] OutputParser.parse_json starting: raw_len=%d, has_schema=%s",
            len(raw_text or ""),
            bool(schema),
        )
        import json
        import re

        text = raw_text.strip()
        # Markdownコードブロック除去
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        # 最も外側の波括弧を抽出
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            candidate = brace_match.group(0)
        else:
            candidate = text

        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                log.warning("[prompt-manager] OutputParser.parse_json failed: parsed object is not a dict")
                return {}, False

            if schema:
                _validate_json_schema_subset(parsed, schema, candidate)

            log.info(
                "[prompt-manager] OutputParser.parse_json succeeded: keys=%s",
                list(parsed.keys()),
            )
            return parsed, True
        except Exception as e:
            log.warning("[prompt-manager] OutputParser.parse_json failed with json error: %r", e)
            return {}, False

    @staticmethod
    def parse_tool_calls(raw_text: str, registry: Any = None) -> list[ParsedToolCall]:
        """LLM出力からツール呼び出し指示（Action）を抽出し、MCPか否かを判定して返す。"""
        return parse_tool_calls(raw_text, registry=registry)

    @staticmethod
    def has_tool_call(raw_text: str) -> bool:
        """LLM出力にツール呼び出し計画が含まれているか判定する。"""
        return bool(parse_tool_calls(raw_text))

    @staticmethod
    def extract_thought_and_action(
        raw_text: str,
        registry: Any = None,
    ) -> tuple[str, list[ParsedToolCall], str]:
        """出力から思考（thought）、ツール呼び出しリスト（actions）、残りのセリフを分離する。"""
        return extract_thought_and_action(raw_text, registry=registry)

    @staticmethod
    def is_mcp_tool(tool_name: str, registry: Any = None) -> bool:
        """指定されたツールがMCP経由のものであるかを判定する。"""
        return is_mcp_tool(tool_name, registry=registry)

    @staticmethod
    async def execute_action(
        call: ParsedToolCall | Any,
        registry: Any = None,
        mcp_client: Any = None,
    ) -> Any:
        """指示されたツール（MCPツールまたはローカルツール）を実行する。"""
        return await execute_action(call, registry=registry, mcp_client=mcp_client)


__all__ = [
    "OutputParser",
    # actions & MCP
    "ParsedToolCall",
    "parse_tool_calls",
    "extract_thought_and_action",
    "is_mcp_tool",
    "execute_action",
    # structured
    "call_ollama_json",
    "_validate_json_schema_subset",
    "_looks_like_truncated_json_text",
    "_json_schema_type_matches",
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
    "strip_bot_self_mentions",
    "sanitize_partner_misaddressing",
    "_normalize_unintended_watashi",
    "strip_unprompted_nerd_emoji",
    "sanitize_false_style_accusation",
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
    "_strip_think_blocks",
    "_looks_like_duplicate_think_echo",
    "_looks_like_pre_think_leak",
    "salvage_reply_from_think_block",
    # sanitizer
    "sanitize_generated_reply",
    "extract_first_user_facing_reply",
    "sanitize_unknown_denial_for_known_subject",
    "looks_like_placeholder_thought",
    "looks_like_copied_log_appraisal",
    # validation
    "_is_compliance_or_acceptance_clause",
    "_is_validation_ending_clause",
    "_looks_like_opening_topic_echo",
    "looks_like_parrot_reply",
    "looks_like_truncated_reply",
    "looks_like_non_japanese_reply",
    "_looks_too_similar_to_previous_reply",
    "looks_like_similar_to_previous_reply",
    "looks_like_abnormal_assistant_reply",
    "looks_like_unusable_assistant_reply",
    "looks_like_time_of_day_contradiction",
    "looks_like_character_deviation",
    "looks_like_unknown_or_incomprehension_reply",
    "looks_like_unknown_word_incomprehension",
    "extract_unknown_incomprehension_word",
    "looks_like_false_style_accusation",
    "should_skip_unknown_reply",
]
