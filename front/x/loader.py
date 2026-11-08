"""x.com ドメインヘルパーのローダーとフォールバック定義。

`front.registry.load_domain_helper("x")` 経由で `front.x.parser` を読み込み、
読み込めない環境や任意パス指定時には `_MissingXComHelpers` へ安全にフォールバックします。
"""
from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from front.registry import load_domain_helper

log = logging.getLogger("ollama_bot.common.web_research")

_FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS: tuple[str, ...] = ("x.com", "twitter.com")
_FALLBACK_XCOM_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don’t miss what’s happening",
    "people on x are the first to know",
    "something went wrong",
    "please log in",
    "please sign in",
    "sign in to",
    "log in to",
    "you need to sign in",
    "login required",
    "access denied",
    "403 forbidden",
    "page not found",
    "404 not found",
    "this content isn't available",
    "this tweet is from an account that no longer exists",
    "something went wrong, but don't fret",
    "something went wrong, but don’t fret",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don’t miss what’s happening",
    "people on x are the first to know",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN = 600


class _MissingXComHelpers:
    DEFAULT_DYNAMIC_BROWSER_HOSTS: tuple[str, ...] = _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS
    DEFAULT_X_TIMELINE_LIMIT = 10
    X_TIMELINE_HOME_URL = "https://x.com/home"
    X_TIMELINE_TWEET_SELECTOR = 'div[data-testid="primaryColumn"] article[data-testid="tweet"]'

    @staticmethod
    def is_dynamic_browser_site(
        url: str,
        *,
        dynamic_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> bool:
        hostname = str(urlparse(str(url or "").strip()).hostname or "").lower()
        if not hostname:
            return False
        for raw_host in dynamic_hosts or _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS:
            host = str(raw_host or "").strip().lower()
            if host and (hostname == host or hostname.endswith(f".{host}")):
                return True
        return False

    @staticmethod
    def is_login_wall_or_error_page(content_text: str) -> bool:
        stripped = str(content_text or "").strip()
        if not stripped:
            return True
        lowered = stripped.lower()
        if any(phrase in lowered for phrase in _FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES):
            return True
        if len(stripped) <= _FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN:
            return any(phrase in lowered for phrase in _FALLBACK_XCOM_LOGIN_WALL_PHRASES)
        return False

    @staticmethod
    def is_x_timeline_request(text: str) -> bool:
        lowered = str(text or "").lower().replace("　", "")
        has_x = any(token in lowered for token in ("x", "twitter", "ツイッター"))
        has_timeline = any(token in lowered for token in ("タイムライン", "tl", "timeline", "feed", "フィード"))
        has_action = any(token in lowered for token in ("今", "いま", "現在", "最新", "直近", "流れて", "見て", "取得", "何", "なに", "どう"))
        return bool(has_timeline and has_action and (has_x or "tl" in lowered))

    @staticmethod
    def extract_x_timeline_limit(text: str, *, default: int = 10, max_limit: int = 10) -> int:
        fallback = max(int(default or 10), 1)
        hard_limit = max(int(max_limit or fallback), 1)
        match = re.search(r"([0-9０-９]{1,2})\s*(?:件|個|ツイート|ポスト|tweet|post)", str(text or ""))
        if not match:
            return max(1, min(fallback, hard_limit))
        try:
            value = int(str(match.group(1)).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except ValueError:
            value = fallback
        return max(1, min(value, hard_limit))

    @staticmethod
    def parse_x_timeline_entries(text: str, *, limit: int = 10) -> list[dict[str, str]]:
        return []

    @staticmethod
    def format_x_timeline_summary(entries: list[dict[str, str]], *, limit: int = 10) -> str:
        return "Xホームタイムラインの投稿を抽出できませんでした。ログイン状態や表示状態を確認してください。"


def _load_chrome_mcp_xcom_helpers(helper_path: Path | None = None) -> Any:
    if helper_path is None:
        try:
            return load_domain_helper("x")
        except Exception:
            pass
        log.warning("harness Chrome MCP x.com helper not found; using built-in fallback")
        return _MissingXComHelpers

    helper_path = Path(helper_path)
    if not helper_path.is_file():
        log.warning(
            "chrome-mcp x.com helper not found at %s; using built-in fallback",
            helper_path,
        )
        return _MissingXComHelpers
    spec = importlib.util.spec_from_file_location(
        "discord_ai_bot_chrome_mcp_xcom",
        helper_path,
    )
    if spec is None or spec.loader is None:
        log.warning(
            "unable to load chrome-mcp x.com helper from %s; using built-in fallback",
            helper_path,
        )
        return _MissingXComHelpers
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        log.warning(
            "chrome-mcp x.com helper load failed path=%s err=%r; using built-in fallback",
            helper_path,
            e,
        )
        return _MissingXComHelpers
    missing = [
        name
        for name in (
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
        )
        if not hasattr(module, name)
    ]
    if missing:
        log.warning(
            "chrome-mcp x.com helper missing attributes path=%s missing=%s; using built-in fallback",
            helper_path,
            ",".join(missing),
        )
        return _MissingXComHelpers
    return module


__all__ = [
    "_MissingXComHelpers",
    "_load_chrome_mcp_xcom_helpers",
]
