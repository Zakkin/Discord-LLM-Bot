"""img.2chan.net helper facade for Discord bot integrations.

このファイルは、img.2chan.net のスレッドURL検出、スレ本文取得・HTML解析、
投稿文の整形、引用つき返信の組み立て、Chrome MCP/HTTPによる投稿、勢い順
カタログ取得を外部へ提供する互換入口です。

実装本体は `img2channet_parts/` に機能別で分割しています。既存コードはこの
`img2channet.py` を importlib で直接読み込むため、分割後も同じ属性名で使える
よう、部品ファイルをこのモジュールの名前空間に読み込んでいます。
"""
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import aiohttp

if TYPE_CHECKING:
    IMG_THREAD_POST_COMMENT_SCHEMA: dict[str, Any]
    HUTABA_POST_EMOTION_SCHEMA: dict[str, Any]
    def is_img_top_request(text: str) -> bool: ...
    def is_img_thread_url(url: str) -> bool: ...
    async def fetch_img_top5_threads(limit: int = 5, timeout_sec: float = 15.0, *, server: str = "img") -> list[dict[str, Any]]: ...
    async def fetch_img_thread(url: str, max_replies: int = 30, timeout_sec: float = 20.0, cached_caption: str | None = None) -> dict[str, Any]: ...
    def clear_thread_image_caption_cache() -> None: ...
    def parse_hutaba_thread_expiration(html_text: str, *, now: Any = None, min_remaining_sec: float = 600.0) -> dict[str, Any]: ...
    def parse_img_thread_html(url: str, html_text: str, max_replies: int = 30) -> dict[str, Any]: ...
    def format_img_top5_summary(threads: list[dict[str, Any]]) -> str: ...
    def format_img_thread_summary(thread_data: dict[str, Any]) -> str: ...
    async def caption_hutaba_thread_image(image_url: str, *, vision_model: str | None = None, timeout_sec: float | None = None) -> str: ...
    async def submit_img_thread_reply(url: str, comment: str = "", *, text: str = "", email: str = "", password: str = "", timeout_sec: float = 20.0, preserve_urls: object = None, image_path: str | Path | None = None, image_bytes: bytes | None = None, image_filename: str | None = None) -> dict[str, Any]: ...
    def compose_img_thread_reply_comment(thread: dict[str, Any], comment: object, *, quote_post_numbers: object = None, quote_reply_numbers: object = None, max_chars: int = 240, max_quotes: int = 2, max_quote_depth: int = 2) -> dict[str, Any]: ...
    def sanitize_img_post_text(text: object, *, max_chars: int = 120, allow_urls: bool = False, max_quote_depth: int = 2) -> str: ...
    def sanitize_img_post_submission_text(text: object, *, max_chars: int = 340, preserve_urls: object = None, max_quote_depth: int = 2) -> str: ...
    def build_hutaba_post_emotion_system_prompt(persona: str = "") -> str: ...
    def build_hutaba_post_emotion_prompt(thread: dict[str, Any] | None, comment: str, *, target_reply: dict[str, Any] | None = None, url: str = "", board: str = "", character_guidance: str = "", current_emotion_text: str = "") -> str: ...

def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "AGENTS.md").is_file() or (p / ".git").is_dir():
            return p
    return start.parents[5]


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PARTS_DIR = Path(__file__).resolve().with_name("img2channet_parts")
_PART_FILES = (
    "00_constants.py",
    "10_thread_fetch.py",
    "20_post_text.py",
    "30_submission.py",
    "40_catalog.py",
)


def _load_img2channet_part(filename: str) -> None:
    part_path = _PARTS_DIR / filename
    source = part_path.read_text(encoding="utf-8")
    exec(compile(source, str(part_path), "exec"), globals(), globals())


for _part_file in _PART_FILES:
    _load_img2channet_part(_part_file)

del _part_file

_orig_fetch_top5 = globals().get("fetch_img_top5_threads")


if not TYPE_CHECKING:
    async def fetch_img_top5_threads(
        limit: int = 5,
        timeout_sec: float = 15.0,
        *,
        server: str = "img",
    ) -> list[dict[str, Any]]:
        fn = _orig_fetch_top5
        if callable(fn):
            try:
                res = await fn(limit=limit, timeout_sec=timeout_sec, server=server)
            except TypeError:
                res = await fn(limit=limit, timeout_sec=timeout_sec)
            return list(res) if isinstance(res, list) else []
        return []


__all__ = [
    "IMG_THREAD_POST_COMMENT_SCHEMA",
    "HUTABA_POST_EMOTION_SCHEMA",
    "build_hutaba_post_emotion_prompt",
    "build_hutaba_post_emotion_system_prompt",
    "caption_hutaba_thread_image",
    "clear_thread_image_caption_cache",
    "compose_img_thread_reply_comment",
    "extract_img_thread_urls",
    "fetch_img_thread",
    "fetch_img_top5_threads",
    "format_img_thread_summary",
    "format_img_top5_summary",
    "is_img_thread_url",
    "is_img_top_request",
    "parse_hutaba_thread_expiration",
    "parse_img_thread_html",
    "sanitize_img_post_submission_text",
    "sanitize_img_post_text",
    "submit_img_thread_reply",
]
