from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def path_suffix(url: str) -> str:
    """URLパスの末尾の拡張子を小文字（先頭ドット付き）で取得する。"""
    try:
        path = urlparse(url).path or ""
        return PurePosixPath(path).suffix.lower()
    except Exception:
        return ""


def is_image_url(url: str) -> bool:
    """URLが対応画像形式の拡張子を持つかを判定する。"""
    return path_suffix(url) in IMAGE_EXTENSIONS


def is_image_attachment(attachment: Any) -> bool:
    """Discord Attachment（または類似オブジェクト）が画像であるかを判定する。"""
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = str(getattr(attachment, "filename", "") or "")
    return PurePosixPath(filename).suffix.lower() in IMAGE_EXTENSIONS


def normalize_hostname(url: str) -> str:
    """URLから小文字のホスト名（netloc）を抽出する。"""
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
