"""LLM（Ollama）へのAPIリクエスト送信、Vision対応、タイピングインジケーターの制御を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import re
from typing import Optional, Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..ollama_chat_helpers import *

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _OllamaChatModelCallBase = OllamaChatProtocol
else:
    _OllamaChatModelCallBase = object


class OllamaChatModelCallMixin(_OllamaChatModelCallBase):
        async def _call_model(
            self,
            prompt: str,
            *,
            think: bool | None = None,
            images: Optional[list[str]] = None,
            system_prompt: Optional[str] = None,
            num_predict: Optional[int] = None,
            timeout_sec: Optional[float] = None,
            retries: int = 1,
        ) -> str:
            model_name = str(cfg("OLLAMA_MODEL", "") or "").strip()

            # think 引数が明示されていない場合は設定値から読む
            # OLLAMA_MAIN_MODEL_THINK=true にすると思考対応モデルに正式な思考モードで
            # <think>...</think> を完結させてから本文を出力させられる。
            # call_ollama 側でストリップして本文だけ返す。
            if think is None:
                think = str(cfg("OLLAMA_MAIN_MODEL_THINK", "false") or "false").strip().lower() in (
                    "1", "true", "yes", "on"
                )

            if images:
                vision_model = str(cfg("OLLAMA_VISION_MODEL", "") or "").strip()
                if vision_model:
                    model_name = vision_model
                else:
                    log.warning(
                        "images were passed to _call_model but OLLAMA_VISION_MODEL is not configured; "
                        "falling back to base model=%s without images",
                        model_name,
                    )
                    images = None

            async with self.semaphore:
                return await call_ollama(
                    prompt,
                    system_prompt=self._build_dynamic_system_prompt(system_prompt),
                    think=think,
                    model=model_name,
                    images=images or None,
                    temperature=float(cfg("OLLAMA_REPLY_TEMPERATURE", 0.4) or 0.4),
                    top_p=cfg_float("OLLAMA_REPLY_TOP_P", 0.8),
                    top_k=cfg_int("OLLAMA_REPLY_TOP_K", 20),
                    min_p=cfg_float("OLLAMA_REPLY_MIN_P", 0.0),
                    presence_penalty=cfg_float("OLLAMA_REPLY_PRESENCE_PENALTY", 1.5),
                    repeat_penalty=cfg_float("OLLAMA_REPLY_REPEAT_PENALTY", 1.0),
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                    retries=retries,
                )

        async def _call_chat_model(
            self,
            messages: list[dict[str, Any]],
            *,
            think: bool | None = None,
            tools: list[dict[str, Any]] | None = None,
            num_predict: int | None = None,
            timeout_sec: float | None = None,
        ) -> OllamaChatResult:
            model_name = str(cfg("OLLAMA_MODEL", "") or "").strip()

            if think is None:
                think = str(cfg("OLLAMA_MAIN_MODEL_THINK", "false") or "false").strip().lower() in (
                    "1", "true", "yes", "on"
                )

            async with self.semaphore:
                return await call_ollama_chat_raw(
                    messages,
                    model=model_name,
                    think=bool(think),
                    tools=tools,
                    temperature=float(cfg("OLLAMA_REPLY_TEMPERATURE", 0.4) or 0.4),
                    top_p=cfg_float("OLLAMA_REPLY_TOP_P", 0.8),
                    top_k=cfg_int("OLLAMA_REPLY_TOP_K", 20),
                    min_p=cfg_float("OLLAMA_REPLY_MIN_P", 0.0),
                    presence_penalty=cfg_float("OLLAMA_REPLY_PRESENCE_PENALTY", 1.5),
                    repeat_penalty=cfg_float("OLLAMA_REPLY_REPEAT_PENALTY", 1.0),
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                )

        def _is_typing_indicator_error(self, exc: Exception) -> bool:
            if isinstance(exc, (discord.HTTPException, OSError, asyncio.TimeoutError)):
                return True
            return type(exc).__module__.startswith("aiohttp")

        def _should_use_vision_for_images(self, images: Optional[list[str]]) -> bool:
            if not images:
                return False

            vision_model = str(cfg("OLLAMA_VISION_MODEL", "") or "").strip()
            if not vision_model:
                return False
            return True

        def _sanitize_images_for_request(self, images: Optional[list[str]]) -> Optional[list[str]]:
            if not images:
                return None
            if self._should_use_vision_for_images(images):
                return images
            log.warning(
                "images were attached but no usable vision model is configured; falling back to text-only handling for this request"
            )
            return None

        async def _call_with_typing(
            self,
            message: discord.Message,
            prompt: str,
            *,
            think: bool | None = None,
            images: Optional[list[str]] = None,
            system_prompt: Optional[str] = None,
            num_predict: Optional[int] = None,
            timeout_sec: Optional[float] = None,
            retries: int = 1,
        ) -> str:
            safe_images = self._sanitize_images_for_request(images)
            typing_cm = None

            try:
                typing_cm = message.channel.typing()
            except Exception as e:
                if not self._is_typing_indicator_error(e):
                    raise
                log.warning(
                    "typing indicator setup failed; continuing without typing channel=%s message=%s err=%r",
                    channel_id(message),
                    getattr(message, "id", None),
                    e,
                )

            if typing_cm is not None:
                try:
                    await typing_cm.__aenter__()
                except Exception as e:
                    if not self._is_typing_indicator_error(e):
                        raise
                    log.warning(
                        "typing indicator failed; continuing without typing channel=%s message=%s err=%r",
                        channel_id(message),
                        getattr(message, "id", None),
                        e,
                    )
                    typing_cm = None

            if typing_cm is None:
                return await self._call_model(
                    prompt,
                    think=think,
                    images=safe_images,
                    system_prompt=system_prompt,
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                    retries=retries,
                )

            model_exc: BaseException | None = None
            try:
                return await self._call_model(
                    prompt,
                    think=think,
                    images=safe_images,
                    system_prompt=system_prompt,
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                    retries=retries,
                )
            except BaseException as e:
                model_exc = e
                raise
            finally:
                with contextlib.suppress(Exception):
                    await typing_cm.__aexit__(
                        type(model_exc) if model_exc is not None else None,
                        model_exc,
                        model_exc.__traceback__ if model_exc is not None else None,
                    )

        async def _call_chat_with_typing(
            self,
            message: discord.Message,
            messages: list[dict[str, Any]],
            *,
            think: bool | None = None,
            tools: list[dict[str, Any]] | None = None,
            num_predict: int | None = None,
            timeout_sec: float | None = None,
        ) -> OllamaChatResult:
            typing_cm = None
            try:
                typing_cm = message.channel.typing()
            except Exception as e:
                if not self._is_typing_indicator_error(e):
                    raise
                log.warning("typing indicator setup failed; continuing without typing channel=%s message=%s err=%r", channel_id(message), getattr(message, "id", None), e)

            if typing_cm is not None:
                try:
                    await typing_cm.__aenter__()
                except Exception as e:
                    if not self._is_typing_indicator_error(e):
                        raise
                    log.warning("typing indicator failed; continuing without typing channel=%s message=%s err=%r", channel_id(message), getattr(message, "id", None), e)
                    typing_cm = None

            if typing_cm is None:
                return await self._call_chat_model(
                    messages,
                    think=think,
                    tools=tools,
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                )

            model_exc: BaseException | None = None
            try:
                return await self._call_chat_model(
                    messages,
                    think=think,
                    tools=tools,
                    num_predict=num_predict,
                    timeout_sec=timeout_sec,
                )
            except BaseException as e:
                model_exc = e
                raise
            finally:
                with contextlib.suppress(Exception):
                    await typing_cm.__aexit__(
                        type(model_exc) if model_exc is not None else None,
                        model_exc,
                        model_exc.__traceback__ if model_exc is not None else None,
                    )

