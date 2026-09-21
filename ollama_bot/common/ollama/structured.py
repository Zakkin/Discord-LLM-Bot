from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..json_compat import JSONDecodeError, loads as json_loads
from .client import (
    OllamaImageNotSupportedError,
    OllamaJSONInvalidError,
    OllamaJSONTruncatedError,
    _build_options,
    _bump_num_predict,
    _is_bad_request_http_error,
    _is_retryable_http_error,
    _merge_generation_kwargs_into_options,
    _reset_ollama_session,
    _resolve_keep_alive,
    _retry_sleep_seconds,
    _should_reset_ollama_session_for_error,
    _should_send_think_flag,
)
from .chat import _post_chat_once
from .text import _log_ollama_payload

log = logging.getLogger("ollama_bot.common.ollama.structured")


def _looks_like_truncated_json_text(text: str) -> bool:
    s = str(text or "").rstrip()
    if not s:
        return False
    if s.endswith(("}", "]")):
        return False
    return True


def _json_schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool))
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "null":
        return value is None
    return True


def _validate_json_schema_subset(parsed: dict[str, Any], schema: dict[str, Any] | None, response_text: str) -> None:
    if not isinstance(schema, dict):
        return
    if schema.get("type") not in (None, "object"):
        return

    required = [str(key) for key in list(schema.get("required") or [])]
    missing = [key for key in required if key not in parsed]
    if missing:
        raise OllamaJSONInvalidError(
            "Ollama returned JSON that does not match schema "
            f"(missing required keys: {', '.join(missing)}): {response_text}"
        )

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for key, property_schema in properties.items():
        if key not in parsed or not isinstance(property_schema, dict):
            continue
        value = parsed[key]
        expected = property_schema.get("type")
        expected_raw = expected if isinstance(expected, list) else [expected]
        expected_types: list[str] = [str(item) for item in expected_raw if item is not None]
        if expected_types and not any(_json_schema_type_matches(value, item) for item in expected_types):
            raise OllamaJSONInvalidError(
                "Ollama returned JSON that does not match schema "
                f"(key {key!r} expected {'/'.join(expected_types)}, got {type(value).__name__}): {response_text}"
            )

        enum_values = property_schema.get("enum")
        if isinstance(enum_values, list) and enum_values and value not in enum_values:
            raise OllamaJSONInvalidError(
                "Ollama returned JSON that does not match schema "
                f"(key {key!r} value {value!r} not in enum): {response_text}"
            )

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = property_schema.get("minimum")
            if isinstance(minimum, (int, float)) and value < minimum:
                raise OllamaJSONInvalidError(
                    "Ollama returned JSON that does not match schema "
                    f"(key {key!r} expected >= {minimum}, got {value!r}): {response_text}"
                )
            maximum = property_schema.get("maximum")
            if isinstance(maximum, (int, float)) and value > maximum:
                raise OllamaJSONInvalidError(
                    "Ollama returned JSON that does not match schema "
                    f"(key {key!r} expected <= {maximum}, got {value!r}): {response_text}"
                )

        if isinstance(value, list) and isinstance(property_schema.get("items"), dict):
            min_items = property_schema.get("minItems")
            if isinstance(min_items, int) and len(value) < min_items:
                raise OllamaJSONInvalidError(
                    "Ollama returned JSON that does not match schema "
                    f"(key {key!r} expected at least {min_items} items, got {len(value)}): {response_text}"
                )
            max_items = property_schema.get("maxItems")
            if isinstance(max_items, int) and len(value) > max_items:
                raise OllamaJSONInvalidError(
                    "Ollama returned JSON that does not match schema "
                    f"(key {key!r} expected at most {max_items} items, got {len(value)}): {response_text}"
                )
            item_type = property_schema["items"].get("type")
            if item_type:
                for item in value:
                    if not _json_schema_type_matches(item, str(item_type)):
                        raise OllamaJSONInvalidError(
                            "Ollama returned JSON that does not match schema "
                            f"(key {key!r} contains invalid item type {type(item).__name__}): {response_text}"
                        )


