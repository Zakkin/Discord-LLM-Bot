# ollama_bot/common/message_media.py
import base64
import json
import logging
import re
from typing import Any


import discord

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.ollama_helpers import call_ollama, release_ollama_model
from front.discord.common.discord_helpers import content
from lib.text_utils import extract_urls
from harness.agent.tool_registry.mcp.fact_check import build_link_summaries_from_text
from ollama_bot.common.http_session import get_shared_http_session
from harness.agent.prompt_parser.prompt_manager.system_prompts import build_media_analysis_text, build_media_prompt_parts

log = logging.getLogger("ollama_bot.common.message_media")

from lib.media_utils import (
    is_image_attachment as _is_image_attachment,
    is_image_url as _is_image_url,
)


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
    """添付画像をVisionモデル（qwen3-vl:8b等）で客観的にキャプション・文字起こしし、実行後にモデルを即時アンロードする。"""
    if not images:
        return []

    target_model = (vision_model or str(cfg("OLLAMA_VISION_MODEL", "") or "")).strip()
    if not target_model:
        return []

    describe_timeout = timeout_sec or float(cfg("OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC", 60.0) or 60.0)
    num_predict = int(cfg("OLLAMA_VISION_NUM_PREDICT", 256) or 256)
    num_ctx = int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096)
    temperature = float(cfg("OLLAMA_VISION_TEMPERATURE", 0.1) or 0.1)

    system_prompt = (
        "あなたは画像分析・状況把握のアシスタントです。\n"
        "提供された画像の内容を、客観的かつ具体的に日本語で説明してください。\n"
        "【出力要件】\n"
        "1. 主な被写体、情景、アクションを簡潔に描写してください。\n"
        "2. 画像内に読み取れる看板・UI・セリフなどの文字があれば、漏れなく書き起こしてください。\n"
        "3. AIとしての挨拶、前置き、主観的な感想、会話は一切不要です。画像の内容説明のみを出力してください。"
    )
    user_prompt = "この画像に何が写っているか、客観的かつ具体的に日本語で説明してください。文字があれば書き起こしてください。"

    descriptions: list[str] = []
    try:
        for idx, img_b64 in enumerate(images, 1):
            try:
                resp = await call_ollama(
                    user_prompt,
                    system_prompt=system_prompt,
                    think=False,
                    model=target_model,
                    images=[img_b64],
                    temperature=temperature,
                    num_predict=num_predict,
                    num_ctx=num_ctx,
                    timeout_sec=describe_timeout,
                    retries=1,
                )
                text = (resp or "").strip()
                if text:
                    descriptions.append(text)
            except Exception as e:
                log.warning("vision captioning failed for image %d: %r", idx, e)
    finally:
        # VRAM競合防止：成功・失敗・例外問わず必ず即時アンロード (BAN-170準拠)
        try:
            await release_ollama_model(target_model, timeout_sec=5.0)
        except Exception as e:
            log.debug("release_ollama_model failed after vision: %r", e)

    return descriptions


async def describe_image_for_identification(
    image_b64: str,
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> str:
    """同定専用の固定プロンプトで構造化記述を得る（推論完了後に即時アンロード）。"""
    target_model = (vision_model or str(cfg("OLLAMA_VISION_MODEL", "") or "")).strip()
    if not target_model or not image_b64:
        return ""

    system_prompt = (
        "あなたは画像同定・特定のアシスタントです。提供された画像の特徴を客観的に記述してください。\n"
        "【出力項目】\n"
        "1. 被写体・キャラクターの特徴（髪型・髪色・瞳の色・服装・装飾品）\n"
        "2. 画風（アニメ・イラスト・実写・3D等）\n"
        "3. 画像内の文字（看板・ロゴ・セリフ等があれば全文書き起こし）\n"
        "4. 推定ジャンル・作品の雰囲気\n"
        "※分からない項目は『不明』と記述し、推測や創作で埋めないでください。前置きや挨拶は不要です。"
    )
    user_prompt = "この画像の特徴を客観的に同定用に記述してください。"
    try:
        resp = await call_ollama(
            user_prompt,
            system_prompt=system_prompt,
            think=False,
            model=target_model,
            images=[image_b64],
            temperature=0.1,
            num_predict=512,
            num_ctx=int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096),
            timeout_sec=timeout_sec or float(cfg("OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC", 60.0) or 60.0),
            retries=1,
        )
        return (resp or "").strip()
    finally:
        await release_ollama_model(target_model, timeout_sec=5.0)


