# ollama_bot/common/message_media.py
import base64
import logging
from typing import Any


import discord

from .config_helpers import cfg
from .discord_helpers import content
from .fact_check import build_link_summaries_from_text, extract_urls
from .http_session import get_shared_http_session
from ..ollama_chat_.ollama_chat_texts import build_media_analysis_text, build_media_prompt_parts

log = logging.getLogger("ollama_bot.common.message_media")

from lib.media_utils import (
    IMAGE_EXTENSIONS as _IMAGE_EXTENSIONS,
    is_image_attachment as _is_image_attachment,
    is_image_url as _is_image_url,
    path_suffix as _path_suffix,
)


def message_has_supported_media_or_links(message: discord.Message) -> bool:
    try:
        attachments = list(getattr(message, "attachments", []) or [])
        if any(_is_image_attachment(att) for att in attachments):
            return True
    except Exception:
        pass
    return bool(extract_urls(content(message)))


async def extract_base64_images(message: discord.Message) -> list[str]:
    """メッセージ内の画像をBase64化して返す。"""
    max_images = max(int(cfg("OLLAMA_MAX_MEDIA_IMAGES", 2) or 2), 0)
    max_image_bytes = max(int(cfg("OLLAMA_MAX_IMAGE_BYTES", 5 * 1024 * 1024) or (5 * 1024 * 1024)), 1024)
    timeout_sec = float(cfg("OLLAMA_MEDIA_FETCH_TIMEOUT_SEC", 5) or 5)

    images: list[str] = []

    attachments = list(getattr(message, "attachments", []) or [])
    for att in attachments:
        if len(images) >= max_images:
            break
        if not _is_image_attachment(att):
            continue
        encoded = await _attachment_to_base64(att, max_bytes=max_image_bytes)
        if encoded:
            images.append(encoded)

    urls = extract_urls(content(message))
    for url in [u for u in urls if _is_image_url(u)]:
        if len(images) >= max_images:
            break
        encoded = await _url_image_to_base64(url, max_bytes=max_image_bytes, timeout_sec=timeout_sec)
        if encoded:
            images.append(encoded)

    return images


async def _attachment_to_base64(attachment: discord.Attachment, *, max_bytes: int) -> str | None:
    try:
        data = await attachment.read(use_cached=True)
    except TypeError:
        data = await attachment.read()
    except Exception as e:
        log.debug("attachment read failed: %s", e)
        return None

    if not data or len(data) > max_bytes:
        return None
    return base64.b64encode(data).decode("ascii")


async def _download_bytes(url: str, *, max_bytes: int, timeout_sec: float) -> bytes | None:
    headers = {"User-Agent": "discord-ai-bot/1.0"}
    try:
        session = await get_shared_http_session(timeout_sec=timeout_sec, headers=headers)
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(65536):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        log.debug("url download failed: %s", e)
        return None


async def _url_image_to_base64(url: str, *, max_bytes: int, timeout_sec: float) -> str | None:
    data = await _download_bytes(url, max_bytes=max_bytes, timeout_sec=timeout_sec)
    if not data:
        return None
    return base64.b64encode(data).decode("ascii")


async def build_message_media_context(message: discord.Message) -> dict[str, Any]:
    max_links = max(int(cfg("OLLAMA_MAX_LINK_SUMMARIES", 2) or 2), 0)
    timeout_sec = float(cfg("OLLAMA_MEDIA_FETCH_TIMEOUT_SEC", 5) or 5)
    max_summary_chars = max(int(cfg("OLLAMA_MAX_LINK_SUMMARY_CHARS", 240) or 240), 60)

    images = await extract_base64_images(message)
    link_summaries = await build_link_summaries_from_text(
        content(message),
        max_links=max_links,
        timeout_sec=timeout_sec,
        max_summary_chars=max_summary_chars,
    )

    prompt_parts = build_media_prompt_parts(image_count=len(images), link_summaries=link_summaries)
    analysis_text = build_media_analysis_text(image_count=len(images), link_summaries=link_summaries)

    return {
        "images": images,
        "image_count": len(images),
        "link_summaries": link_summaries,
        "prompt_parts": prompt_parts,
        "analysis_text": analysis_text,
    }