async def call_ollama_json(
    user_prompt_or_client: Any,
    *,
    system_prompt: str,
    user_prompt: str | None = None,
    model: str,
    think: bool = False,
    options: dict | None = None,
    schema: dict[str, Any] | None = None,
    timeout_sec: float | None = None,
    retries: int = 0,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    repeat_penalty: float | None = None,
    num_predict: int | None = None,
) -> dict[str, Any]:
    merged_options = _merge_generation_kwargs_into_options(
        options,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
        presence_penalty=presence_penalty,
        repeat_penalty=repeat_penalty,
        num_predict=num_predict,
    )

    if user_prompt is None and isinstance(user_prompt_or_client, str):
        prompt_text = user_prompt_or_client
        payload: dict[str, Any] = {
            "model": str(model).strip(),
            "messages": [
                {"role": "system", "content": str(system_prompt)},
                {"role": "user", "content": prompt_text},
            ],
            "stream": False,
            "format": schema or "json",
            "options": _build_options(think=think, options=merged_options),
        }
        if _should_send_think_flag():
            payload["think"] = bool(think)
        keep_alive = _resolve_keep_alive(str(model).strip())
        if keep_alive not in (None, ""):
            payload["keep_alive"] = keep_alive

        _log_ollama_payload(payload, kind="json")

        attempts = max(int(retries or 0), 0) + 1
        last_exc: Exception | None = None
        response_text = ""
        done_reason = None
        truncated = False
        current_num_predict = num_predict

        current_attempt = 1
        while current_attempt <= attempts:
            attempt = current_attempt
            current_attempt += 1
            try:
                result = await _post_chat_once(payload=payload, timeout_sec=timeout_sec)
                response_text = result.content
                done_reason = result.done_reason
                truncated = result.truncated

                log.info(
                    "ollama json response think=%s done_reason=%s truncated=%s content=\n%s",
                    think,
                    done_reason,
                    truncated,
                    response_text,
                )

                # 1. length / truncated によるトークン不足のリトライ
                if (truncated or done_reason == "length") and attempt < attempts:
                    retry_options = _build_options(
                        think=think,
                        options=_bump_num_predict(
                            payload.get("options", {}),
                            current_num_predict=current_num_predict,
                        ),
                    )
                    log.info(
                        "ollama json truncated on %s attempt; retrying with larger num_predict "
                        "model=%s num_predict=%s retry_num_predict=%s content=\n%s",
                        "first" if attempt == 1 else f"attempt={attempt}/{attempts}",
                        model,
                        payload.get("options", {}).get("num_predict"),
                        retry_options.get("num_predict"),
                        response_text,
                    )
                    current_num_predict = retry_options.get("num_predict")
                    payload = dict(payload)
                    payload["options"] = retry_options
                    _log_ollama_payload(payload, kind="json-retry-length")
                    continue

                # 2. JSON パース & スキーマ検証
                try:
                    parsed = json_loads(response_text)
                    if not isinstance(parsed, dict):
                        raise OllamaJSONInvalidError(
                            "Ollama returned JSON of unexpected type "
                            f"(expected object, got {type(parsed).__name__}): {response_text}"
                        )
                    _validate_json_schema_subset(parsed, schema, response_text)
                    return parsed
                except (JSONDecodeError, OllamaJSONInvalidError) as parse_err:
                    if attempt < attempts:
                        if isinstance(schema, dict) and payload.get("format") != "json":
                            log.warning(
                                "ollama json response was invalid/truncated with schema format on attempt=%s/%s; "
                                "retrying with plain JSON format model=%s content=\n%s",
                                attempt,
                                attempts,
                                model,
                                response_text,
                            )
                            payload = dict(payload)
                            payload["format"] = "json"
                            _log_ollama_payload(payload, kind="json-retry-plain-format")
                            continue
                        elif _looks_like_truncated_json_text(response_text):
                            retry_options = _build_options(
                                think=think,
                                options=_bump_num_predict(
                                    payload.get("options", {}),
                                    current_num_predict=current_num_predict,
                                ),
                            )
                            log.warning(
                                "ollama json response looks truncated on attempt=%s/%s; "
                                "retrying with larger num_predict model=%s content=\n%s",
                                attempt,
                                attempts,
                                model,
                                response_text,
                            )
                            current_num_predict = retry_options.get("num_predict")
                            payload = dict(payload)
                            payload["options"] = retry_options
                            _log_ollama_payload(payload, kind="json-retry-length")
                            continue
                        else:
                            log.warning(
                                "ollama json response parse failed on attempt=%s/%s model=%s: %r; retrying",
                                attempt,
                                attempts,
                                model,
                                parse_err,
                            )
                            await asyncio.sleep(0.5)
                            continue

                    if _looks_like_truncated_json_text(response_text) or truncated or done_reason == "length":
                        raise OllamaJSONTruncatedError(
                            "Ollama returned apparently truncated JSON "
                            f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
                        ) from parse_err
                    if isinstance(parse_err, OllamaJSONInvalidError):
                        raise parse_err
                    raise OllamaJSONInvalidError(
                        "Ollama returned invalid JSON "
                        f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
                    ) from parse_err

            except OllamaImageNotSupportedError as e:
                last_exc = e
                log.warning("ollama json model=%s rejected images; retrying without images on attempt=%s/%s", model, attempt, attempts)
                payload = dict(payload)
                new_messages = []
                for msg in payload.get("messages", []):
                    new_msg = dict(msg)
                    new_msg.pop("images", None)
                    new_messages.append(new_msg)
                payload["messages"] = new_messages
                _log_ollama_payload(payload, kind="json-retry-no-images")
                if attempt >= attempts:
                    attempts = attempt + 1
                continue
            except Exception as e:
                last_exc = e
                if _is_bad_request_http_error(e) and isinstance(schema, dict) and payload.get("format") != "json":
                    log.warning(
                        "ollama json schema request was rejected with HTTP 400; retrying once with plain JSON format model=%s",
                        model,
                    )
                    payload = dict(payload)
                    payload["format"] = "json"
                    _log_ollama_payload(payload, kind="json-retry-plain-format")
                    if attempt >= attempts:
                        attempts = attempt + 1
                    continue
                if _is_retryable_http_error(e):
                    log.warning(
                        "ollama json request failed with retryable error on attempt=%s/%s model=%s: %r",
                        attempt,
                        attempts,
                        model,
                        e,
                    )
                    if _should_reset_ollama_session_for_error(e):
                        await _reset_ollama_session()
                if attempt >= attempts:
                    raise
                await asyncio.sleep(_retry_sleep_seconds(e, attempt))

        if last_exc:
            raise last_exc
        raise OllamaJSONInvalidError(f"Ollama returned empty response: {response_text}")

    else:
        client = user_prompt_or_client
        prompt_text = str(user_prompt or "")
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_text},
            ],
            format=schema or "json",
            options=merged_options,
            stream=False,
        )
        message = response.get("message", {}) or {}
        response_text = str(message.get("content", "") or "")
        done_reason = response.get("done_reason")
        truncated = bool(response.get("truncated", False))

        log.info(
            "ollama json response think=%s done_reason=%s truncated=%s content=\n%s",
            think,
            done_reason,
            truncated,
            response_text,
        )

        if (truncated or done_reason == "length") and _looks_like_truncated_json_text(response_text):
            raise OllamaJSONTruncatedError(
                "Ollama returned truncated JSON "
                f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
            )

        try:
            parsed = json_loads(response_text)
        except JSONDecodeError as e:
            if _looks_like_truncated_json_text(response_text):
                raise OllamaJSONTruncatedError(
                    "Ollama returned apparently truncated JSON "
                    f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
                ) from e
            if truncated or done_reason == "length":
                raise OllamaJSONTruncatedError(
                    "Ollama returned truncated JSON "
                    f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
                ) from e
            raise OllamaJSONInvalidError(
                "Ollama returned invalid JSON "
                f"(done_reason={done_reason}, truncated={truncated}): {response_text}"
            ) from e

        if not isinstance(parsed, dict):
            raise OllamaJSONInvalidError(
                "Ollama returned JSON of unexpected type "
                f"(expected object, got {type(parsed).__name__}): {response_text}"
            )
        _validate_json_schema_subset(parsed, schema, response_text)
        return parsed