async def verify_image_candidates(
    target_b64: str,
    candidate_b64s: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> list[dict[str, Any]]:
    """対象画像と候補画像を2枚見比べ、同一対象かを固定JSONで判定する（一括推論後に即時アンロード）。"""
    target_model = (vision_model or str(cfg("OLLAMA_VISION_MODEL", "") or "")).strip()
    if not target_model or not target_b64 or not candidate_b64s:
        return []

    system_prompt = (
        "あなたは2枚の画像を比較して同一のキャラクターまたは同一の作品かを厳密に判定する検証AIです。\n"
        "1枚目が『検索対象画像』、2枚目が『候補画像』です。\n"
        "以下のJSON形式【のみ】を出力してください。前置きや挨拶、解説文は絶対に出力してはなりません。\n"
        '{"verdict": "match" | "partial" | "no", "confidence": 0〜100の整数, "reason": "30文字以内の判定理由"}\n'
        "※match=同一キャラ/同一作品と断定、partial=似ているが確証なし、no=無関係。"
    )
    user_prompt = "1枚目の対象画像と2枚目の候補画像を比較し、指定のJSON形式のみで判定を出力してください。"

    results: list[dict[str, Any]] = []
    try:
        for cand_b64 in candidate_b64s:
            try:
                resp = await call_ollama(
                    user_prompt,
                    system_prompt=system_prompt,
                    think=False,
                    model=target_model,
                    images=[target_b64, cand_b64],
                    temperature=0.1,
                    num_predict=128,
                    num_ctx=int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096),
                    timeout_sec=timeout_sec or 30.0,
                    retries=1,
                )
                m = re.search(r"\{.*\}", resp or "", re.DOTALL)
                parsed = json.loads(m.group(0)) if m else {}
                verdict = parsed.get("verdict") if parsed.get("verdict") in ("match", "partial", "no") else "no"
                results.append({
                    "verdict": verdict,
                    "confidence": int(parsed.get("confidence", 0) or 0),
                    "reason": str(parsed.get("reason", "") or "")[:30],
                })
            except Exception:
                results.append({"verdict": "no", "confidence": 0, "reason": "判定不能"})
    finally:
        await release_ollama_model(target_model, timeout_sec=5.0)

    return results


async def score_image_candidates(
    query: str,
    image_b64s: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> list[dict[str, Any]]:
    """希望テキストと候補画像の一致度を採点する（一括推論後に即時アンロード）。"""
    target_model = (vision_model or str(cfg("OLLAMA_VISION_MODEL", "") or "")).strip()
    if not target_model or not image_b64s:
        return []

    system_prompt = (
        "あなたはユーザーの希望条件に画像が合致しているかを客観的に採点するAIです。\n"
        f"ユーザーの希望: {query}\n"
        "画像を見て、希望にどれだけ合致しているかを以下のJSON形式【のみ】で出力してください。\n"
        '{"match_score": 0〜100の整数, "reason": "30文字以内の採点理由"}\n'
        "※前置きや挨拶、解説文は絶対に出力してはなりません。"
    )
    user_prompt = "この画像が希望条件にどれくらい合致しているか、指定のJSON形式のみで採点を出力してください。"

    results: list[dict[str, Any]] = []
    try:
        for img_b64 in image_b64s:
            try:
                resp = await call_ollama(
                    user_prompt,
                    system_prompt=system_prompt,
                    think=False,
                    model=target_model,
                    images=[img_b64],
                    temperature=0.1,
                    num_predict=128,
                    num_ctx=int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096),
                    timeout_sec=timeout_sec or 30.0,
                    retries=1,
                )
                m = re.search(r"\{.*\}", resp or "", re.DOTALL)
                parsed = json.loads(m.group(0)) if m else {}
                score = max(0, min(100, int(parsed.get("match_score", 0) or 0)))
                results.append({
                    "match_score": score,
                    "reason": str(parsed.get("reason", "") or "")[:30],
                })
            except Exception:
                results.append({"match_score": 0, "reason": "採点不能"})
    finally:
        await release_ollama_model(target_model, timeout_sec=5.0)

    return results


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
