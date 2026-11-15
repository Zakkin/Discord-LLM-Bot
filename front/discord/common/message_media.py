# ollama_bot/common/message_media.py
import base64
import logging
from typing import Any


import discord

from ollama_bot.common.config_helpers import cfg
import ollama_bot.common.vision_caption as _vision_caption
from ollama_bot.common.vision_caption import (
    describe_image_for_identification,
    describe_images_b64,
    download_image_b64,
    download_image_bytes,
    score_image_candidates,
    verify_image_candidates,
)

_ORIG_DESCRIBE_IMAGES_B64 = describe_images_b64
_ORIG_DOWNLOAD_IMAGE_BYTES = download_image_bytes
_ORIG_DOWNLOAD_IMAGE_B64 = download_image_b64
from front.discord.common.discord_helpers import content
from lib.text_utils import extract_urls
from harness.agent.tool_registry.mcp.fact_check import build_link_summaries_from_text
from harness.agent.prompt_parser.prompt_manager.system_prompts import build_media_analysis_text, build_media_prompt_parts

log = logging.getLogger("ollama_bot.common.message_media")

from lib.media_utils import (
    is_image_attachment as _is_image_attachment,
    is_image_url as _is_image_url,
)

# 直近画像のキャプションキャッシュ（プロセス内メモリのみ、永続化しない、上限200件のFIFO）
_image_caption_cache: dict[int, str] = {}
_IMAGE_CAPTION_CACHE_MAX_SIZE = 200


def get_cached_image_caption(message_id: int) -> str | None:
    """キャッシュされた画像のキャプションを取得する。プロセス内メモリのみ（永続化しない）。"""
    if not isinstance(message_id, int):
        return None
    return _image_caption_cache.get(message_id)


def store_image_caption(message_id: int, caption: str) -> None:
    """画像のキャプションをキャッシュに保存する。上限200件のFIFO。プロセス内メモリのみ（永続化しない）。"""
    if not isinstance(message_id, int) or not caption:
        return
    if message_id in _image_caption_cache:
        del _image_caption_cache[message_id]
    elif len(_image_caption_cache) >= _IMAGE_CAPTION_CACHE_MAX_SIZE:
        oldest_key = next(iter(_image_caption_cache))
        del _image_caption_cache[oldest_key]
    _image_caption_cache[message_id] = str(caption).strip()


def clear_image_caption_cache() -> None:
    """テスト用: キャプションキャッシュを全消去する。"""
    _image_caption_cache.clear()


def message_has_image(message: discord.Message) -> bool:
    """メッセージに画像添付または画像URLが含まれているかを判定する。"""
    try:
        attachments = list(getattr(message, "attachments", []) or [])
        if any(_is_image_attachment(att) for att in attachments):
            return True
        urls = extract_urls(content(message))
        if any(_is_image_url(u) for u in urls):
            return True
    except Exception as e:
        log.debug("message_has_image check failed: %s", e)
        return False
    return False


def extract_first_image_url(message: Any) -> str:
    """メッセージに添付された画像ファイルまたは本文内の画像URLから最初の画像URLを取得する。"""
    if message is None:
        return ""
    try:
        attachments = list(getattr(message, "attachments", []) or [])
        for att in attachments:
            if _is_image_attachment(att):
                url = getattr(att, "url", "")
                if url:
                    return str(url)
        urls = extract_urls(content(message))
        for u in urls:
            if _is_image_url(u):
                return str(u)

        # 同期フォールバック: reference.resolved に画像がある場合
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        if resolved is not None and resolved is not message:
            return extract_first_image_url(resolved)
    except Exception as e:
        log.debug("extract_first_image_url check failed: %s", e)
        return ""
    return ""


async def resolve_message_with_media(message: discord.Message) -> discord.Message:
    """メッセージ自身に画像がない場合、返信先（reference）のメッセージから画像を自動解決する。"""
    if message_has_image(message):
        return message

    # 返信先メッセージの解決
    ref = getattr(message, "reference", None)
    if ref is not None:
        resolved = getattr(ref, "resolved", None)
        if isinstance(resolved, discord.Message) and message_has_image(resolved):
            return resolved

        # resolved が未キャッシュの場合、メッセージIDから非同期取得
        msg_id = getattr(ref, "message_id", None)
        channel = getattr(message, "channel", None)
        if msg_id and channel and hasattr(channel, "fetch_message"):
            try:
                fetched = await channel.fetch_message(msg_id)
                if fetched and message_has_image(fetched):
                    return fetched
            except Exception as e:
                log.debug("fetch referenced message for media failed: %r", e)

    return message


def message_has_supported_media_or_links(message: discord.Message) -> bool:
    """画像以外のサポート対象メディアまたは一般URLが含まれているかを判定する。"""
    try:
        if message_has_image(message):
            return False
        return bool(extract_urls(content(message)))
    except Exception:
        return False


