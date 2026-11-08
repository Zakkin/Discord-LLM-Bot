"""
HTML処理に関する共通ユーティリティ。
"""
from __future__ import annotations

import html
import re

__all__ = ["clean_html"]


def clean_html(text: str | None) -> str:
    """HTMLタグを除去し、<br>タグを改行に変換し、HTML実体参照をアンエスケープする。"""
    cleaned = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return html.unescape(cleaned).strip()
