"""AIエージェント・ハーネス Chrome MCP 連携パッケージ (chrome)"""
from __future__ import annotations

from . import img2chan as img2chan
from . import img2channet as img2channet
from . import xcom as xcom
from .img2chan import (
    fetch_img_thread,
    fetch_img_top5_threads,
    format_img_thread_summary,
    format_img_top5_summary,
    is_img_thread_url,
    is_img_top_request,
    submit_img_thread_reply,
)
from .xcom import (
    DEFAULT_DYNAMIC_BROWSER_HOSTS,
    DEFAULT_X_TIMELINE_LIMIT,
    X_TIMELINE_HOME_URL,
    X_TIMELINE_TWEET_SELECTOR,
    extract_x_timeline_limit,
    format_x_timeline_summary,
    is_dynamic_browser_site,
    is_login_wall_or_error_page,
    is_x_timeline_request,
    parse_x_timeline_entries,
)
from typing import Any

chrome_mcp_app: Any = None
try:
    from .server import app as _server_app
    chrome_mcp_app = _server_app
except Exception:
    pass

__all__ = [
    # Modules
    "img2chan",
    "img2channet",
    "xcom",
    # Server
    "chrome_mcp_app",
    # X (Twitter)
    "DEFAULT_DYNAMIC_BROWSER_HOSTS",
    "DEFAULT_X_TIMELINE_LIMIT",
    "X_TIMELINE_HOME_URL",
    "X_TIMELINE_TWEET_SELECTOR",
    "extract_x_timeline_limit",
    "format_x_timeline_summary",
    "is_dynamic_browser_site",
    "is_login_wall_or_error_page",
    "is_x_timeline_request",
    "parse_x_timeline_entries",
    # IMG / 2ch
    "is_img_top_request",
    "is_img_thread_url",
    "fetch_img_top5_threads",
    "fetch_img_thread",
    "format_img_top5_summary",
    "format_img_thread_summary",
    "submit_img_thread_reply",
]
