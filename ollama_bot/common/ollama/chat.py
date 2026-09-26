from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..json_compat import loads as json_loads
from ..ollama_reply_safety import _strip_think_blocks
from .client import (
    OllamaChatResult,
    OllamaImageNotSupportedError,
    OllamaThinkingNotSupportedError,
    _build_options,
    _build_timeout,
    _bump_num_predict,
    _get_ollama_request_semaphore,
    _get_ollama_session,
    _is_retryable_http_error,
    _merge_generation_kwargs_into_options,
    _reset_ollama_session,
    _resolve_keep_alive,
    _retry_sleep_seconds,
    _should_retry_length_limited_chat,
    _should_reset_ollama_session_for_error,
    _should_send_think_flag,
)
from .resolve import resolve_cfg, resolve_ollama_fn
from .text import (
    _log_ollama_payload,
    _looks_like_think_only_output,
    truncate_text,
)

log = logging.getLogger("ollama_bot.common.ollama_helpers.chat")

_cfg = resolve_cfg


async def _post_chat_once(
    *,
    payload: dict[str, Any],
    timeout_sec: float | None = None,
    log_http_errors: bool = True,
) -> OllamaChatResult:
    use_openai = str(_cfg("OLLAMA_USE_OPENAI_API", "false")).lower() in ("1", "true", "yes", "on")
    endpoint = "/v1/chat/completions" if use_openai else "/api/chat"

    request_payload = payload
    if use_openai:
        request_payload = {
            "model": payload.get("model", "default"),
            "messages": payload.get("messages", []),
            "stream": payload.get("stream", False),
        }
        opts = payload.get("options") or {}
        if "temperature" in opts:
            request_payload["temperature"] = opts["temperature"]
        if "top_p" in opts:
            request_payload["top_p"] = opts["top_p"]
        if "num_predict" in opts:
            request_payload["max_tokens"] = opts["num_predict"]
        if "presence_penalty" in opts:
            request_payload["presence_penalty"] = opts["presence_penalty"]

    session_fn = resolve_ollama_fn("_get_ollama_session", _get_ollama_session)
    timeout_fn = resolve_ollama_fn("_build_timeout", _build_timeout)
    session = await session_fn()
    async with _get_ollama_request_semaphore():
        async with session.post(endpoint, json=request_payload, timeout=timeout_fn(timeout_sec)) as response:
            if response.status >= 400:
                raw_text = await response.text()
                preview = truncate_text(raw_text, 1200)
                if log_http_errors:
                    log_fn = log.warning if response.status in (429, 500, 502, 503, 504) else log.error
                    log_fn(
                        "ollama http error status=%s body_head=%r",
                        response.status,
                        preview,
                    )
                if response.status == 500 and "missing data required for image input" in raw_text:
                    raise OllamaImageNotSupportedError(f"Model lacks vision support: {preview}")
                if response.status == 400 and "does not support thinking" in raw_text:
                    raise OllamaThinkingNotSupportedError(f"Model does not support thinking API parameter: {preview}")
                response.raise_for_status()

            content_pieces = []
            thinking_pieces = []
            tool_calls: list[Any] | None = None
            done_reason = None
            truncated = False

            if payload.get("stream", False):
                async for line in response.content:
                    line = line.strip()
                    if not line:
                        continue

                    if use_openai:
                        if line.startswith(b"data: "):
                            line = line[6:]
                        if line == b"[DONE]":
                            break

                    try:
                        data = json_loads(line.decode("utf-8"))
                    except Exception as e:
                        raise RuntimeError(f"Failed to decode chunk as JSON: {line!r}") from e

                    if use_openai:
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            c = delta.get("content")
                            if c:
                                content_pieces.append(c)
                            if choices[0].get("finish_reason"):
                                done_reason = choices[0].get("finish_reason")
                                truncated = (done_reason == "length")
                    else:
                        message = data.get("message", {}) or {}
                        c = message.get("content")
                        if c:
                            content_pieces.append(c)
                        t = message.get("thinking")
                        if t:
                            thinking_pieces.append(t)

                        if "tool_calls" in message and message["tool_calls"]:
                            if tool_calls is None:
                                tool_calls = []
                            tool_calls.extend(message["tool_calls"])

                        if data.get("done"):
                            done_reason = data.get("done_reason")
                            truncated = bool(data.get("truncated", False))
                            break

                content = "".join(content_pieces)
                thinking = "".join(thinking_pieces)
            else:
                raw_text = await response.text()
                try:
                    data = json_loads(raw_text)
                except Exception as e:
                    raise RuntimeError(f"Failed to decode response as JSON: {truncate_text(raw_text, 1200)}") from e

                if use_openai:
                    choices = data.get("choices", [])
                    if choices:
                        content = str(choices[0].get("message", {}).get("content", "") or "")
                        done_reason = choices[0].get("finish_reason")
                        truncated = (done_reason == "length")
                    else:
                        content = ""
                else:
                    message = data.get("message", {}) or {}
                    content = str(message.get("content", "") or "")
                    thinking = str(message.get("thinking", "") or "")
                    raw_tool_calls = message.get("tool_calls")
                    tool_calls = list(raw_tool_calls) if isinstance(raw_tool_calls, list) else None
                    done_reason = data.get("done_reason")
                    truncated = bool(data.get("truncated", False))

    return OllamaChatResult(
        content=content,
        done_reason=str(done_reason) if done_reason is not None else None,
        truncated=truncated,
        thinking=thinking,
        tool_calls=tool_calls,
    )


async def call_ollama(
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    think: bool = False,
    model: str | None = None,
    timeout_sec: float | None = None,
    retries: int = 1,
    images: list[str] | None = None,
    response_format: str | dict[str, Any] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    repeat_penalty: float | None = None,
    num_predict: int | None = None,
    options: dict[str, Any] | None = None,
    log_retryable_errors: bool = True,
    log_http_errors: bool = True,
) -> str:
    model_name = str(model or _cfg("OLLAMA_MODEL", "") or "").strip()
    if not model_name:
        raise RuntimeError("OLLAMA_MODEL is not configured")

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [],
        "stream": True,
        "options": _build_options(
            think=think,
            options=_merge_generation_kwargs_into_options(
                options,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                repeat_penalty=repeat_penalty,
                num_predict=num_predict,
            ),
        ),
    }
    if _should_send_think_flag():
        payload["think"] = bool(think)
    if response_format is not None:
        payload["format"] = response_format

    keep_alive = _resolve_keep_alive(model_name)
    if keep_alive not in (None, ""):
        payload["keep_alive"] = keep_alive

    if system_prompt:
        payload["messages"].append({"role": "system", "content": str(system_prompt)})

    user_message: dict[str, Any] = {"role": "user", "content": str(user_prompt or "")}
    if images:
        user_message["images"] = images
    payload["messages"].append(user_message)
    _log_ollama_payload(payload, kind="chat")

    attempts = max(int(retries or 0), 0) + 1
    last_exc: Exception | None = None

    current_attempt = 1
    while current_attempt <= attempts:
        attempt = current_attempt
        current_attempt += 1
        try:
            post_chat_fn = resolve_ollama_fn("_post_chat_once", _post_chat_once)
            result = await post_chat_fn(
                payload=payload,
                timeout_sec=timeout_sec,
                log_http_errors=log_http_errors,
            )
            log.info(
                "ollama response model=%s think=%s done_reason=%s truncated=%s content=\n%s",
                model_name,
                think,
                result.done_reason,
                result.truncated,
                result.content,
            )
            if (
                (result.truncated or result.done_reason == "length")
                and attempt < attempts
                and _should_retry_length_limited_chat(
                    payload.get("options") if isinstance(payload.get("options"), dict) else None,
                    num_predict,
                )
            ):
                if not think and _looks_like_think_only_output(result.content):
                    log.warning(
                        "ollama response was length-limited inside a think block despite think=false; "
                        "returning to caller guards without larger hidden retry model=%s",
                        model_name,
                    )
                    return result.content
                if _looks_like_think_only_output(result.content):
                    stripped = _strip_think_blocks(result.content).strip()
                    if stripped:
                        log.info(
                            "ollama response: think block stripped, returning body model=%s",
                            model_name,
                        )
                        return stripped
                    if attempt < attempts:
                        log.warning(
                            "ollama response length-limited inside think block (body empty); "
                            "bumping num_predict and retrying model=%s attempt=%s/%s",
                            model_name,
                            attempt,
                            attempts,
                        )
                        payload = dict(payload)
                        payload["options"] = _build_options(
                            think=think,
                            options=_bump_num_predict(
                                payload.get("options") if isinstance(payload.get("options"), dict) else None,
                            ),
                        )
                        _log_ollama_payload(payload, kind="chat-retry-think-only")
                        continue
                    log.warning(
                        "ollama response think-only with no retries left; returning empty model=%s",
                        model_name,
                    )
                    return ""
                log.info(
                    "ollama response was length-limited; retrying with larger num_predict model=%s attempt=%s/%s",
                    model_name,
                    attempt,
                    attempts,
                )
                payload = dict(payload)
                payload["options"] = _build_options(
                    think=think,
                    options=_bump_num_predict(
                        payload.get("options") if isinstance(payload.get("options"), dict) else None,
                    ),
                )
                _log_ollama_payload(payload, kind="chat-retry-length")
                continue

            if result.thinking:
                log.info("ollama native thinking output model=%s len=%s:\n%s", model_name, len(result.thinking), truncate_text(result.thinking, 1200))

            return result.content
        except OllamaThinkingNotSupportedError as e:
            last_exc = e
            log.warning("ollama model=%s rejected 'think: true'; falling back to think=False on attempt=%s/%s", model_name, attempt, attempts)
            think = False
            payload = dict(payload)
            payload.pop("think", None)
            if "options" in payload and isinstance(payload["options"], dict):
                payload["options"].pop("thinking", None)
            _log_ollama_payload(payload, kind="chat-retry-no-think")
            if attempt >= attempts:
                attempts = attempt + 1
            continue
        except OllamaImageNotSupportedError as e:
            last_exc = e
            log.warning("ollama model=%s rejected images; retrying without images on attempt=%s/%s", model_name, attempt, attempts)
            payload = dict(payload)
            new_messages = []
            for msg in payload.get("messages", []):
                new_msg = dict(msg)
                new_msg.pop("images", None)
                new_messages.append(new_msg)
            payload["messages"] = new_messages
            _log_ollama_payload(payload, kind="chat-retry-no-images")
            if attempt >= attempts:
                attempts = attempt + 1
            continue
        except Exception as e:
            last_exc = e
            is_retryable_fn = resolve_ollama_fn("_is_retryable_http_error", _is_retryable_http_error)
            should_reset_fn = resolve_ollama_fn("_should_reset_ollama_session_for_error", _should_reset_ollama_session_for_error)
            reset_session_fn = resolve_ollama_fn("_reset_ollama_session", _reset_ollama_session)
            retry_sleep_fn = resolve_ollama_fn("_retry_sleep_seconds", _retry_sleep_seconds)
            if is_retryable_fn(e):
                if log_retryable_errors:
                    log.warning(
                        "ollama request failed with retryable error on attempt=%s/%s: %r",
                        attempt,
                        attempts,
                        e,
                    )
                if should_reset_fn(e):
                    await reset_session_fn()
            if attempt >= attempts:
                break
            await asyncio.sleep(retry_sleep_fn(e, attempt))

    assert last_exc is not None
    raise last_exc


async def call_ollama_chat_raw(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    think: bool = False,
    timeout_sec: float | None = None,
    retries: int = 1,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    repeat_penalty: float | None = None,
    num_predict: int | None = None,
    options: dict[str, Any] | None = None,
) -> OllamaChatResult:
    model_name = str(model or _cfg("OLLAMA_MODEL", "") or "").strip()
    if not model_name:
        raise RuntimeError("OLLAMA_MODEL is not configured")

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": list(messages),
        "stream": False,
        "options": _build_options(
            think=think,
            options=_merge_generation_kwargs_into_options(
                options,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                repeat_penalty=repeat_penalty,
                num_predict=num_predict,
            ),
        ),
    }
    if _should_send_think_flag():
        payload["think"] = bool(think)

    if tools:
        payload["tools"] = list(tools)

    keep_alive = _resolve_keep_alive(model_name)
    if keep_alive not in (None, ""):
        payload["keep_alive"] = keep_alive

    _log_ollama_payload(payload, kind="chat_raw")

    attempts = max(int(retries or 0), 0) + 1
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            post_chat_fn = resolve_ollama_fn("_post_chat_once", _post_chat_once)
            result = await post_chat_fn(payload=payload, timeout_sec=timeout_sec)
            log.info(
                "ollama raw chat res model=%s done_reason=%s content_len=%d tools_cnt=%d",
                model_name,
                result.done_reason,
                len(result.content),
                len(result.tool_calls) if result.tool_calls else 0,
            )
            if result.thinking:
                log.info("ollama native thinking output model=%s len=%s:\n%s", model_name, len(result.thinking), truncate_text(result.thinking, 1200))
            return result
        except OllamaThinkingNotSupportedError as e:
            last_exc = e
            log.warning("ollama raw chat model=%s rejected 'think: true'; falling back to think=False on attempt=%s/%s", model_name, attempt, attempts)
            think = False
            payload = dict(payload)
            payload.pop("think", None)
            if "options" in payload and isinstance(payload["options"], dict):
                payload["options"].pop("thinking", None)
            _log_ollama_payload(payload, kind="chat_raw-retry-no-think")
            if attempt >= attempts:
                attempts = attempt + 1
            continue
        except OllamaImageNotSupportedError as e:
            last_exc = e
            log.warning("ollama raw chat model=%s rejected images; retrying without images on attempt=%s/%s", model_name, attempt, attempts)
            payload = dict(payload)
            new_messages = []
            for msg in payload.get("messages", []):
                new_msg = dict(msg)
                new_msg.pop("images", None)
                new_messages.append(new_msg)
            payload["messages"] = new_messages
            _log_ollama_payload(payload, kind="chat_raw-retry-no-images")
            if attempt >= attempts:
                attempts = attempt + 1
            continue
        except Exception as e:
            last_exc = e
            if _is_retryable_http_error(e):
                log.warning(
                    "ollama raw chat failed retryable error attempt=%s/%s: %r",
                    attempt,
                    attempts,
                    e,
                )
                if _should_reset_ollama_session_for_error(e):
                    await _reset_ollama_session()
            if attempt >= attempts:
                break
            await asyncio.sleep(_retry_sleep_seconds(e, attempt))

    assert last_exc is not None
    raise last_exc
