"""
img.2chan.net (ふたば☆ちゃんねる) 関連の共通ユーティリティ。
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "_HUTABA_THREAD_URL_PATTERN",
    "_IMG_THREAD_URL_PATTERN",
    "_IMG_THREAD_URL_RE",
    "normalize_img_thread_url",
    "normalize_hutaba_board_server",
    "extract_hutaba_board_server",
    "default_browser_headers",
]

_HUTABA_THREAD_URL_PATTERN = re.compile(
    r"(?:https?://)?(?P<server>img|may)\.2chan\.net/b/res/(?P<thread_no>\d+)\.htm", re.IGNORECASE
)
_IMG_THREAD_URL_PATTERN = _HUTABA_THREAD_URL_PATTERN
_IMG_THREAD_URL_RE = _HUTABA_THREAD_URL_PATTERN


def normalize_img_thread_url(url: str | None) -> str:
    """スレッドURLから正規化された https://{img|may}.2chan.net/b/res/{thread_no}.htm を返す。"""
    match = _HUTABA_THREAD_URL_PATTERN.search(str(url or "").strip())
    if not match:
        return ""
    server = match.group("server").lower()
    thread_no = match.group("thread_no")
    return f"https://{server}.2chan.net/b/res/{thread_no}.htm"


def normalize_hutaba_board_server(server: object = "img") -> str:
    """ふたばのサーバー指定（'img' または 'may'）を正規化する。"""
    raw = str(server or "").strip().lower()
    if raw in ("may", "may.2chan.net") or "may.2chan.net" in raw:
        return "may"
    return "img"


def extract_hutaba_board_server(url: object) -> str:
    """スレッドURL等からサーバー名（'img' または 'may'）を抽出する。"""
    raw = str(url or "").strip().lower()
    match = _IMG_THREAD_URL_RE.search(raw)
    if match:
        return str(match.group("server") or "img").lower()
    if "may.2chan.net" in raw or raw == "may":
        return "may"
    return "img"


def default_browser_headers(*, referer: str = "https://img.2chan.net/b/futaba.htm") -> dict[str, str]:
    """ブラウザを模倣した共通HTTPヘッダーを返す。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }
