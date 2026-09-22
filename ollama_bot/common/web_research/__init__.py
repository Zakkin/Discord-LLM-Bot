"""
web_research/__init__.py

ウェブリサーチ機能の公開APIエンドポイントです。
5つのモジュール (config, helpers, parsing, client, dispatch) で構成される
ブラウザ/MCP/X.com/IMGスレッドなどの調査機能を統合して外部へ提供します。

外部モジュールは `from common.web_research import ...` として
このファイルから必要な関数・クラスをインポートしてください。
"""

from __future__ import annotations

import logging

from ..fact_check import (
    build_link_summaries_from_text,
    extract_urls,
    looks_like_url_only_text,
)
from .config import ResearchDispatchMode
from .helpers import (
    _clear_known_term_cache,
    _load_chrome_mcp_img_helpers,
    _load_chrome_mcp_xcom_helpers,
    _remember_known_term,
    _init_helpers as _init_helpers_mod,
    detect_deepdive_followup,
    detect_reply_research_rules,
    extract_explicit_research_term,
    extract_named_term_candidates,
    resolve_research_mode,
)
from .topic_investigation import (
    build_unknown_topic_fallback_reply,
    check_memory_for_topic,
    detect_topic_opinion_inquiry,
)
from .img_thread import (
    build_img_thread_direct_reply,
    clean_img_thread_fragment,
)
from .client import (
    MCPChromeHTTPClient,
    MCPInvalidSessionError,
    MCPTransportAlreadyConnectedError,
    _build_navigate_args,
    _clear_saved_mcp_session_id,
    _get_shared_mcp_client,
    _read_saved_mcp_session_id,
    _reset_shared_mcp_client,
    _write_saved_mcp_session_id,
)
from .parsing import (
    _build_get_web_content_args,
)
from .dispatch import (
    _img_helper,
    _is_login_wall_or_error_page,
    _read_url_with_browser,
    _read_x_timeline_with_browser,
    _search_web_entries_with_mcp,
    _xcom,
    _init_helpers as _init_dispatch_mod,
    get_ollama_web_search_tools,
    research_dispatch,
)

log = logging.getLogger("ollama_bot.common.web_research")

# ---------------------------------------------------------------------------
# Late initialization / Dependency Injection
# ---------------------------------------------------------------------------
# web_researchモジュールは x_helpers や img2channet などの特定ドメインヘルパーに
# 依存しますが、これらもまた一部の共通設定に依存するため、インポート時の循環を
# 防ぐ目的で遅延ロード・依存注入を行います。

_xcom_helper = _load_chrome_mcp_xcom_helpers()
_img_helper_module = _load_chrome_mcp_img_helpers()

_init_helpers_mod(_xcom_helper, _img_helper_module)
_init_dispatch_mod(_xcom_helper, _img_helper_module)

_xcom = _xcom_helper
_img_helper = _img_helper_module

# ---------------------------------------------------------------------------
# Public API Exports
# ---------------------------------------------------------------------------

__all__ = [
    "ResearchDispatchMode",
    "MCPChromeHTTPClient",
    "MCPInvalidSessionError",
    "MCPTransportAlreadyConnectedError",
    "research_dispatch",
    "detect_reply_research_rules",
    "detect_deepdive_followup",
    "detect_topic_opinion_inquiry",
    "check_memory_for_topic",
    "build_unknown_topic_fallback_reply",
    "build_img_thread_direct_reply",
    "clean_img_thread_fragment",
    "get_ollama_web_search_tools",
    "extract_explicit_research_term",
    "extract_named_term_candidates",
    "resolve_research_mode",
    "_clear_known_term_cache",
    "_get_shared_mcp_client",
    "_reset_shared_mcp_client",
    # Internal exports for tests
    "_build_get_web_content_args",
    "_build_navigate_args",
    "_clear_saved_mcp_session_id",
    "_img_helper",
    "_is_login_wall_or_error_page",
    "_load_chrome_mcp_xcom_helpers",
    "_read_saved_mcp_session_id",
    "_read_url_with_browser",
    "_read_x_timeline_with_browser",
    "_remember_known_term",
    "_search_web_entries_with_mcp",
    "_write_saved_mcp_session_id",
    "_xcom",
    # fact_check re-exports
    "build_link_summaries_from_text",
    "extract_urls",
    "looks_like_url_only_text",
]
