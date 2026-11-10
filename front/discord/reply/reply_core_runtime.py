"""OllamaChatReplyCoreMixinのMessageRuntime構築・時刻/天気即時応答・発言記録を担当するMixin。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Sequence, TYPE_CHECKING

import discord

from front.discord.common.bot_identity import resolve_bot_identity
from front.discord.common.discord_helpers import (
    append_context_message,
    collect_same_author_burst_texts,
    format_context_lines_with_relation_labels,
    looks_like_incomplete_topic_opener,
)
from harness.agent.tool_registry.singing_helpers import compact_singing_context_lines
from harness.agent.tool_registry.weather_helpers import (
    build_weather_followup_reply,
    extract_recent_weather_fact_from_context,
    extract_weather_place,
)
from ollama_bot.common.ollama_helpers import (
    looks_like_abnormal_assistant_reply,
    truncate_lines,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    _build_reply_context_user_text,
    _reset_other_channel_message_count,
    author_id,
    channel_id,
    content,
    display_name,
    extract_weather_place_from_context,
    fetch_weather_summary,
    is_explicit_weather_request,
    looks_like_weather_query,
    prune_stale_context_cache,
    recent_context_items,
)
from ollama_bot.ollama_chat_.ollama_chat_types import MediaBundle, MessageRuntime
from front.discord.reply.classifier import _calculate_elapsed_sec, _calculate_human_gap
from harness.agent.prompt_parser.parser.reply_helpers import (
    _build_weather_comment,
    _merge_user_text_with_media_analysis,
    build_current_time_reply,
    looks_like_time_query,
)
from front.discord.reply.reply_core_common import (
    _OllamaChatReplyCoreBase,
    _build_summary_context_lines,
    build_message_media_context,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_managed_channel_ids,
    extract_and_store_memory,
    extract_base64_images,
    fetch_recent_context_lines,
    log,
    maybe_update_channel_summary,
    report_error,
)


class ReplyRuntimeMixin(_OllamaChatReplyCoreBase):
    """MessageRuntimeの構築、コンテキスト集約、時刻・天気の即時応答、発言履歴追跡を担当するMixin。"""

    def _resolve_author_display_name(self, message: Any) -> str:
        if message is None:
            return ""
        disp = display_name(message)
        if disp:
            return str(disp).strip()
        author = getattr(message, "author", None)
        if author is not None:
            return str(getattr(author, "display_name", None) or getattr(author, "name", "") or "").strip()
        return ""

    def _get_channel_context_queue(self, message: discord.Message | None) -> Sequence[Any] | None:
        if message is None:
            return None
        cid = channel_id(message)
        cache = getattr(self, "channel_context_cache", None)
        return cache.get(cid) if cache and cid is not None else None

    def _calculate_message_elapsed_sec(self, message: discord.Message) -> float:
        return _calculate_elapsed_sec(message)

    def _calculate_channel_gap(self, message: discord.Message) -> int:
        queue = self._get_channel_context_queue(message)
        bot_user_id = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        return _calculate_human_gap(message, queue, bot_user_id=bot_user_id)

    def _append_current_user_message(self, runtime: MessageRuntime, message: discord.Message) -> None:
        if runtime.cid is not None:
            append_context_message(self.channel_context_cache, message, bot_user_id=runtime.bot_user_id)
            if hasattr(self, "thread_tracker") and self.thread_tracker:
                self.thread_tracker.record_message(
                    message,
                    bot_user_id=runtime.bot_user_id,
                    bot_identity=resolve_bot_identity(bot=self.bot, message=message),
                )

    def _append_sent_message(self, sent: discord.Message | None) -> None:
        if sent is None:
            return
        sent_text = content(sent)
        if looks_like_abnormal_assistant_reply(sent_text):
            return
        bot_uid = self.bot.user.id if self.bot.user else None
        append_context_message(self.channel_context_cache, sent, bot_user_id=bot_uid)
        if hasattr(self, "thread_tracker") and self.thread_tracker:
            self.thread_tracker.record_message(
                sent,
                bot_user_id=bot_uid,
                bot_identity=resolve_bot_identity(bot=self.bot, message=sent),
            )
        cid = channel_id(sent)
        if cid is not None:
            _reset_other_channel_message_count(self, cid)

    async def _build_runtime(self, message: discord.Message) -> MessageRuntime:
        cid = channel_id(message)
        bot_user_id = self.bot.user.id if self.bot.user else None
        if cid is not None:
            prune_stale_context_cache(
                self.channel_context_cache,
                cid,
                before_message=message,
                max_age_sec=self._context_max_age_sec(),
            )
        cached_items = recent_context_items(
            self.channel_context_cache,
            cid,
            before_message=message,
            max_age_sec=self._context_max_age_sec(),
        ) if cid is not None else []

        recent_general = cached_items[-10:]
        recent_author = [item for item in cached_items if item.get("author_id") == author_id(message)][-5:]

        # id() を使って O(1) set ルックアップで和集合を保持（挿入順は cached_items の順序通り）
        include_ids = {id(item) for item in recent_general} | {id(item) for item in recent_author}
        merged_items = [item for item in cached_items if id(item) in include_ids]

        context_lines = [str(item.get("line", "")) for item in merged_items if item.get("line")]
        prompt_fetch_limit = max(int(cfg("CONTEXT_WINDOW_MESSAGES", 7) or 7), 1)
        if len(context_lines) < prompt_fetch_limit:
            try:
                fetched_context_lines = await fetch_recent_context_lines(
                    message,
                    limit=prompt_fetch_limit,
                    bot_user_id=bot_user_id,
                    max_age_sec=self._context_max_age_sec(),
                )
                if fetched_context_lines:
                    seen_prompt_lines: set[str] = set()
                    merged_prompt_lines: list[str] = []
                    for line in list(fetched_context_lines) + context_lines:
                        normalized_line = str(line or "").strip()
                        if not normalized_line or normalized_line in seen_prompt_lines:
                            continue
                        seen_prompt_lines.add(normalized_line)
                        merged_prompt_lines.append(normalized_line)
                    context_lines = merged_prompt_lines
            except Exception as e:
                log.debug("prompt context history fetch failed: %r", e)

        context_lines = compact_singing_context_lines(context_lines)
        context_lines = format_context_lines_with_relation_labels(context_lines)
        context_lines = truncate_lines(
            context_lines,
            max_lines=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 15) or 15),
            max_chars_per_line=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
            max_total_chars=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 1800) or 1800),
        )
        memory_context_source_lines = [str(item.get("line", "")) for item in cached_items if item.get("line")]
        memory_fetch_limit = max(
            int(cfg("MEMORY_LONG_CONTEXT_LINES", 30) or 30),
            int(cfg("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20),
        )
        if len(memory_context_source_lines) < memory_fetch_limit:
            try:
                fetched_context_lines = await fetch_recent_context_lines(
                    message,
                    limit=memory_fetch_limit,
                    bot_user_id=bot_user_id,
                    max_age_sec=self._context_max_age_sec(),
                )
                if fetched_context_lines:
                    seen_context_lines: set[str] = set()
                    merged_memory_lines: list[str] = []
                    for line in list(fetched_context_lines) + memory_context_source_lines:
                        normalized_line = str(line or "").strip()
                        if not normalized_line or normalized_line in seen_context_lines:
                            continue
                        seen_context_lines.add(normalized_line)
                        merged_memory_lines.append(normalized_line)
                    memory_context_source_lines = merged_memory_lines
            except Exception as e:
                log.debug("memory context history fetch failed: %r", e)
        memory_context_lines = self._trim_memory_context_lines(compact_singing_context_lines(memory_context_source_lines))
        original_user_text = content(message)
        if cid is not None and looks_like_incomplete_topic_opener(original_user_text):
            burst_texts = collect_same_author_burst_texts(
                getattr(self, "channel_context_cache", None),
                cid,
                message,
            )
            if not burst_texts:
                wait_sec = max(0.0, float(cfg("BURST_OPENER_WAIT_SEC", 0.0) or 0.0))
                if wait_sec > 0.0:
                    await asyncio.sleep(min(wait_sec, 2.5))
                    burst_texts = collect_same_author_burst_texts(
                        getattr(self, "channel_context_cache", None),
                        cid,
                        message,
                    )
            if burst_texts:
                original_user_text = "\n".join([original_user_text, *burst_texts])
                log.info(
                    "TRACE coalesced same-author burst opener: cid=%s author=%s count=%d text=%r",
                    cid,
                    author_id(message),
                    len(burst_texts),
                    original_user_text[:120],
                )
        effective_user_text = await _build_reply_context_user_text(
            message,
            original_user_text,
            bot_user_id=bot_user_id,
        )
        base64_images = await extract_base64_images(message)
        media_context = await build_message_media_context(message)
        use_caption = bool(media_context.get("image_descriptions"))
        media = MediaBundle(
            images=[] if use_caption else (base64_images or list(media_context.get("images") or [])),
            prompt_parts=list(media_context.get("prompt_parts") or []),
            analysis_text=str(media_context.get("analysis_text") or "").strip(),
        )
        return MessageRuntime(
            cid=cid,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
            original_user_text=original_user_text,
            effective_user_text=_merge_user_text_with_media_analysis(effective_user_text, media.analysis_text),
            context_lines=context_lines,
            media=media,
            bot_user_id=bot_user_id,
            author_name=display_name(message) or getattr(getattr(message, "author", None), "name", "") or "ユーザー",
            author_display_name=display_name(message) or getattr(getattr(message, "author", None), "name", "") or "ユーザー",
            memory_context_lines=memory_context_lines,
            is_umigame_reply=await self._is_umigame_reply_message(message),
            is_twenty_doors_reply=await self._is_twenty_doors_reply_message(message),
            message=message,
        )

    async def _get_or_build_runtime(self, message: discord.Message) -> MessageRuntime | None:
        runtime = getattr(message, "_ollama_runtime", None)
        if runtime is not None:
            return runtime
        mid = getattr(message, "id", None)
        if isinstance(mid, int):
            cache = getattr(self, "_message_runtime_cache", None)
            if isinstance(cache, dict) and mid in cache:
                return cache[mid]

        runtime = await self._build_runtime(message)
        try:
            setattr(message, "_ollama_runtime", runtime)
        except Exception:
            pass
        if isinstance(mid, int):
            cache = getattr(self, "_message_runtime_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                try:
                    setattr(self, "_message_runtime_cache", cache)
                except Exception:
                    pass
            if isinstance(cache, dict):
                cache[mid] = runtime
                while len(cache) > 256:
                    try:
                        cache.pop(next(iter(cache)), None)
                    except StopIteration:
                        break
        return runtime

    async def _handle_time(self, message: discord.Message, runtime: MessageRuntime, *, is_direct: bool = False) -> bool:
        if not looks_like_time_query(runtime.original_user_text):
            return False
        self._append_current_user_message(runtime, message)
        sent = await self._send_reply(message, build_current_time_reply(runtime.original_user_text), is_direct=is_direct)
        if sent is None:
            return True
        self._append_sent_message(sent)
        return True

    async def _handle_weather(self, message: discord.Message, runtime: MessageRuntime, *, is_direct: bool = False) -> bool:
        if not looks_like_weather_query(runtime.original_user_text):
            return False

        context_lines = runtime.context_lines or await fetch_recent_context_lines(
            message,
            limit=cfg_int("CONTEXT_WINDOW_MESSAGES", 7),
            bot_user_id=runtime.bot_user_id,
        )
        explicit_request = is_explicit_weather_request(runtime.original_user_text)
        recent_weather_fact = await extract_recent_weather_fact_from_context(context_lines)

        if not explicit_request and not recent_weather_fact:
            return False

        runtime.is_weather_reply = True

        if not explicit_request and recent_weather_fact:
            reply = build_weather_followup_reply(runtime.original_user_text, recent_weather_fact)
        else:
            place = extract_weather_place(runtime.original_user_text) or await extract_weather_place_from_context(context_lines)
            if not place:
                reply = cfg("OLLAMA_WEATHER_PLACE_REQUIRED_FALLBACK", "")
            else:
                if "明後日" in runtime.original_user_text:
                    when = "day_after_tomorrow"
                elif "明日" in runtime.original_user_text:
                    when = "tomorrow"
                else:
                    when = "today"
                try:
                    weather_summary = await fetch_weather_summary(place, when=when)
                except Exception as e:
                    log.warning("_handle_weather fetch_weather_summary failed: place=%r error=%s", place, e)
                    weather_summary = None

                if weather_summary:
                    try:
                        weather_comment = await _build_weather_comment(weather_summary)
                    except Exception as e:
                        log.warning("_handle_weather _build_weather_comment failed: %s", e)
                        weather_comment = None
                    reply = f"{weather_summary}\n{weather_comment}" if weather_comment else weather_summary
                else:
                    reply = cfg("OLLAMA_UNKNOWN_FACT_FALLBACK", "")

        self._append_current_user_message(runtime, message)
        sent = await self._send_reply(message, reply, is_direct=is_direct)
        if sent is None:
            return True
        self._append_sent_message(sent)

        weather_emotion_scores, _, weather_emotion_reason = await self._get_emotion_snapshot()
        user_name = getattr(getattr(message, "author", None), "name", None)
        user_display_name = self._resolve_author_display_name(message) or user_name

        async def _bg_weather_memory() -> None:
            self._ensure_background_llm_serialization()
            delay_sec = max(cfg_float("MEMORY_EXTRACTION_INITIAL_DELAY_SEC", 3.0), 0.1)
            await asyncio.sleep(delay_sec)

            yield_fn = getattr(self, "_yield_to_user_replies", None)
            if callable(yield_fn):
                await yield_fn()

            thought_wait_start = time.time()
            thought_last_logged = thought_wait_start
            thought_max_wait = max(30.0, cfg_float("LLM_YIELD_MAX_WAIT_SEC", 300.0))
            while getattr(self, "_is_thought_generation_active", False):
                elapsed = time.time() - thought_wait_start
                if elapsed >= thought_max_wait:
                    log.warning("weather memory: waiting for background thought generation reached maximum limit (%.1fs); proceeding", thought_max_wait)
                    break
                if elapsed >= 30.0 and time.time() - thought_last_logged >= 15.0:
                    thought_last_logged = time.time()
                    log.info("weather memory: background thought generation still active (elapsed=%.1fs); safely extending wait...", elapsed)
                await asyncio.sleep(0.2)

            bg_lock = getattr(self, "_bg_llm_lock", None)
            if bg_lock is None:
                bg_lock = asyncio.Lock()
                setattr(self, "_bg_llm_lock", bg_lock)

            async with bg_lock:
                if callable(yield_fn):
                    await yield_fn()
                setattr(self, "_is_memory_extraction_active", True)
                try:
                    await extract_and_store_memory(
                        cfg=cfg,
                        store=self.memory_store,
                        guild_id=getattr(message.guild, "id", None),
                        channel_id=runtime.cid,
                        user_id=author_id(message),
                        user_text=runtime.original_user_text,
                        assistant_text=reply,
                        context_lines=context_lines,
                        emotion_scores=weather_emotion_scores,
                        emotion_reason=weather_emotion_reason,
                        user_name=user_name,
                        user_display_name=user_display_name,
                    )
                except Exception as e:
                    log.exception("weather memory extract failed: %s", e)
                    report_error("reply_core._bg_weather_memory", e)
                finally:
                    setattr(self, "_is_memory_extraction_active", False)

                if callable(yield_fn):
                    await yield_fn()

                try:
                    summary_context_lines = await _build_summary_context_lines(
                        message,
                        sent,
                        base_context_lines=context_lines,
                        bot_user_id=runtime.bot_user_id,
                    )
                    await maybe_update_channel_summary(
                        cfg=cfg,
                        store=self.memory_store,
                        guild_id=getattr(message.guild, "id", None),
                        channel_id=runtime.cid,
                        context_lines=summary_context_lines,
                    )
                except Exception as e:
                    log.exception("weather summary update failed: %s", e)
                    report_error("reply_core._bg_weather_memory", e)
        
        asyncio.create_task(_bg_weather_memory())

        return True

    def _managed_channel_ids(self) -> set[int]:
        return cfg_managed_channel_ids()
