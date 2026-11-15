"""プラットフォーム非依存の Vision（画像キャプション・同定・検証・採点・取得）共通モジュール。"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import aiohttp

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.http_session import get_shared_http_session
from ollama_bot.common.ollama_helpers import (
    OllamaContextSizeExceededError,
    call_ollama,
    release_ollama_model,
)

log = logging.getLogger("ollama_bot.common.vision_caption")

__all__ = [
    "caption_num_ctx_candidates",
    "describe_image_b64",
    "describe_images_b64",
    "describe_image_for_identification",
    "verify_image_candidates",
    "score_image_candidates",
    "download_image_bytes",
    "download_image_b64",
]

_DEFAULT_USER_AGENT = "discord-ai-bot/1.0"
_DOWNLOAD_CHUNK_BYTES = 65536
_NUM_CTX_ESCALATION_STEPS = (8192, 16384)

_DEFAULT_CAPTION_SYSTEM_PROMPT = (
    "あなたは画像分析・状況把握のアシスタントです。\n"
    "提供された画像の内容を、客観的かつ具体的に日本語で説明してください。\n"
    "【出力要件】\n"
    "1. 主な被写体、情景、アクションを簡潔に描写してください。\n"
    "2. 画像内に読み取れる看板・UI・セリフなどの文字があれば、漏れなく書き起こしてください。\n"
    "3. AIとしての挨拶、前置き、主観的な感想、会話は一切不要です。画像の内容説明のみを出力してください。"
)
_DEFAULT_CAPTION_USER_PROMPT = (
    "この画像に何が写っているか、客観的かつ具体的に日本語で説明してください。文字があれば書き起こしてください。"
)


def caption_num_ctx_candidates(base_num_ctx: int) -> list[int]:
    """キャプション生成で試す num_ctx の候補列（昇順・重複なし）。

    既定の num_ctx でコンテキスト超過になった場合にだけ、より大きな値へ段階的に上げる。
    """
    raw = int(base_num_ctx or 0)
    base = raw if raw > 0 else 4096
    candidates = [base]
    for value in _NUM_CTX_ESCALATION_STEPS:
        if value > base and value not in candidates:
            candidates.append(value)
    return candidates


def _resolve_vision_model(vision_model: str | None) -> str:
    return (vision_model or str(cfg("OLLAMA_VISION_MODEL", "") or "")).strip()


def _resolve_caption_options(
    *,
    timeout_sec: float | None,
    num_predict: int | None,
) -> tuple[float, int, float, list[int]]:
    describe_timeout = float(timeout_sec or cfg("OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC", 60.0) or 60.0)
    resolved_num_predict = int(
        num_predict if num_predict is not None else (cfg("OLLAMA_VISION_NUM_PREDICT", 256) or 256)
    )
    raw_temp = cfg("OLLAMA_VISION_TEMPERATURE", 0.1)
    temperature = 0.1 if raw_temp in (None, "") else float(raw_temp)
    base_num_ctx = int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096)
    return describe_timeout, resolved_num_predict, temperature, caption_num_ctx_candidates(base_num_ctx)


async def _release_vision_model(target_model: str) -> None:
    try:
        await release_ollama_model(target_model, timeout_sec=5.0)
    except Exception as e:
        log.debug("release_ollama_model failed after vision: %r", e)


async def _describe_single_image_b64(
    image_b64: str,
    *,
    target_model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    num_predict: int,
    num_ctx_candidates: list[int],
    timeout_sec: float,
    image_index: int | None = None,
) -> str:
    for attempt_idx, attempt_num_ctx in enumerate(num_ctx_candidates):
        if attempt_idx > 0:
            log.info(
                "[vision] Vision captioning retry with larger num_ctx: model=%s num_ctx=%d",
                target_model,
                attempt_num_ctx,
            )
        try:
            resp = await call_ollama(
                user_prompt,
                system_prompt=system_prompt,
                think=False,
                model=target_model,
                images=[image_b64],
                temperature=temperature,
                num_predict=num_predict,
                num_ctx=attempt_num_ctx,
                timeout_sec=timeout_sec,
                retries=1 if attempt_idx == 0 else 0,
            )
            return (resp or "").strip()
        except OllamaContextSizeExceededError as e:
            if attempt_idx + 1 >= len(num_ctx_candidates):
                if image_index is not None:
                    log.warning("vision captioning failed for image %d: %r", image_index, e)
                else:
                    log.warning("[vision] Vision captioning failed: %r", e)
                return ""
            continue
        except Exception as e:
            if image_index is not None:
                log.warning("vision captioning failed for image %d: %r", image_index, e)
            else:
                log.warning("[vision] Vision captioning failed: %r", e)
            return ""
    return ""


async def describe_image_b64(
    image_b64: str,
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    num_predict: int | None = None,
) -> str:
    """単一のBase64画像をVisionモデルでキャプション・文字起こしし、完了後にモデルを即時アンロードする。"""
    if not image_b64:
        return ""

    target_model = _resolve_vision_model(vision_model)
    if not target_model:
        return ""

    describe_timeout, resolved_num_predict, temperature, num_ctx_candidates = _resolve_caption_options(
        timeout_sec=timeout_sec,
        num_predict=num_predict,
    )
    resolved_system_prompt = _DEFAULT_CAPTION_SYSTEM_PROMPT if system_prompt is None else system_prompt
    resolved_user_prompt = _DEFAULT_CAPTION_USER_PROMPT if user_prompt is None else user_prompt

    try:
        return await _describe_single_image_b64(
            image_b64,
            target_model=target_model,
            system_prompt=resolved_system_prompt,
            user_prompt=resolved_user_prompt,
            temperature=temperature,
            num_predict=resolved_num_predict,
            num_ctx_candidates=num_ctx_candidates,
            timeout_sec=describe_timeout,
        )
    finally:
        await _release_vision_model(target_model)


async def describe_images_b64(
    images: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
) -> list[str]:
    """複数のBase64画像をVisionモデルで順次キャプション・文字起こしし、全処理後にモデルを1回だけアンロードする。"""
    if not images:
        return []

    target_model = _resolve_vision_model(vision_model)
    if not target_model:
        return []

    valid_images = [img for img in images if img]
    if not valid_images:
        return []

    describe_timeout, resolved_num_predict, temperature, num_ctx_candidates = _resolve_caption_options(
        timeout_sec=timeout_sec,
        num_predict=None,
    )
    resolved_system_prompt = _DEFAULT_CAPTION_SYSTEM_PROMPT if system_prompt is None else system_prompt
    resolved_user_prompt = _DEFAULT_CAPTION_USER_PROMPT if user_prompt is None else user_prompt

    descriptions: list[str] = []
    try:
        for idx, img_b64 in enumerate(valid_images, 1):
            text = await _describe_single_image_b64(
                img_b64,
                target_model=target_model,
                system_prompt=resolved_system_prompt,
                user_prompt=resolved_user_prompt,
                temperature=temperature,
                num_predict=resolved_num_predict,
                num_ctx_candidates=num_ctx_candidates,
                timeout_sec=describe_timeout,
                image_index=idx,
            )
            if text:
                descriptions.append(text)
    finally:
        await _release_vision_model(target_model)

    return descriptions


async def describe_image_for_identification(
    image_b64: str,
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> str:
    """同定専用の固定プロンプトで構造化記述を得る（推論完了後に即時アンロード）。"""
    target_model = _resolve_vision_model(vision_model)
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
        await _release_vision_model(target_model)


def _parse_json_object(resp: str | None) -> dict[str, Any]:
    m = re.search(r"\{.*\}", resp or "", re.DOTALL)
    if not m:
        return {}
    parsed = json.loads(m.group(0))
    return parsed if isinstance(parsed, dict) else {}


async def verify_image_candidates(
    target_b64: str,
    candidate_b64s: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> list[dict[str, Any]]:
    """対象画像と候補画像を2枚見比べ、同一対象かを固定JSONで判定する（一括推論後に即時アンロード）。"""
    target_model = _resolve_vision_model(vision_model)
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
                parsed = _parse_json_object(resp)
                verdict = parsed.get("verdict") if parsed.get("verdict") in ("match", "partial", "no") else "no"
                results.append({
                    "verdict": verdict,
                    "confidence": int(parsed.get("confidence", 0) or 0),
                    "reason": str(parsed.get("reason", "") or "")[:30],
                })
            except Exception:
                results.append({"verdict": "no", "confidence": 0, "reason": "判定不能"})
    finally:
        await _release_vision_model(target_model)

    return results


async def score_image_candidates(
    query: str,
    image_b64s: list[str],
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> list[dict[str, Any]]:
    """希望テキストと候補画像の一致度を採点する（一括推論後に即時アンロード）。"""
    target_model = _resolve_vision_model(vision_model)
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
                parsed = _parse_json_object(resp)
                score = max(0, min(100, int(parsed.get("match_score", 0) or 0)))
                results.append({
                    "match_score": score,
                    "reason": str(parsed.get("reason", "") or "")[:30],
                })
            except Exception:
                results.append({"match_score": 0, "reason": "採点不能"})
    finally:
        await _release_vision_model(target_model)

    return results


async def download_image_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout_sec: float,
    headers: dict[str, str] | None = None,
) -> bytes | None:
    """URLから画像をストリーミングダウンロードし、max_bytes超過または非200応答時はNoneを返す。"""
    if not url:
        return None
    request_headers = {"User-Agent": _DEFAULT_USER_AGENT}
    if headers:
        request_headers.update(headers)
    try:
        session = await get_shared_http_session(
            timeout_sec=timeout_sec,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
        )
        async with session.get(
            url,
            headers=request_headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=float(timeout_sec)),
        ) as resp:
            if resp.status != 200:
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        log.debug("url download failed: %s", e)
        return None


async def download_image_b64(
    url: str,
    *,
    max_bytes: int,
    timeout_sec: float,
    headers: dict[str, str] | None = None,
) -> str | None:
    """URLから画像をダウンロードし、Base64文字列として返す（失敗時はNone）。"""
    data = await download_image_bytes(
        url,
        max_bytes=max_bytes,
        timeout_sec=timeout_sec,
        headers=headers,
    )
    if not data:
        return None
    return base64.b64encode(data).decode("ascii")
