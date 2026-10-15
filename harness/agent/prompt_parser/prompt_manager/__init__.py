from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional
from collections import deque

import discord

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.discord_helpers import (
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
from ollama_bot.common.emotion_helpers import format_emotion_tags_ja
from harness.agent.memory_manager import logic as memory_logic
from . import prospective_memory
from . import user_profile_logic
from .working_memory import (
    build_working_context,
    format_working_context_for_prompt,
    needs_specific_recall,
)
from harness.agent.memory_manager import MemoryStore
from ollama_bot.common import ollama_helpers
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState, UserRelationship
from ollama_bot.common.bot_identity import BotIdentity
from .analysis_prompts import (
    INTENT_ANALYZER_SYSTEM_PROMPT,
    MEMORY_RELEVANCE_SELECTOR_SYSTEM_PROMPT,
    PRE_REPLY_ANALYZER_SYSTEM_PROMPT,
    CONTEXT_CHECKER_SYSTEM_PROMPT,
    REPLY_DELIBERATION_SYSTEM_PROMPT,
    REACTION_CLASSIFIER_SYSTEM_PROMPT,
    THINK_CLASSIFIER_SYSTEM_PROMPT,
    EMOTION_SCORER_SYSTEM_PROMPT,
    EMOTION_REASONER_SYSTEM_PROMPT,
    MCP_ROUTER_SYSTEM_PROMPT,
    MCP_ROUTING_DECISION_SCHEMA,
    build_current_datetime_context,
    build_pre_reply_analyzer_system_prompt,
    build_emotion_scorer_system_prompt,
    build_mcp_routing_prompt,
    is_standalone_reply_question,
)
from .system_prompts import (
    FACT_CHECK_SYSTEM_PROMPT,
    FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT,
    REPLY_INVESTIGATE_SYSTEM_PROMPT,
    REPLY_RESEARCH_DECIDER_SYSTEM_PROMPT,
    USER_REPLY_BASE_PARTS,
    append_chat_reply_output_suffix,
    build_break_prompt_parts,
    build_chat_reply_system_prompt,
    build_fact_check_system_prompt,
    build_media_analysis_text,
    build_media_prompt_parts,
    build_memory_selector_tail_parts,
    build_reply_deliberation_prompt,
    build_playful_say_request_instruction,
    build_reply_investigate_system_prompt,
    build_reply_research_decider_prompt,
    build_reply_research_decider_system_prompt,
    build_research_grounded_reply_prompt,
    build_topic_carryover_guard_parts,
    build_topic_system_prompt,
    build_user_prompt_extra_rule_parts,
    build_user_prompt_target_is_ai_parts,
    build_weather_comment_prompt,
)
from .relationship_prompts import (
    build_relationship_thought_prompt,
    format_interaction_logs_for_prompt,
)
from .retry_prompts import (
    CHAT_REPLY_OUTPUT_SUFFIX,
    _slim_base_prompt_for_retry,
    build_character_deviation_retry_prompt,
    build_final_reply_extractor_prompt,
    build_output_only_retry_prompt,
    build_parrot_retry_prompt,
    build_role_flip_retry_prompt,
    build_similar_retry_prompt,
    build_time_contradiction_retry_prompt,
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
    is_assistant_echo_memory_line,
    is_chat_log_or_system_line,
    sanitize_memory_candidates,
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
    classify_mcp_routing,
    detect_third_party_hearsay,
    extract_playful_say_request,
    looks_like_ai_utterance_confirmation,
    looks_like_user_deduction_or_confirmation,
    looks_like_user_playful_request_or_command,
)
from .builder import (
    _build_user_prompt,
    build_user_prompt,
    build_break_reply_prompt,
    build_memory_break_prompt,
    build_user_interest_break_prompt,
    _detect_recent_bot_utterance_tsukkomi,
)
from .research_prompts import (
    build_x_timeline_break_prompt,
    build_img_break_prompt,
    build_research_context_text,
    build_research_reply_base_prompt,
    build_research_break_base_prompt_text,
)
from .thread_tracker import ConversationThread, ThreadTracker
from .tool_context import ToolContextManager

log = logging.getLogger("harness.agent.prompt_manager")


class PromptManager:
    """ハーネス 1-1. プロンプトマネージャー (フェーズ1)
    
    システムプロンプトの動的生成、コンテキスト予算管理、文脈・意図分析、スレッド追跡を司る。
    """

    def __init__(
        self,
        thread_tracker: Optional[ThreadTracker] = None,
        tool_registry: Any = None,
    ) -> None:
        self.thread_tracker = thread_tracker or ThreadTracker()
        self.tool_registry = ToolContextManager.get_tool_registry(tool_registry)

    async def sync_mcp_tools(
        self,
        mcp_client: Any = None,
        category: Any = None,
    ) -> list[Any]:
        """外部MCPサーバーに接続し、利用可能なツールカタログを動的に取得・レジストリへ同期する。"""
        log.info("[prompt-manager] PromptManager.sync_mcp_tools requested")
        imported = await ToolContextManager.sync_mcp_tools(
            registry=self.tool_registry,
            mcp_client=mcp_client,
            category=category,
        )
        return imported

    def render_tool_catalog(
        self,
        format_name: str = "markdown",
        include_guidelines: bool = True,
    ) -> str:
        """現在利用可能なツールのカタログ文字列を生成する。"""
        return ToolContextManager.render_tool_catalog(
            self.tool_registry,
            format_name=format_name,
            include_guidelines=include_guidelines,
        )

    async def build_prompt(
        self,
        message: discord.Message,
        cache: dict[int, deque[dict[str, Any]]],
        memory_store: MemoryStore,
        *,
        bot_user_id: Optional[int],
        emotion_state: EmotionState | None = None,
        core_beliefs: Optional[list[str]] = None,
        effective_user_text: str = "",
        media_prompt_parts: Optional[list[str]] = None,
        habit_profile_lines: Optional[list[str]] = None,
        fetch_recent_context_lines: Callable[[discord.Message], Awaitable[list[str]]],
        find_last_assistant_message_text: Callable[..., Awaitable[str]],
        preferred_emotion_tags: Optional[list[str]] = None,
        user_relationships: Optional[dict[int, Any]] = None,
        bot_identity: Optional[BotIdentity] = None,
        tool_registry: Any = None,
    ) -> tuple[str, list[int], list[str], str, dict[str, Any], dict[str, Any], dict[str, Any]]:
        """ユーザー・チャンネル状況に応じた動的プロンプトを生成する。"""
        mid = getattr(message, "id", None)
        author = getattr(message, "author", None)
        author_name = getattr(author, "display_name", str(author)) if author else "unknown"
        effective_registry = tool_registry if tool_registry is not None else self.tool_registry
        log.info(
            "[prompt-manager] PromptManager.build_prompt starting: message_id=%s, author=%s, effective_text=%r, has_tool_registry=%s",
            mid,
            author_name,
            effective_user_text[:80] if effective_user_text else "",
            bool(effective_registry),
        )
        result = await _build_user_prompt(
            message,
            cache,
            memory_store,
            bot_user_id=bot_user_id,
            emotion_state=emotion_state,
            core_beliefs=core_beliefs,
            effective_user_text=effective_user_text,
            media_prompt_parts=media_prompt_parts,
            habit_profile_lines=habit_profile_lines,
            fetch_recent_context_lines=fetch_recent_context_lines,
            find_last_assistant_message_text=find_last_assistant_message_text,
            preferred_emotion_tags=preferred_emotion_tags,
            user_relationships=user_relationships,
            bot_identity=bot_identity,
            thread_tracker=self.thread_tracker,
            tool_registry=effective_registry,
        )
        user_prompt, selected_ids, memory_lines, topic_flow, intent_info, deliberation_info, persona_info = result
        log.info(
            "[prompt-manager] PromptManager.build_prompt completed: message_id=%s, prompt_len=%d, selected_memories=%d, flow=%s, intent=%s",
            mid,
            len(user_prompt),
            len(selected_ids),
            topic_flow,
            intent_info.get("intent", "unknown"),
        )
        return result

    async def analyze_pre_reply_context(
        self,
        *,
        current_user_text: str,
        last_assistant_text: str,
        memory_lines: list[str],
        channel_summary: str | None,
        preferred_emotion_tags: list[str] | None = None,
        is_reply_to_other_user: bool = False,
        is_reply_to_bot: bool = False,
        reply_context_hint: str | None = None,
        recent_bot_utterances: list[str] | None = None,
        bot_identity: BotIdentity | None = None,
        mentioned_users: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[str], str | None, dict[str, Any]]:
        """返信前の意図・文脈・メモリ選定・自己整合性を分析する。"""
        log.info(
            "[prompt-manager] PromptManager.analyze_pre_reply_context starting: user_text=%r, memory_candidates=%d",
            current_user_text[:80],
            len(memory_lines),
        )
        res = await _analyze_pre_reply_context(
            current_user_text=current_user_text,
            last_assistant_text=last_assistant_text,
            memory_lines=memory_lines,
            channel_summary=channel_summary,
            preferred_emotion_tags=preferred_emotion_tags,
            is_reply_to_other_user=is_reply_to_other_user,
            is_reply_to_bot=is_reply_to_bot,
            reply_context_hint=reply_context_hint,
            recent_bot_utterances=recent_bot_utterances,
            bot_identity=bot_identity,
            mentioned_users=mentioned_users,
        )
        intent_info, selected_lines, chan_summary, deliberation_info = res
        log.info(
            "[prompt-manager] PromptManager.analyze_pre_reply_context completed: intent=%s, target_is_ai=%s, flow=%s, selected_memories=%d",
            intent_info.get("intent"),
            intent_info.get("target_is_ai"),
            intent_info.get("flow"),
            len(selected_lines),
        )
        return res

    def build_system_prompt(
        self_or_base: Any = "",
        base_system_prompt: str = "",
        tool_registry: Any = None,
        format_name: str = "markdown",
        include_tools: bool = True,
    ) -> str:
        """メインチャット返信用の動的システムプロンプトを生成する。
        
        tool_registry または self.tool_registry が指定されている場合、
        利用可能なツールカタログ（スキーマ・引数）および推論・計画立案プロトコル（Thought & Planning）を
        システムプロンプトに動的に結合する。
        """
        if isinstance(self_or_base, str):
            actual_base = self_or_base
            reg = tool_registry
        else:
            actual_base = base_system_prompt or ""
            reg = tool_registry if tool_registry is not None else getattr(self_or_base, "tool_registry", None)

        log.info("[prompt-manager] PromptManager.build_system_prompt requested: base_len=%d", len(actual_base or ""))
        base_prompt = build_chat_reply_system_prompt(actual_base)
        if not include_tools:
            return base_prompt

        if reg is not None:
            return ToolContextManager.bind_to_system_prompt(
                base_prompt,
                reg,
                format_name=format_name,
            )
        return base_prompt

    @staticmethod
    def build_fact_check_system_prompt() -> str:
        """ファクトチェック用システムプロンプトを生成する。"""
        log.info("[prompt-manager] PromptManager.build_fact_check_system_prompt requested")
        return build_fact_check_system_prompt()

    @staticmethod
    def build_reply_investigate_system_prompt() -> str:
        """外部調査・解説用システムプロンプトを生成する。"""
        log.info("[prompt-manager] PromptManager.build_reply_investigate_system_prompt requested")
        return build_reply_investigate_system_prompt()

    @staticmethod
    def build_reply_research_decider_system_prompt() -> str:
        """外部調査要否判定用システムプロンプトを生成する。"""
        log.info("[prompt-manager] PromptManager.build_reply_research_decider_system_prompt requested")
        return build_reply_research_decider_system_prompt()

    @staticmethod
    def build_topic_system_prompt() -> str:
        """自発話題振り用システムプロンプトを生成する。"""
        log.info("[prompt-manager] PromptManager.build_topic_system_prompt requested")
        return build_topic_system_prompt()

    @staticmethod
    def build_datetime_context() -> str:
        """サーバ時刻（JST）と曜日の現在日時コンテキストを生成する。"""
        return build_current_datetime_context()

    @staticmethod
    async def classify_mcp_routing(
        user_text: str,
        context_lines: list[str] | None = None,
        rule_hints: dict[str, Any] | None = None,
        model: str | None = None,
        timeout_sec: float = 4.0,
    ) -> dict[str, Any]:
        """小型分類モデル（middle-llm）を用いてMCPツール利用要否・種別を判定する。"""
        return await classify_mcp_routing(
            user_text=user_text,
            context_lines=context_lines,
            rule_hints=rule_hints,
            model=model,
            timeout_sec=timeout_sec,
        )

    @staticmethod
    def build_break_reply_prompt(
        *,
        source: str = "memory",
        user_text_for_prompt: str,
        target_name: str = "ユーザー",
        bot_primary_name: str = "AI",
        bot_user_id: int | None = None,
        source_block: str = "",
        reply_style_guard: str = "",
        extra_reply_rule: str = "",
        channel_summary: str | None = None,
        memory_lines: list[str] | None = None,
        last_assistant_text: str = "",
        target_is_ai: bool = False,
        should_clarify: bool = False,
        mentioned_parts: list[str] | None = None,
        habit_profile_lines: list[str] | None = None,
        core_beliefs: list[str] | None = None,
        target_aliases: list[str] | None = None,
        target_uid: int | None = None,
        media_prompt_parts: list[str] | None = None,
    ) -> str:
        """話題転換（BREAK）プロンプトをソース種別（memory, x, img, user_interest）に応じて統一構築する。"""
        return build_break_reply_prompt(
            source=source,
            user_text_for_prompt=user_text_for_prompt,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            source_block=source_block,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            channel_summary=channel_summary,
            memory_lines=memory_lines,
            last_assistant_text=last_assistant_text,
            target_is_ai=target_is_ai,
            should_clarify=should_clarify,
            mentioned_parts=mentioned_parts,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )

    @staticmethod
    def build_memory_break_prompt(**kwargs: Any) -> str:
        """記憶（長期記憶・直前文脈）を材料にした話題転換プロンプトを構築する。"""
        return build_memory_break_prompt(**kwargs)

    @staticmethod
    def build_user_interest_break_prompt(**kwargs: Any) -> str:
        """サーバーメンバーの関心事を材料にした話題転換プロンプトを構築する。"""
        return build_user_interest_break_prompt(**kwargs)

    @staticmethod
    def build_x_timeline_break_prompt(**kwargs: Any) -> str:
        """X（旧Twitter）ホームタイムラインを材料にした話題転換プロンプトを構築する。"""
        return build_x_timeline_break_prompt(**kwargs)

    @staticmethod
    def build_img_break_prompt(**kwargs: Any) -> str:
        """img.2chan.netの勢い上位スレッドを材料にした話題転換プロンプトを構築する。"""
        return build_img_break_prompt(**kwargs)


__all__ = [
    "PromptManager",
    # system_prompts
    "FACT_CHECK_SYSTEM_PROMPT",
    "FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT",
    "REPLY_INVESTIGATE_SYSTEM_PROMPT",
    "REPLY_RESEARCH_DECIDER_SYSTEM_PROMPT",
    "USER_REPLY_BASE_PARTS",
    "append_chat_reply_output_suffix",
    "build_break_prompt_parts",
    "build_chat_reply_system_prompt",
    "build_fact_check_system_prompt",
    "build_media_analysis_text",
    "build_media_prompt_parts",
    "build_memory_selector_tail_parts",
    "build_reply_deliberation_prompt",
    "build_reply_investigate_system_prompt",
    "build_reply_research_decider_prompt",
    "build_reply_research_decider_system_prompt",
    "build_research_grounded_reply_prompt",
    "build_topic_carryover_guard_parts",
    "build_topic_system_prompt",
    "build_user_prompt_extra_rule_parts",
    "build_user_prompt_target_is_ai_parts",
    "build_playful_say_request_instruction",
    "build_weather_comment_prompt",
    # analysis_prompts
    "INTENT_ANALYZER_SYSTEM_PROMPT",
    "MEMORY_RELEVANCE_SELECTOR_SYSTEM_PROMPT",
    "PRE_REPLY_ANALYZER_SYSTEM_PROMPT",
    "CONTEXT_CHECKER_SYSTEM_PROMPT",
    "REPLY_DELIBERATION_SYSTEM_PROMPT",
    "REACTION_CLASSIFIER_SYSTEM_PROMPT",
    "THINK_CLASSIFIER_SYSTEM_PROMPT",
    "EMOTION_SCORER_SYSTEM_PROMPT",
    "EMOTION_REASONER_SYSTEM_PROMPT",
    "MCP_ROUTER_SYSTEM_PROMPT",
    "MCP_ROUTING_DECISION_SCHEMA",
    "build_current_datetime_context",
    "build_pre_reply_analyzer_system_prompt",
    "build_emotion_scorer_system_prompt",
    "build_mcp_routing_prompt",
    # retry_prompts
    "CHAT_REPLY_OUTPUT_SUFFIX",
    "_slim_base_prompt_for_retry",
    "build_character_deviation_retry_prompt",
    "build_final_reply_extractor_prompt",
    "build_output_only_retry_prompt",
    "build_parrot_retry_prompt",
    "build_role_flip_retry_prompt",
    "build_similar_retry_prompt",
    "build_time_contradiction_retry_prompt",
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
    "is_assistant_echo_memory_line",
    "is_chat_log_or_system_line",
    "sanitize_memory_candidates",
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
    "classify_mcp_routing",
    "looks_like_ai_utterance_confirmation",
    "looks_like_user_deduction_or_confirmation",
    "looks_like_user_playful_request_or_command",
    "extract_playful_say_request",
    # builder
    "_build_user_prompt",
    "build_user_prompt",
    "build_break_reply_prompt",
    "build_memory_break_prompt",
    "build_user_interest_break_prompt",
    # research_prompts
    "build_x_timeline_break_prompt",
    "build_img_break_prompt",
    "build_research_context_text",
    "build_research_reply_base_prompt",
    "build_research_break_base_prompt_text",
    # thread_tracker
    "ConversationThread",
    "ThreadTracker",
    # tool_context & MCP
    "ToolContextManager",
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
    "_detect_recent_bot_utterance_tsukkomi",
    "build_relationship_thought_prompt",
    "format_interaction_logs_for_prompt",
    "is_standalone_reply_question",
]
