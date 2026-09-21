"""
img.2chan.net (ふたば☆ちゃんねる) 関連の共通ユーティリティ。
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "_IMG_THREAD_URL_PATTERN",
    "normalize_img_thread_url",
    "default_browser_headers",
]

_IMG_THREAD_URL_PATTERN = re.compile(
    r"(?:https?://)?img\.2chan\.net/b/res/(\d+)\.htm", re.IGNORECASE
)


def normalize_img_thread_url(url: str | None) -> str:
    """スレッドURLから正規化された https://img.2chan.net/b/res/{thread_no}.htm を返す。"""
    match = _IMG_THREAD_URL_PATTERN.search(str(url or "").strip())
    if not match:
        return ""
    return f"https://img.2chan.net/b/res/{match.group(1)}.htm"


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
