"""OllamaChatReplyCoreMixinのモデル呼び出し、再試行、直列化制御を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import re
import time
from typing import Any, Sequence, TYPE_CHECKING

import discord

from front.discord.common.discord_helpers import (
    looks_like_meta_ignorance_reply,
    sanitize_meta_ignorance_reply,
)
from ollama_bot.common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_unusable_assistant_reply,
    truncate_lines,
)
from harness.agent.prompt_parser.parser.reply_helpers import _strip_user_echo_prefix
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    append_chat_reply_output_suffix,
    channel_id,
    display_name,
    recent_context_items,
    sanitize_generated_reply,
)
from ollama_bot.ollama_chat_.ollama_chat_types import ReplyModelTimeoutError
from front.discord.reply.reply_core_common import (
    _OllamaChatReplyCoreBase,
    _is_context_size_exceeded_error,
    _is_transient_reply_model_error,
    _reply_model_timeout_fallback,
    call_ollama_chat_raw,
    cfg,
    cfg_float,
    cfg_int,
    log,
    release_ollama_model,
    report_error,
    resolve_reference_message,
)


class ReplyModelCallMixin(_OllamaChatReplyCoreBase):
    """モデル呼び出し、再試行・リカバリ、排他・直列化制御を担当するMixin。"""

    async def _yield_to_user_replies(self, timeout_sec: float = 30.0) -> None:
        """バックグラウンドLLMタスクがユーザー返信と競合しないよう、返信生成中（_active_user_reply_count > 0）は待機して譲歩する。

        ユーザー返信生成（70秒超等）の最中に timeout_sec を迎過しても強行実行せず、
        返信が完了するまで（または絶対上限まで）安全に待機を延長する。
        """
        start_ts = time.time()
        last_logged_ts = start_ts
        max_wait_sec = max(timeout_sec, cfg_float("LLM_YIELD_MAX_WAIT_SEC", 300.0))

        is_busy_fn = getattr(self, "_is_user_reply_busy", None)
        while is_busy_fn() if callable(is_busy_fn) else (getattr(self, "_active_user_reply_count", 0) > 0):
            elapsed = time.time() - start_ts
            if elapsed >= max_wait_sec:
                log.warning(
                    "[llm-yield] user reply active wait reached maximum limit (%.1fs, count=%s); continuing background task to prevent deadlock",
                    max_wait_sec,
                    getattr(self, "_active_user_reply_count", 0),
                )
                break

            if elapsed >= timeout_sec and time.time() - last_logged_ts >= 15.0:
                last_logged_ts = time.time()
                log.info(
                    "[llm-yield] user reply still active (count=%s, elapsed=%.1fs, max=%.1fs); safely extending wait...",
                    getattr(self, "_active_user_reply_count", 0),
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

    def _ensure_background_llm_serialization(self) -> None:
        """所感生成タスクと長期記憶抽出タスクの直列化・競合抑止フックを配備する。"""
        if getattr(self, "_bg_llm_serialization_installed", False):
            return

        orig_update_thought = getattr(self, "_update_user_thought_async", None)
        if callable(orig_update_thought):
            try:
                sig = inspect.signature(orig_update_thought)
                accepts_initial_delay = "initial_delay_sec" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
            except (ValueError, TypeError):
                accepts_initial_delay = True

            @functools.wraps(orig_update_thought)
            async def _serialized_update_thought(
                user_id: int,
                user_name: str = "",
                *args: Any,
                initial_delay_sec: float = 0.0,
                **kwargs: Any,
            ) -> Any:
                thought_interval = max(cfg_int("BG_THOUGHT_UPDATE_INTERVAL", 2), 1)
                thought_count = getattr(self, "_thought_update_counter", 0) + 1
                setattr(self, "_thought_update_counter", thought_count)

                if thought_count % thought_interval != 0:
                    log.info("[THOUGHT] skipping thought generation due to interval gate (%d %% %d != 0)", thought_count, thought_interval)
                    return None

                budget = getattr(self, "_current_turn_llm_budget", None)
                if isinstance(budget, dict):
                    budget["thought"] = 1

                call_kwargs = dict(kwargs)
                raw_delay = call_kwargs.pop("initial_delay_sec", initial_delay_sec)
                try:
                    delay = float(raw_delay or 0.0)
                except (ValueError, TypeError):
                    delay = 0.0

                if delay > 0.0:
                    await asyncio.sleep(delay)

                # ユーザー返信優先: 返信処理中なら譲歩
                yield_fn = getattr(self, "_yield_to_user_replies", None)
                if callable(yield_fn):
                    await yield_fn()

                # 記憶抽出中なら待機して直列化
                mem_wait_start = time.time()
                mem_last_logged = mem_wait_start
                mem_max_wait = max(30.0, cfg_float("LLM_YIELD_MAX_WAIT_SEC", 300.0))
                while getattr(self, "_is_memory_extraction_active", False):
                    elapsed = time.time() - mem_wait_start
                    if elapsed >= mem_max_wait:
                        log.warning("_update_user_thought_async: waiting for memory extraction reached maximum limit (%.1fs); proceeding", mem_max_wait)
                        break
                    if elapsed >= 30.0 and time.time() - mem_last_logged >= 15.0:
                        mem_last_logged = time.time()
                        log.info("_update_user_thought_async: memory extraction still active (elapsed=%.1fs); safely extending wait...", elapsed)
                    await asyncio.sleep(0.2)

                bg_lock = getattr(self, "_bg_llm_lock", None)
                if bg_lock is None:
                    bg_lock = asyncio.Lock()
                    setattr(self, "_bg_llm_lock", bg_lock)

                setattr(self, "_is_thought_generation_active", True)
                try:
                    async with bg_lock:
                        if callable(yield_fn):
                            await yield_fn()
                        if accepts_initial_delay:
                            call_kwargs["initial_delay_sec"] = 0.0
                        if args:
                            return await orig_update_thought(user_id, user_name, *args, **call_kwargs)
                        return await orig_update_thought(user_id, user_name=user_name, **call_kwargs)
                except Exception as e:
                    log.warning("failed to update user thought: %r", e)
                    report_error("reply_core._update_user_thought_async", e)
                    raise
                finally:
                    setattr(self, "_is_thought_generation_active", False)

            try:
                setattr(self, "_update_user_thought_async", _serialized_update_thought)
            except Exception as e:
                log.warning("failed to attach _serialized_update_thought: %r", e)

        setattr(self, "_bg_llm_serialization_installed", True)

    def _select_retry_backup_reply(self, candidate: str) -> str:
        cleaned = extract_first_user_facing_reply(candidate)
        if not cleaned:
            cleaned = sanitize_generated_reply(candidate)
        cleaned = _strip_user_echo_prefix("", cleaned).strip()
        if not cleaned:
            return ""
        if (
            looks_like_prompt_leak(cleaned)
            or looks_like_reasoning_leak(cleaned)
            or looks_like_multi_turn_output(cleaned)
            or looks_like_non_japanese_reply(cleaned)
            or looks_like_unusable_assistant_reply(cleaned)
        ):
            return ""
        return cleaned

    def _trim_memory_context_lines(self, context_lines: list[str]) -> list[str]:
        return truncate_lines(
            context_lines,
            max_lines=int(cfg("MEMORY_LONG_CONTEXT_LINES", 30) or 30),
            max_chars_per_line=int(cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260) or 260),
            max_total_chars=int(cfg("MEMORY_LONG_CONTEXT_MAX_TOTAL_CHARS", 5000) or 5000),
        )

    def _context_max_age_sec(self) -> float:
        return max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0)


    async def _execute_reply_model_single_attempt(
        self,
        message: discord.Message,
        prompt_with_suffix: str,
        *,
        system_prompt: str,
        images: list[str] | None,
        num_predict: int | None,
        raw_prompt: str | None = None,
        think_override: bool | None = None,
        num_ctx: int | None = None,
    ) -> str:
        effective_think = (
            think_override
            if think_override is not None
            else (str(cfg("OLLAMA_MAIN_MODEL_THINK", "false") or "false").strip().lower() in ("1", "true", "yes", "on"))
        )
        effective_num_ctx = num_ctx if num_ctx is not None else cfg_int("OLLAMA_NUM_CTX", 16384)

        # ハーネスオーケストレーター経由での実行
        AgentOrchestrator = None
        OrchestratorConfig = None
        tool_registry = getattr(self, "_tool_registry", None)
        try:
            from harness.agent import (
                AgentOrchestrator as _AO,
                OrchestratorConfig as _OC,
                get_default_registry as _GDR,
                register_builtin_tools as _RBT,
            )
            AgentOrchestrator = _AO
            OrchestratorConfig = _OC
            if tool_registry is None:
                if callable(_RBT):
                    tool_registry = _RBT(enable_web_search=True)
                elif callable(_GDR):
                    tool_registry = _GDR()
                if tool_registry is not None:
                    try:
                        setattr(self, "_tool_registry", tool_registry)
                    except Exception:
                        pass
        except Exception as e:
            log.warning("failed to import harness.agent: %r", e)

        # オーケストレーターが利用可能でない場合は従来の直接呼び出しへ安全にフォールバック
        if not callable(AgentOrchestrator) or not callable(OrchestratorConfig):
            return await self._call_with_typing(
                message,
                prompt_with_suffix,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
                think=effective_think,
                num_ctx=effective_num_ctx,
            )

        async def _discord_llm_chat(messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            tools = kwargs.get("tools")
            num_ctx_val = kwargs.get("num_ctx") or effective_num_ctx
            think_val = kwargs["think"] if "think" in kwargs else effective_think
            if tools:
                self._active_user_reply_count = getattr(self, "_active_user_reply_count", 0) + 1
                reply_evt = getattr(self, "_user_reply_done_event", None)
                if reply_evt is not None:
                    reply_evt.clear()
                try:
                    typing_cm = None
                    if self._is_typing_indicator_enabled() and hasattr(message, "channel") and hasattr(message.channel, "typing"):
                        try:
                            typing_cm = message.channel.typing()
                            await typing_cm.__aenter__()
                        except Exception as e:
                            if not self._is_typing_indicator_error(e):
                                raise
                            typing_cm = None
                    try:
                        async with self.semaphore:
                            raw_res = await call_ollama_chat_raw(
                                messages=messages,
                                tools=tools,
                                model=str(kwargs.get("model") or cfg("OLLAMA_MODEL", "") or "").strip() or None,
                                think=bool(think_val),
                                timeout_sec=kwargs.get("timeout_sec"),
                                temperature=kwargs.get("temperature"),
                                num_predict=num_predict if num_predict is not None else kwargs.get("num_predict"),
                                num_ctx=num_ctx_val,
                            )
                    finally:
                        if typing_cm is not None:
                            with contextlib.suppress(Exception):
                                await typing_cm.__aexit__(None, None, None)
                finally:
                    new_count = max(0, getattr(self, "_active_user_reply_count", 1) - 1)
                    self._active_user_reply_count = new_count
                    refresh_fn = getattr(self, "_refresh_user_reply_done_event", None)
                    if callable(refresh_fn):
                        refresh_fn()
                    elif new_count == 0:
                        done_evt = getattr(self, "_user_reply_done_event", None)
                        if done_evt is not None:
                            done_evt.set()

                raw_content = getattr(raw_res, "content", "") if not isinstance(raw_res, dict) else str(raw_res.get("content") or "")
                tool_calls = getattr(raw_res, "tool_calls", None) if not isinstance(raw_res, dict) else raw_res.get("tool_calls")
                msg_payload: dict[str, Any] = {"role": "assistant", "content": raw_content}
                if tool_calls:
                    msg_payload["tool_calls"] = tool_calls
                return {"message": msg_payload}

            user_contents = [
                str(m.get("content") or "").strip()
                for m in messages
                if m.get("role") == "user" and str(m.get("content") or "").strip()
            ]
            combined_user_msg = "\n\n".join(user_contents) if user_contents else (
                str(messages[-1].get("content") or "").strip() if messages else prompt_with_suffix
            )
            log.info(
                "[ollama-reply] Phase 5 LLM call: user_blocks=%d total_user_chars=%d",
                len(user_contents),
                len(combined_user_msg),
            )
            raw_out = await self._call_with_typing(
                message,
                combined_user_msg,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
                think=think_val,
                num_ctx=num_ctx_val,
            )
            return {"message": {"role": "assistant", "content": raw_out}}

        try:
            orch = AgentOrchestrator(
                config=OrchestratorConfig(
                    max_turns=cfg_int("ORCHESTRATOR_MAX_TURNS", 3),
                    model_name=str(cfg("OLLAMA_MODEL", "") or "").strip(),
                    think=effective_think,
                    timeout_sec=cfg_float("OLLAMA_TIMEOUT_SEC", 180.0),
                    num_ctx=effective_num_ctx,
                ),
                tool_registry=tool_registry,
                llm_client=_discord_llm_chat,
            )
            target_name = display_name(message).strip()
            latest_ref = await resolve_reference_message(message)
            cid = getattr(getattr(message, "channel", None), "id", None)
            cached_items = recent_context_items(self.channel_context_cache, cid, before_message=message, max_age_sec=3600) if cid else []
            other_names: set[str] = set()
            for item in cached_items:
                line = str(item.get("line") or "")
                line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
                if ":" in line_no_ts:
                    speaker_part = line_no_ts.split(":", 1)[0].strip()
                    speaker_clean = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
                    if speaker_clean and speaker_clean != target_name:
                        other_names.add(speaker_clean)
            ref_author = ""
            if latest_ref is not None:
                ref_author = display_name(latest_ref).strip()
                if ref_author and ref_author != target_name:
                    other_names.add(ref_author)

            from front.bridge import AgentBridge
            from front.discord.adapter import DiscordAdapter
            from front.discord.common.message_media import extract_first_image_url, resolve_message_with_media

            target_msg = await resolve_message_with_media(message)
            target_image_url = extract_first_image_url(target_msg) or extract_first_image_url(latest_ref)
            discord_adapter = DiscordAdapter()
            agent_task = discord_adapter.build_reply_task(
                message,
                user_prompt=raw_prompt or prompt_with_suffix,
                system_prompt=system_prompt,
                target_name=target_name,
                other_names=other_names,
                channel_id=cid,
                user_id=getattr(getattr(message, "author", None), "id", None),
                latest_ref=latest_ref,
                ref_author=ref_author,
                image_url=target_image_url,
            )
            outcome = await AgentBridge(orch).run_task(agent_task)
            if outcome.text:
                return outcome.text
            log.warning("orchestrator returned empty response; falling back to direct model call")
            return await self._call_with_typing(
                message,
                prompt_with_suffix,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
                think=effective_think,
                num_ctx=effective_num_ctx,
            )
        except Exception as e:
            if _is_transient_reply_model_error(e) or _is_context_size_exceeded_error(e):
                raise
            log.warning(
                "orchestrator reply model call failed; falling back to direct model call channel=%s message=%s err=%r",
                channel_id(message),
                getattr(message, "id", None),
                e,
            )
            return await self._call_with_typing(
                message,
                prompt_with_suffix,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
                think=effective_think,
                num_ctx=effective_num_ctx,
            )

    async def _try_compact_recovery_reply(
        self,
        message: discord.Message,
        *,
        system_prompt: str,
        images: list[str] | None,
        num_predict: int | None,
        think: bool | None = None,
        num_ctx: int | None = None,
    ) -> str:
        """コンテキストサイズ超過（HTTP 400）時に、長大な周辺情報を除外した最小プロンプトで1回だけリカバリ再試行する。"""
        try:
            user_text = str(getattr(message, "content", "") or "").strip() or "こんにちは"
            compact_prompt = f"対象メッセージ: {user_text}\n※短く自然に応答してください。"
            compact_prompt_with_suffix = append_chat_reply_output_suffix(compact_prompt)
            compact_system = system_prompt[:400].strip() if system_prompt else ""

            reply = await self._call_with_typing(
                message,
                compact_prompt_with_suffix,
                system_prompt=compact_system,
                images=images,
                num_predict=num_predict,
                think=think,
                num_ctx=num_ctx,
            )
            sanitized = sanitize_generated_reply(reply.strip())
            return sanitize_meta_ignorance_reply(sanitized)
        except Exception as rec_err:
            log.warning("compact recovery reply failed: %r", rec_err)
            return ""

    async def _call_reply_model_or_fallback(
        self,
        message: discord.Message,
        prompt: str,
        *,
        system_prompt: str,
        images: list[str] | None,
        num_predict: int | None,
        think_override: bool | None = None,
        num_ctx: int | None = None,
    ) -> str:
        # メインモデル実行直前に、直前の事前分析やWeb調査判定で使用された middle-llm 等をアンロードし、
        # メインモデルのVRAM空間（最大GPUオフロード）を確保する（BAN項目 170）
        middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
        if middle_model:
            timeout_sec = max(cfg_float("OLLAMA_UTILITY_RELEASE_TIMEOUT_SEC", 2.0), 0.2)
            await release_ollama_model(middle_model, timeout_sec=timeout_sec)

        prompt = self._clean_duplicate_prompt_instructions(prompt, system_prompt)
        prompt_with_suffix = append_chat_reply_output_suffix(prompt)
        max_retries = max(cfg_int("REPLY_MODEL_TRANSIENT_RETRY_COUNT", 2), 0)
        delay_sec = max(cfg_float("REPLY_MODEL_TRANSIENT_RETRY_DELAY_SEC", 1.5), 0.0)
        total_attempts = max_retries + 1
        last_transient_error: Exception | None = None

        for attempt in range(1, total_attempts + 1):
            try:
                reply = await self._execute_reply_model_single_attempt(
                    message,
                    prompt_with_suffix,
                    system_prompt=system_prompt,
                    images=images,
                    num_predict=num_predict,
                    raw_prompt=prompt,
                    think_override=think_override,
                    num_ctx=num_ctx,
                )
                if reply:
                    if looks_like_meta_ignorance_reply(reply):
                        log.warning("meta ignorance reply detected; rewriting to empathetic nod: %r", reply)
                        reply = sanitize_meta_ignorance_reply(reply)
                    return reply
                log.warning("reply model returned empty response on attempt %d/%d", attempt, total_attempts)
            except Exception as e:
                if _is_context_size_exceeded_error(e):
                    log.warning(
                        "reply model call failed due to Ollama context size exceeded (HTTP 400) channel=%s message=%s; "
                        "attempting emergency compact recovery with minimal context... err=%r",
                        channel_id(message),
                        getattr(message, "id", None),
                        e,
                    )
                    compact_recovery_reply = await self._try_compact_recovery_reply(
                        message,
                        system_prompt=system_prompt,
                        images=images,
                        num_predict=num_predict,
                        think=think_override,
                        num_ctx=num_ctx,
                    )
                    if compact_recovery_reply:
                        log.info(
                            "compact recovery reply succeeded after context size exceeded error channel=%s message=%s",
                            channel_id(message),
                            getattr(message, "id", None),
                        )
                        return compact_recovery_reply
                    log.warning(
                        "compact recovery reply failed or returned empty; using timeout fallback channel=%s message=%s",
                        channel_id(message),
                        getattr(message, "id", None),
                    )
                    return _reply_model_timeout_fallback()

                if not _is_transient_reply_model_error(e):
                    raise
                last_transient_error = e
                if attempt < total_attempts:
                    log.warning(
                        "reply model call transient error/timeout on attempt %d/%d; sleeping %.1f seconds before retry channel=%s message=%s err=%r",
                        attempt,
                        total_attempts,
                        delay_sec,
                        channel_id(message),
                        getattr(message, "id", None),
                        e,
                    )
                    await asyncio.sleep(delay_sec)
                else:
                    log.warning(
                        "reply model call transient error/timeout exhausted %d attempts channel=%s message=%s err=%r",
                        total_attempts,
                        channel_id(message),
                        getattr(message, "id", None),
                        e,
                    )

        if last_transient_error is not None:
            log.warning(
                "reply model call transient error exhausted all %d attempts; suppressing fallback reply to retry later channel=%s message=%s",
                total_attempts,
                channel_id(message),
                getattr(message, "id", None),
            )
            raise ReplyModelTimeoutError(
                f"reply model call timed out after {total_attempts} attempts: {last_transient_error!r}"
            ) from last_transient_error

        log.warning(
            "reply model call returned empty after %d attempts; using empty fallback channel=%s message=%s",
            total_attempts,
            channel_id(message),
            getattr(message, "id", None),
        )
        return str(cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "") or "").strip()

