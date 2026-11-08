"""ファクトチェック MCP パッケージ (harness.agent.tool_registry.mcp.fact_check)

Web検索、URLメタデータ展開、X/Twitter投稿解決、言説の真偽検証を行う
Model Context Protocol (MCP) サーバーおよびクライアント機能を提供する。
"""
from __future__ import annotations

from .client import FactCheckMCPClient
from .server import FactCheckMCPServer, app as fact_check_mcp_app
from .service import (
    FactCheckService,
    _INLINE_CONTEXT_MIN_CHARS,
    _SOCIAL_MEDIA_HOSTS,
    _SOCIAL_REDIRECT_HOSTS,
    _TWITTER_HOSTS,
    _build_url_summary_candidates,
    _clean_text,
    _collect_external_urls_from_payload,
    _extract_meta_content,
    _extract_title,
    _extract_twitter_status_id,
    _fallback_http_search,
    _fetch_link_summary,
    _fetch_twitter_status_context,
    _is_external_article_url,
    _is_resolvable_payload_url,
    _normalize_reply_action_text,
    _strip_urls_for_context,
    build_factcheck_target_context,
    build_link_context_block,
    build_link_summaries_from_text,
    build_search_query_source_text,
    classify_reply_investigate_mode,
    detect_reply_action,
    extract_embed_text_from_message,
    extract_target_text_from_message,
    has_enough_inline_context,
    looks_like_url_only_text,
    search_web,
    search_web_entries,
    should_build_link_summaries,
    _URL_RE,
    extract_urls,
)

from .types import (
    ACTION_KEYWORDS,
    FactCheckResult,
    FactCheckTargetContext,
    ReplyActionType,
    ReplyInvestigateMode,
)

__all__ = [
    # Server & Client
    "FactCheckMCPServer",
    "FactCheckMCPClient",
    "fact_check_mcp_app",
    "FactCheckService",
    # Types & Constants
    "FactCheckTargetContext",
    "FactCheckResult",
    "ReplyActionType",
    "ReplyInvestigateMode",
    "ACTION_KEYWORDS",
    # Functions
    "detect_reply_action",
    "classify_reply_investigate_mode",
    "build_factcheck_target_context",
    "build_link_summaries_from_text",
    "build_search_query_source_text",
    "build_link_context_block",
    "extract_embed_text_from_message",
    "extract_target_text_from_message",
    "search_web",
    "search_web_entries",
    "extract_urls",
    "_URL_RE",
    "looks_like_url_only_text",
    "has_enough_inline_context",
    "should_build_link_summaries",
    "_fallback_http_search",
    "_extract_twitter_status_id",
    "_fetch_twitter_status_context",
    "_fetch_link_summary",
    "_clean_text",
    "_extract_meta_content",
    "_extract_title",
    "_is_external_article_url",
    "_is_resolvable_payload_url",
    "_normalize_reply_action_text",
    "_strip_urls_for_context",
    "_INLINE_CONTEXT_MIN_CHARS",
    "_SOCIAL_MEDIA_HOSTS",
    "_SOCIAL_REDIRECT_HOSTS",
    "_TWITTER_HOSTS",
    "_build_url_summary_candidates",
    "_collect_external_urls_from_payload",
]

