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


class OllamaJSONTruncatedError(RuntimeError):
    pass


class OllamaJSONInvalidError(RuntimeError):
    pass


class OllamaImageNotSupportedError(RuntimeError):
    pass


class OllamaThinkingNotSupportedError(RuntimeError):
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
    return merged


def _build_options(think: bool, options: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(options or {})
    if "temperature" not in merged:
        try:
            merged["temperature"] = float(_cfg("OLLAMA_TEMPERATURE", 0.7) or 0.7)
        except Exception:
            pass
    if think:
        try:
            merged.setdefault("num_predict", int(_cfg("OLLAMA_THINK_NUM_PREDICT", 1024) or 1024))
        except Exception:
            pass
    return merged


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
