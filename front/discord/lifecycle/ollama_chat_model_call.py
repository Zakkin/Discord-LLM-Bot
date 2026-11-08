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

from ollama_bot.ollama_chat_.ollama_chat_helpers import *

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
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
            num_ctx: Optional[int] = None,
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
                pipeline_mode = str(cfg("VISION_PIPELINE_MODE", "caption_then_main") or "").strip().lower()
                vision_model = str(cfg("OLLAMA_VISION_MODEL", "") or "").strip()
                if pipeline_mode == "caption_then_main" and not self._is_vision_capable_model(model_name):
                    # 2段階パイプライン運用時: メインモデルが非Visionの場合は画像を切り離してメインモデルを呼び出す
                    log.debug("caption_then_main pipeline active: stripping images for base model=%s", model_name)
                    images = None
                elif vision_model:
                    model_name = vision_model
                elif self._is_vision_capable_model(model_name) or cfg_bool("OLLAMA_MODEL_SUPPORTS_VISION", False):
                    # OLLAMA_VISION_MODEL 未指定でもベースモデル自身が画像認識対応モデルの場合は画像を渡す
                    pass
                else:
                    log.warning(
                        "images were passed to _call_model but OLLAMA_VISION_MODEL is not configured and "
                        "base model=%s is not vision-capable; falling back to base model without images",
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
                    presence_penalty=cfg_float("OLLAMA_REPLY_PRESENCE_PENALTY", 0.2),
                    repeat_penalty=cfg_float("OLLAMA_REPLY_REPEAT_PENALTY", 1.0),
                    num_predict=num_predict,
                    num_ctx=num_ctx,
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
            num_ctx: int | None = None,
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
                    presence_penalty=cfg_float("OLLAMA_REPLY_PRESENCE_PENALTY", 0.2),
                    repeat_penalty=cfg_float("OLLAMA_REPLY_REPEAT_PENALTY", 1.0),
                    num_predict=num_predict,
                    num_ctx=num_ctx,
                    timeout_sec=timeout_sec,
                )

        def _is_typing_indicator_enabled(self) -> bool:
            return bool(
                cfg_bool("DISCORD_TYPING_INDICATOR_ENABLED", False)
                or cfg_bool("TYPING_INDICATOR_ENABLED", False)
            )

        def _is_typing_indicator_error(self, exc: Exception) -> bool:
            if isinstance(exc, (discord.HTTPException, OSError, asyncio.TimeoutError)):
                return True
            return type(exc).__module__.startswith("aiohttp")

        @staticmethod
        def _is_vision_capable_model(model_name: str) -> bool:
            """指定されたモデル名がマルチモーダル（画像認識対応）モデルであるかを判定する。"""
            if not model_name:
                return False
            lowered = model_name.lower()
            vision_keywords = (
                "-vl",
                ":vl",
                "_vl",
                "vision",
                "llava",
                "minicpm-v",
                "moondream",
                "bakllava",
                "cogvlm",
            )
            return any(kw in lowered for kw in vision_keywords)

        def _should_use_vision_for_images(self, images: Optional[list[str]]) -> bool:
            if not images:
                return False

            vision_model = str(cfg("OLLAMA_VISION_MODEL", "") or "").strip()
            if vision_model:
                return True

            main_model = str(cfg("OLLAMA_MODEL", "") or "").strip()
            if cfg_bool("OLLAMA_MODEL_SUPPORTS_VISION", False) or self._is_vision_capable_model(main_model):
                return True

            return False

        def _sanitize_images_for_request(self, images: Optional[list[str]]) -> Optional[list[str]]:
            if not images:
                return None
            if self._should_use_vision_for_images(images):
                return images
            main_model = str(cfg("OLLAMA_MODEL", "") or "").strip()
            log.warning(
                "images were attached but neither OLLAMA_VISION_MODEL nor a vision-capable base model (%s) is configured; "
                "falling back to text-only handling for this request",
                main_model or "unset",
            )
            return None

        def _is_user_reply_busy(self) -> bool:
            """Discordのユーザー返信が推論中、前処理（調査・分析）実行中、または返信キューに待機中かを判定する。"""
            if getattr(self, "_active_user_reply_count", 0) > 0:
                return True
            if getattr(self, "_active_user_reply_turn_count", 0) > 0:
                return True
            q = getattr(self, "_reply_queue", None)
            if q is not None and hasattr(q, "qsize"):
                try:
                    if int(q.qsize()) > 0:
                        return True
                except Exception:
                    pass
            return False

        def _refresh_user_reply_done_event(self) -> None:
            evt = getattr(self, "_user_reply_done_event", None)
            if evt is None:
                return
            if self._is_user_reply_busy():
                evt.clear()
            else:
                evt.set()

        def _get_user_reply_done_event(self) -> asyncio.Event:
            evt = getattr(self, "_user_reply_done_event", None)
            if evt is None:
                evt = asyncio.Event()
                if not self._is_user_reply_busy():
                    evt.set()
                setattr(self, "_user_reply_done_event", evt)
            return evt

        async def _yield_to_user_replies(self, timeout_sec: float = 30.0) -> None:
            """バックグラウンドLLMタスクがユーザー返信と競合しないよう、返信生成中（_active_user_reply_count > 0）は待機して譲歩する。

            ユーザー返信生成（70秒超等）の最中に timeout_sec を迎過しても強行実行せず、
            返信が完了するまで（または絶対上限まで）安全に待機を延長する。
            """
            start_ts = time.time()
            last_logged_ts = start_ts
            max_wait_sec = max(timeout_sec, cfg_float("LLM_YIELD_MAX_WAIT_SEC", 300.0))

            while self._is_user_reply_busy():
                elapsed = time.time() - start_ts
                if elapsed >= max_wait_sec:
                    log.warning(
                        "[llm-yield] user reply active wait reached maximum limit (%.1fs, count=%s, turn_count=%s); continuing background task to prevent deadlock",
                        max_wait_sec,
                        getattr(self, "_active_user_reply_count", 0),
                        getattr(self, "_active_user_reply_turn_count", 0),
                    )
                    break

                if elapsed >= timeout_sec and time.time() - last_logged_ts >= 15.0:
                    last_logged_ts = time.time()
                    log.info(
                        "[llm-yield] user reply still active (count=%s, turn_count=%s, elapsed=%.1fs, max=%.1fs); safely extending wait...",
                        getattr(self, "_active_user_reply_count", 0),
                        getattr(self, "_active_user_reply_turn_count", 0),
                        elapsed,
                        max_wait_sec,
                    )

                event = getattr(self, "_user_reply_done_event", None)
                # event が未セットの場合のみ待機。すでにセットされているか存在しない場合は必ず sleep してビジーループを防ぐ
                if event is not None and not event.is_set():
                    try:
                        remaining = max(0.1, min(0.5, max_wait_sec - elapsed))
                        await asyncio.wait_for(event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
                else:
                    # イベントループの他タスク（Discord Heartbeat等）へ確実に制御を譲る
                    await asyncio.sleep(0.2)

        async def _call_with_typing(
            self,
            message: discord.Message,
            prompt: str,
            *,
            think: bool | None = None,
            images: Optional[list[str]] = None,
            system_prompt: Optional[str] = None,
            num_predict: Optional[int] = None,
            num_ctx: Optional[int] = None,
            timeout_sec: Optional[float] = None,
            retries: int = 1,
        ) -> str:
            self._active_user_reply_count = getattr(self, "_active_user_reply_count", 0) + 1
            reply_evt = getattr(self, "_user_reply_done_event", None)
            if reply_evt is not None:
                reply_evt.clear()
            try:
                safe_images = self._sanitize_images_for_request(images)
                if not self._is_typing_indicator_enabled():
                    return await self._call_model(
                        prompt,
                        think=think,
                        images=safe_images,
                        system_prompt=system_prompt,
                        num_predict=num_predict,
                        num_ctx=num_ctx,
                        timeout_sec=timeout_sec,
                        retries=retries,
                    )

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
                        num_ctx=num_ctx,
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
                        num_ctx=num_ctx,
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
            finally:
                new_count = max(0, getattr(self, "_active_user_reply_count", 1) - 1)
                self._active_user_reply_count = new_count
                self._refresh_user_reply_done_event()

        async def _call_chat_with_typing(
            self,
            message: discord.Message,
            messages: list[dict[str, Any]],
            *,
            think: bool | None = None,
            tools: list[dict[str, Any]] | None = None,
            num_predict: int | None = None,
            num_ctx: int | None = None,
            timeout_sec: float | None = None,
        ) -> OllamaChatResult:
            self._active_user_reply_count = getattr(self, "_active_user_reply_count", 0) + 1
            chat_evt = getattr(self, "_user_reply_done_event", None)
            if chat_evt is not None:
                chat_evt.clear()
            try:
                if not self._is_typing_indicator_enabled():
                    return await self._call_chat_model(
                        messages,
                        think=think,
                        tools=tools,
                        num_predict=num_predict,
                        num_ctx=num_ctx,
                        timeout_sec=timeout_sec,
                    )

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
                        num_ctx=num_ctx,
                        timeout_sec=timeout_sec,
                    )

                model_exc: BaseException | None = None
                try:
                    return await self._call_chat_model(
                        messages,
                        think=think,
                        tools=tools,
                        num_predict=num_predict,
                        num_ctx=num_ctx,
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
            finally:
                new_count = max(0, getattr(self, "_active_user_reply_count", 1) - 1)
                self._active_user_reply_count = new_count
                self._refresh_user_reply_done_event()

