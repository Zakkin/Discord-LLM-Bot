"""ハーネス 1-1. プロンプトマネージャー(フェーズ1)＆パーサー(フェーズ2)

AIエージェント・ハーネスの実行基盤（エージェント本体）における
・システムプロンプトの動的生成（PromptManager）
・LLM出力からの構造化データ抽出・サニタイズ・バリデーション（OutputParser）
を集約・提供する。
"""
from __future__ import annotations

try:
    from .prompt_manager import (
        PromptManager,
        ConversationThread,
        ThreadTracker,
        build_agent_persona_parts,
        build_chat_reply_system_prompt,
        build_fact_check_system_prompt,
        build_reply_investigate_system_prompt,
        build_topic_system_prompt,
        build_current_datetime_context,
        ToolContextManager,
        classify_mcp_routing,
        build_mcp_routing_prompt,
        MCP_ROUTER_SYSTEM_PROMPT,
        MCP_ROUTING_DECISION_SCHEMA,
        looks_like_user_playful_request_or_command,
    )
except ImportError:
    PromptManager = None  # type: ignore[assignment, misc]
    ConversationThread = None  # type: ignore[assignment, misc]
    ThreadTracker = None  # type: ignore[assignment, misc]
    ToolContextManager = None  # type: ignore[assignment, misc]
    classify_mcp_routing = None  # type: ignore[assignment]
    build_mcp_routing_prompt = None  # type: ignore[assignment]
    MCP_ROUTER_SYSTEM_PROMPT = ""
    MCP_ROUTING_DECISION_SCHEMA = {}
    looks_like_user_playful_request_or_command = None  # type: ignore[assignment]
    build_agent_persona_parts = None  # type: ignore[assignment]
    build_chat_reply_system_prompt = None  # type: ignore[assignment]
    build_fact_check_system_prompt = None  # type: ignore[assignment]
    build_reply_investigate_system_prompt = None  # type: ignore[assignment]
    build_topic_system_prompt = None  # type: ignore[assignment]
    build_current_datetime_context = None  # type: ignore[assignment]
from .parser import (
    OutputParser,
    ParsedToolCall,
    parse_tool_calls,
    extract_thought_and_action,
    is_mcp_tool,
    execute_action,
    call_ollama_json,
    sanitize_generated_reply,
    extract_first_user_facing_reply,
    salvage_reply_from_think_block,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_parrot_reply,
    looks_like_truncated_reply,
    looks_like_non_japanese_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_unusable_assistant_reply,
    looks_like_character_deviation,
    looks_like_unknown_or_incomprehension_reply,
    should_skip_unknown_reply,
    strip_bot_self_mentions,
    strip_markdown_artifacts,
    strip_trailing_reasoning_notes,
    sanitize_partner_misaddressing,
)

__all__ = [
    # Facades
    "PromptManager",
    "OutputParser",
    # Prompt Manager components
    "ConversationThread",
    "ThreadTracker",
    "build_agent_persona_parts",
    "build_chat_reply_system_prompt",
    "build_fact_check_system_prompt",
    "build_reply_investigate_system_prompt",
    "build_topic_system_prompt",
    "build_current_datetime_context",
    "ToolContextManager",
    "classify_mcp_routing",
    "build_mcp_routing_prompt",
    "MCP_ROUTER_SYSTEM_PROMPT",
    "MCP_ROUTING_DECISION_SCHEMA",
    "looks_like_user_playful_request_or_command",
    # Parser components
    "ParsedToolCall",
    "parse_tool_calls",
    "extract_thought_and_action",
    "is_mcp_tool",
    "execute_action",
    "call_ollama_json",
    "sanitize_generated_reply",
    "extract_first_user_facing_reply",
    "salvage_reply_from_think_block",
    "looks_like_prompt_leak",
    "looks_like_reasoning_leak",
    "looks_like_parrot_reply",
    "looks_like_truncated_reply",
    "looks_like_non_japanese_reply",
    "looks_like_abnormal_assistant_reply",
    "looks_like_unusable_assistant_reply",
    "looks_like_character_deviation",
    "looks_like_unknown_or_incomprehension_reply",
    "should_skip_unknown_reply",
    "strip_bot_self_mentions",
    "strip_markdown_artifacts",
    "strip_trailing_reasoning_notes",
    "sanitize_partner_misaddressing",
]
