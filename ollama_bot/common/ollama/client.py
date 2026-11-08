from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..json_compat import aiohttp_dumps
from .resolve import resolve_cfg, resolve_ollama_fn

log = logging.getLogger("ollama_bot.common.ollama_helpers.client")

_cfg = resolve_cfg


_shared_ollama_session: aiohttp.ClientSession | None = None
_shared_ollama_session_lock: asyncio.Lock | None = None
_ollama_request_semaphore: asyncio.Semaphore | None = None
_ollama_request_semaphore_limit: int | None = None


def _cfg_int_value(name: str, default: int) -> int:
    try:
        return int(_cfg(name, default))
    except (TypeError, ValueError):
        return default


def _cfg_float_value(name: str, default: float) -> float:
    try:
        return float(_cfg(name, default))
    except (TypeError, ValueError):
        return default


def _get_ollama_request_semaphore() -> asyncio.Semaphore:
    global _ollama_request_semaphore
    global _ollama_request_semaphore_limit

    limit = max(_cfg_int_value("OLLAMA_MAX_CONCURRENT_REQUESTS", 1), 1)
    if _ollama_request_semaphore is None or _ollama_request_semaphore_limit != limit:
        _ollama_request_semaphore = asyncio.Semaphore(limit)
        _ollama_request_semaphore_limit = limit
    return _ollama_request_semaphore


from .structured import (
    OllamaImageNotSupportedError,
    OllamaJSONInvalidError,
    OllamaJSONTruncatedError,
)


class OllamaThinkingNotSupportedError(RuntimeError):
    pass


class OllamaContextSizeExceededError(RuntimeError):
    pass


@dataclass
class OllamaChatResult:
    content: str
    done_reason: str | None
    truncated: bool
    thinking: str = ""
    tool_calls: list[dict[str, Any]] | None = None


def _build_timeout(timeout_sec: float | None = None) -> aiohttp.ClientTimeout:
    resolved = float(timeout_sec if timeout_sec is not None else (_cfg("OLLAMA_TIMEOUT_SEC", 300) or 300))
    return aiohttp.ClientTimeout(
        total=resolved,
        sock_connect=min(resolved, 10.0),
        sock_read=resolved,
    )


async def _get_ollama_session(timeout_sec: float | None = None) -> aiohttp.ClientSession:
    global _shared_ollama_session
    global _shared_ollama_session_lock

    if _shared_ollama_session_lock is None:
        _shared_ollama_session_lock = asyncio.Lock()

    if _shared_ollama_session is not None and not _shared_ollama_session.closed:
        return _shared_ollama_session

    async with _shared_ollama_session_lock:
        if _shared_ollama_session is None or _shared_ollama_session.closed:
            _shared_ollama_session = aiohttp.ClientSession(
                base_url=str(_cfg("OLLAMA_BASE_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").rstrip("/"),
                timeout=_build_timeout(None),
                headers={"User-Agent": "discord-ai-bot/1.0"},
                json_serialize=aiohttp_dumps,
            )
            log.info("created shared ollama session")
        return _shared_ollama_session


async def close_ollama_session() -> None:
    global _shared_ollama_session
    global _shared_ollama_session_lock

    if _shared_ollama_session is not None and not _shared_ollama_session.closed:
        await _shared_ollama_session.close()
        log.info("closed shared ollama session")

    _shared_ollama_session = None
    _shared_ollama_session_lock = None


async def _reset_ollama_session() -> None:
    global _shared_ollama_session
    global _shared_ollama_session_lock

    if _shared_ollama_session is not None and not _shared_ollama_session.closed:
        await _shared_ollama_session.close()
        log.warning("reset shared ollama session")

    _shared_ollama_session = None
    _shared_ollama_session_lock = None


def _resolve_keep_alive(model: str | None) -> str | int | None:
    def _normalize_keep_alive(value: Any) -> str | int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            return str(value)

        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"[-+]?\d+", text):
            try:
                return int(text)
            except Exception:
                return text
        return text

    model_name = str(model or _cfg("OLLAMA_MODEL", "") or "").strip()
    default_model = str(_cfg("OLLAMA_MODEL", "") or "").strip()
    main_model = str(_cfg("OLLAMA_MAIN_MODEL", default_model) or default_model).strip()
    if model_name and model_name in {default_model, main_model}:
        return _normalize_keep_alive(_cfg("OLLAMA_MAIN_KEEP_ALIVE", None))
    if model_name == str(_cfg("OLLAMA_MIDDLE_MODEL", "") or ""):
        return _normalize_keep_alive(_cfg("OLLAMA_MIDDLE_KEEP_ALIVE", _cfg("OLLAMA_CLASSIFIER_KEEP_ALIVE", None)))
    if model_name == str(_cfg("OLLAMA_CLASSIFIER_MODEL", "") or ""):
        return _normalize_keep_alive(_cfg("OLLAMA_CLASSIFIER_KEEP_ALIVE", None))
    if model_name == str(_cfg("OLLAMA_VISION_MODEL", "") or ""):
        return _normalize_keep_alive(_cfg("OLLAMA_VISION_KEEP_ALIVE", None))
    # 埋め込みモデル（bge-m3 等）の keep_alive 解決
    embed_model = str(_cfg("OLLAMA_EMBED_MODEL", "bge-m3") or "bge-m3").strip()
    if model_name and (model_name == embed_model or model_name == "bge-m3" or "embed" in model_name.lower()):
        return _normalize_keep_alive(_cfg("OLLAMA_EMBED_KEEP_ALIVE", "0"))
    return _normalize_keep_alive(_cfg("OLLAMA_MAIN_KEEP_ALIVE", None))


