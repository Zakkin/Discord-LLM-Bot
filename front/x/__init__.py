"""front.x: x.com（旧Twitter）操作サブプロジェクト。

- `front.x.parser`:         URL生成・アクセシビリティスナップショット解析・ログイン壁判定・要求判定
- `front.x.loader`:         ドメインヘルパーローダーとフォールバック（`_load_chrome_mcp_xcom_helpers`）
- `front.x.timeline`:       Chrome MCP を用いたタイムライン読み取り（`read_x_timeline_with_browser`）
- `front.x.tools`:          Xタイムライン・X検索の調査ディスパッチ（`x_timeline_dispatch`, `x_search_dispatch`）
- `front.x.proactive_feed`: 自発発話向けタイムライン取得・ローテーション・プロンプト整形
- `front.x.adapter`:        `XAdapter`（`PlatformAdapter` 実装）
"""
from __future__ import annotations

from front.x import parser as parser
from front.x.adapter import XAdapter
from front.x.parser import (
    DEFAULT_DYNAMIC_BROWSER_HOSTS,
    DEFAULT_X_TIMELINE_LIMIT,
    X_TIMELINE_HOME_URL,
    X_TIMELINE_TWEET_SELECTOR,
    build_x_search_url,
    extract_x_timeline_limit,
    format_x_search_summary,
    format_x_timeline_summary,
    hostname_from_url,
    is_dynamic_browser_site,
    is_login_wall_or_error_page,
    is_x_search_request,
    is_x_timeline_request,
    parse_x_timeline_entries,
)

__all__ = [
    "DEFAULT_DYNAMIC_BROWSER_HOSTS",
    "DEFAULT_X_TIMELINE_LIMIT",
    "XAdapter",
    "X_TIMELINE_HOME_URL",
    "X_TIMELINE_TWEET_SELECTOR",
    "build_x_search_url",
    "extract_x_timeline_limit",
    "format_x_search_summary",
    "format_x_timeline_summary",
    "hostname_from_url",
    "is_dynamic_browser_site",
    "is_login_wall_or_error_page",
    "is_x_search_request",
    "is_x_timeline_request",
    "parse_x_timeline_entries",
    "parser",
]
