"""
Ollama JSON構造化出力APIおよび検証ユーティリティ。
Structured JSON output helper and validation utilities for Ollama.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from harness.agent.prompt_parser.parser.structured import (
    OllamaImageNotSupportedError,
    OllamaJSONInvalidError,
    OllamaJSONTruncatedError,
    _json_schema_type_matches,
    _looks_like_truncated_json_text,
    _resolve_post_chat_fn,
    _validate_json_schema_subset,
    call_ollama_json as _harness_call_ollama_json,
)
from .text import format_ollama_metrics, log_ollama_metrics

log = logging.getLogger("ollama_bot.common.ollama_helpers.structured")


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
    num_ctx: int | None = None,
    salvage_comment_on_truncation: bool = False,
) -> dict[str, Any]:
    """JSON形式の出力をOllamaから取得し検証する。タイムアウト発生時は詳細ログを出力する。"""
    timeout_val = float(timeout_sec) if timeout_sec is not None else 0.0
    model_str = str(model).strip()
    call_kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "model": model,
        "think": think,
        "options": options,
        "schema": schema,
        "timeout_sec": timeout_sec,
        "retries": retries,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "presence_penalty": presence_penalty,
        "repeat_penalty": repeat_penalty,
        "num_predict": num_predict,
        "num_ctx": num_ctx,
    }
    if salvage_comment_on_truncation:
        call_kwargs["salvage_comment_on_truncation"] = True

    try:
        try:
            return await _harness_call_ollama_json(
                user_prompt_or_client,
                **call_kwargs,
            )
        except TypeError as te:
            # harness 側がまだ salvage_comment_on_truncation 引数を未実装な過渡状態でも落とさない
            if "salvage_comment_on_truncation" in str(te) and "salvage_comment_on_truncation" in call_kwargs:
                call_kwargs.pop("salvage_comment_on_truncation", None)
                return await _harness_call_ollama_json(
                    user_prompt_or_client,
                    **call_kwargs,
                )
            raise
    except (asyncio.TimeoutError, TimeoutError) as e:
        log.warning(
            "[prompt-manager] call_ollama_json timed out after %.1fs for model=%s: %r",
            timeout_val,
            model_str,
            e,
        )
        raise


__all__ = [
    "OllamaJSONTruncatedError",
    "OllamaJSONInvalidError",
    "OllamaImageNotSupportedError",
    "_validate_json_schema_subset",
    "_looks_like_truncated_json_text",
    "_json_schema_type_matches",
    "_resolve_post_chat_fn",
    "call_ollama_json",
    "format_ollama_metrics",
    "log_ollama_metrics",
]