async def release_ollama_model(model_name: str, *, timeout_sec: float = 8.0) -> None:
    """指定モデルをOllama VRAMから即時解放する（best-effort）。

    Ollamaは keep_alive=0 を含むリクエストを受け取るとそのモデルをアンロードする。
    次に別モデルを呼び出す前にこの関数を実行することで、モデル切り替え時のVRAM競合を防ぐ。
    失敗しても例外を飛ばさない（呼び出し元のフローに影響させない）。
    """
    if not model_name:
        return
    try:
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [],
            "keep_alive": 0,
            "stream": False,
        }
        session = await _get_ollama_session()
        async with _get_ollama_request_semaphore():
            async with session.post(
                "/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                await resp.read()
        log.info("released model from VRAM: model=%s", model_name)
    except Exception as e:
        log.debug("release_ollama_model failed (non-fatal): model=%s err=%r", model_name, e)


def _should_send_think_flag() -> bool:
    value = _cfg("OLLAMA_SEND_THINK_FLAG", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _merge_generation_kwargs_into_options(
    options: dict[str, Any] | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    repeat_penalty: float | None = None,
    num_predict: int | None = None,
    num_ctx: int | None = None,
) -> dict[str, Any]:
    merged = dict(options or {})
    if temperature is not None:
        merged["temperature"] = temperature
    if top_p is not None:
        merged["top_p"] = top_p
    if top_k is not None:
        merged["top_k"] = top_k
    if min_p is not None:
        merged["min_p"] = min_p
    if presence_penalty is not None:
        merged["presence_penalty"] = presence_penalty
    if repeat_penalty is not None:
        merged["repeat_penalty"] = repeat_penalty
    if num_predict is not None:
        merged["num_predict"] = num_predict
    if num_ctx is not None:
        merged["num_ctx"] = num_ctx
    return merged


def _build_options(think: bool, options: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(options or {})
    if "temperature" not in merged:
        try:
            merged["temperature"] = float(_cfg("OLLAMA_TEMPERATURE", 0.7) or 0.7)
        except Exception:
            pass
    if "num_ctx" not in merged:
        try:
            raw_ctx = _cfg("OLLAMA_NUM_CTX", 16384)
            if raw_ctx is not None:
                merged["num_ctx"] = int(raw_ctx)
            else:
                merged["num_ctx"] = 16384
        except Exception:
            merged["num_ctx"] = 16384
    if think:
        try:
            merged.setdefault("num_predict", int(_cfg("OLLAMA_THINK_NUM_PREDICT", 1024) or 1024))
        except Exception:
            pass
    return merged


def count_payload_chars_and_estimate_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """ペイロード内の全メッセージとツール定義の文字数を計測し、推定入力トークン数を算出する。"""
    content_parts: list[str] = []
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, str):
            content_parts.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and "text" in part:
                    content_parts.append(str(part["text"]))
                elif isinstance(part, str):
                    content_parts.append(part)
        elif c is not None:
            content_parts.append(str(c))
    if tools:
        import json
        try:
            content_parts.append(json.dumps(tools, ensure_ascii=False))
        except Exception:
            content_parts.append(str(tools))

    full_text = "".join(content_parts)
    prompt_chars = len(full_text)
    if prompt_chars == 0:
        return 0, 0

    cjk_count = len(re.findall(r"[^\x00-\x7F]", full_text))
    ascii_count = prompt_chars - cjk_count
    estimated_tokens = int(ascii_count * 0.35 + cjk_count * 1.15)
    return prompt_chars, estimated_tokens


def adjust_options_for_prompt_size(
    options: dict[str, Any],
    *,
    prompt_chars: int,
    estimated_input_tokens: int,
    think: bool = False,
) -> dict[str, Any]:
    """事前計測した文字数・トークン数に基づき options['num_predict'] と options['num_ctx'] を可変調整する。"""
    adjusted = dict(options)

    # 1. num_predict の可変制御
    configured_think_predict = _cfg_int_value("OLLAMA_THINK_NUM_PREDICT", 1536)
    min_think_predict = max(1536, configured_think_predict)

    current_predict_raw = adjusted.get("num_predict")
    try:
        current_predict = int(current_predict_raw) if current_predict_raw is not None else None
    except (TypeError, ValueError):
        current_predict = None

    if think:
        if current_predict is None:
            adjusted["num_predict"] = min_think_predict
        else:
            adjusted["num_predict"] = max(current_predict, min_think_predict)
    else:
        long_predict = max(_cfg_int_value("OLLAMA_REPLY_NUM_PREDICT_LONG", 1536), 1536)
        if current_predict is None:
            if prompt_chars >= 6000:
                adjusted["num_predict"] = long_predict
            else:
                adjusted["num_predict"] = _cfg_int_value("OLLAMA_REPLY_NUM_PREDICT", 512)
        else:
            if prompt_chars >= 6000 and current_predict < long_predict:
                adjusted["num_predict"] = long_predict

    final_predict = int(adjusted.get("num_predict", 512))

    # 2. num_ctx の段階的可変制御
    base_num_ctx = _cfg_int_value("OLLAMA_NUM_CTX", 16384)
    specified_ctx_raw = options.get("num_ctx")
    try:
        specified_ctx = int(specified_ctx_raw) if specified_ctx_raw is not None else None
    except (TypeError, ValueError):
        specified_ctx = None

    effective_base_ctx = specified_ctx if (specified_ctx is not None and specified_ctx > 0) else base_num_ctx
    max_num_ctx = max(_cfg_int_value("OLLAMA_MAX_NUM_CTX", 32768), effective_base_ctx)

    required_tokens = estimated_input_tokens + final_predict + 512

    if required_tokens <= effective_base_ctx:
        final_num_ctx = effective_base_ctx
    else:
        bucket_step = 8192
        target_ctx = ((required_tokens + bucket_step - 1) // bucket_step) * bucket_step
        final_num_ctx = min(max(target_ctx, effective_base_ctx), max_num_ctx)

    adjusted["num_ctx"] = final_num_ctx

    log.info(
        "adjusted ollama options: prompt_chars=%d est_tokens=%d num_ctx=%d num_predict=%s",
        prompt_chars,
        estimated_input_tokens,
        final_num_ctx,
        adjusted.get("num_predict"),
    )
    return adjusted


def _bump_num_predict(
    options: dict[str, Any] | None,
    *,
    current_num_predict: int | None = None,
    minimum: int = 512,
    multiplier: float = 1.75,
) -> dict[str, Any]:
    merged = dict(options or {})
    base = current_num_predict
    if base is None:
        try:
            raw_predict = merged.get("num_predict")
            base = int(raw_predict) if raw_predict is not None else None
        except Exception:
            base = None
    if base is None or base <= 0:
        base = minimum
    merged["num_predict"] = max(int(base * multiplier), minimum)
    return merged


def _should_retry_length_limited_chat(options: dict[str, Any] | None, current_num_predict: int | None) -> bool:
    resolved = current_num_predict
    if resolved is None:
        try:
            raw_predict = (options or {}).get("num_predict")
            resolved = int(raw_predict) if raw_predict is not None else None
        except Exception:
            resolved = None
    if resolved is None:
        return True
    return resolved >= 64


def _is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in (429, 500, 502, 503, 504)
    return isinstance(exc, (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError))


def _is_ollama_busy_error(exc: Exception) -> bool:
    return isinstance(exc, aiohttp.ClientResponseError) and getattr(exc, "status", None) in (429, 503)


def _retry_sleep_seconds(exc: Exception, attempt: int) -> float:
    if _is_ollama_busy_error(exc):
        base = max(_cfg_float_value("OLLAMA_BUSY_RETRY_BASE_SEC", 4.0), 0.0)
        cap = max(_cfg_float_value("OLLAMA_BUSY_RETRY_MAX_SEC", 15.0), base)
        return min(base * max(attempt, 1), cap)
    base = max(_cfg_float_value("OLLAMA_RETRY_BASE_SEC", 1.5), 0.0)
    cap = max(_cfg_float_value("OLLAMA_RETRY_MAX_SEC", 3.0), base)
    return min(base * max(attempt, 1), cap)


def _is_bad_request_http_error(exc: Exception) -> bool:
    return isinstance(exc, aiohttp.ClientResponseError) and getattr(exc, "status", None) == 400


def _should_reset_ollama_session_for_error(exc: Exception) -> bool:
    return isinstance(exc, (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError))