async def extract_base64_images(message: discord.Message) -> list[str]:
    """メッセージ内の画像をBase64化して返す。"""
    target_msg = await resolve_message_with_media(message)
    max_images = max(int(cfg("OLLAMA_MAX_MEDIA_IMAGES", 2) or 2), 0)
    max_image_bytes = max(int(cfg("OLLAMA_MAX_IMAGE_BYTES", 5 * 1024 * 1024) or (5 * 1024 * 1024)), 1024)
    timeout_sec = float(cfg("OLLAMA_MEDIA_FETCH_TIMEOUT_SEC", 5) or 5)

    images: list[str] = []

    attachments = list(getattr(target_msg, "attachments", []) or [])
    for att in attachments:
        if len(images) >= max_images:
            break
        if not _is_image_attachment(att):
            continue
        encoded = await _attachment_to_base64(att, max_bytes=max_image_bytes)
        if encoded:
            images.append(encoded)

    urls = extract_urls(content(target_msg))
    for url in [u for u in urls if _is_image_url(u)]:
        if len(images) >= max_images:
            break
        encoded = await _url_image_to_base64(url, max_bytes=max_image_bytes, timeout_sec=timeout_sec)
        if encoded:
            images.append(encoded)

    return images


async def describe_images_with_vision_model(
    images: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> list[str]:
    """共通ビジョン部品（ollama_bot.common.vision_caption）への委譲ラッパ。"""
    target = (
        describe_images_b64
        if describe_images_b64 is not _ORIG_DESCRIBE_IMAGES_B64
        else _vision_caption.describe_images_b64
    )
    return await target(images, vision_model=vision_model, timeout_sec=timeout_sec)


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
    target = (
        download_image_bytes
        if download_image_bytes is not _ORIG_DOWNLOAD_IMAGE_BYTES
        else _vision_caption.download_image_bytes
    )
    return await target(url, max_bytes=max_bytes, timeout_sec=timeout_sec)


async def _url_image_to_base64(url: str, *, max_bytes: int, timeout_sec: float) -> str | None:
    target = (
        download_image_b64
        if download_image_b64 is not _ORIG_DOWNLOAD_IMAGE_B64
        else _vision_caption.download_image_b64
    )
    return await target(url, max_bytes=max_bytes, timeout_sec=timeout_sec)


async def build_message_media_context(message: discord.Message) -> dict[str, Any]:
    max_links = max(int(cfg("OLLAMA_MAX_LINK_SUMMARIES", 2) or 2), 0)
    timeout_sec = float(cfg("OLLAMA_MEDIA_FETCH_TIMEOUT_SEC", 5) or 5)
    max_summary_chars = max(int(cfg("OLLAMA_MAX_LINK_SUMMARY_CHARS", 240) or 240), 60)

    target_msg = await resolve_message_with_media(message)
    images = await extract_base64_images(target_msg)
    link_summaries = await build_link_summaries_from_text(
        content(message),
        max_links=max_links,
        timeout_sec=timeout_sec,
        max_summary_chars=max_summary_chars,
    )

    # 2段階Visionパイプライン: 画像が存在する場合に客観的キャプションを抽出
    image_descriptions: list[str] = []
    pipeline_mode = str(cfg("VISION_PIPELINE_MODE", "caption_then_main") or "").strip().lower()
    if images and pipeline_mode != "direct":
        image_descriptions = await describe_images_with_vision_model(images)
        if image_descriptions:
            target_msg_id = getattr(target_msg, "id", None)
            if isinstance(target_msg_id, int):
                store_image_caption(target_msg_id, "\n".join(image_descriptions))

    prompt_parts = build_media_prompt_parts(image_count=len(images), link_summaries=link_summaries)

    # analysis_text の構築: キャプションが存在すれば詳細内容を注入
    analysis_parts: list[str] = []
    if image_descriptions:
        for idx, desc in enumerate(image_descriptions, 1):
            analysis_parts.append(f"添付画像{idx}の解析内容:\n{desc}")
    elif len(images) > 0:
        analysis_parts.append(f"画像{len(images)}件あり")

    # === 追加: 添付音声・動画の解析テキスト追記 ===
    from front.discord.common.message_media_av import (
        is_audio_attachment,
        is_video_attachment,
        build_audio_analysis_text,
        build_video_analysis_text,
    )
    audio_idx = 1
    video_idx = 1
    for att in list(getattr(target_msg, "attachments", []) or []):
        if is_audio_attachment(att):
            a_text = await build_audio_analysis_text(att, idx=audio_idx)
            if a_text:
                analysis_parts.append(a_text)
                audio_idx += 1
        elif is_video_attachment(att):
            v_text = await build_video_analysis_text(att, idx=video_idx)
            if v_text:
                analysis_parts.append(v_text)
                video_idx += 1
    # ===============================================

    if link_summaries:
        analysis_parts.append("リンク要約:")
        analysis_parts.extend([f"- {item}" for item in link_summaries])

    analysis_text = "\n\n".join(analysis_parts).strip()

    return {
        "images": images,
        "image_count": len(images),
        "image_descriptions": image_descriptions,
        "link_summaries": link_summaries,
        "prompt_parts": prompt_parts,
        "analysis_text": analysis_text,
    }


async def caption_message_first_image(message: discord.Message) -> str | None:
    """メッセージ内の最初の画像1枚だけをVisionモデルでキャプションし、キャッシュに保存して返す。"""
    if message is None:
        return None
    try:
        images = await extract_base64_images(message)
        if not images:
            return None
        target_images = [images[0]]
        descriptions = await describe_images_with_vision_model(target_images)
        caption = (descriptions[0] if descriptions else "").strip()
        if caption:
            mid = getattr(message, "id", None)
            if isinstance(mid, int):
                store_image_caption(mid, caption)
            return caption
    except Exception as e:
        log.debug("caption_message_first_image failed: %r", e)
    return None

