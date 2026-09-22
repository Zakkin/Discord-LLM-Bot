"""img.2chan.netへの投稿生成、投稿実行、スレ監視ループを担当するMixin。
DiscordメッセージからimgスレURLを拾い、遅延投稿・返信追跡・結果報告までを行います。
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
from typing import Any

import discord

from ...common.discord_helpers import author_id, channel_id, content
from ...common.memory_logic import get_relevant_memories, get_user_habit_profile_lines
from ...common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    sanitize_generated_reply,
    truncate_text,
)
from ..ollama_chat_helpers import _get_channel_kind
from .img2chan_context import (
    _get_img2chan_helper,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    log,
)
from lib.text_utils import compact_exception_message as _compact_exception_message


from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _Img2chanPostingBase = OllamaChatProtocol
else:
    _Img2chanPostingBase = object


class _Img2chanPostingMixin(_Img2chanPostingBase):
    def _img2chan_character_guidance(self) -> str:
        parts: list[str] = []
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            parts.append(f"人格名: {display_name}")
        core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()
        if core_values:
            parts.append(f"核となる価値観・性格: {core_values}")
        for label, key in (
            ("基本キャラクター設定", "OLLAMA_SYSTEM_PROMPT"),
            ("口調ガード", "OLLAMA_REPLY_STYLE_GUARD"),
            ("追加ルール", "OLLAMA_REPLY_EXTRA_RULE"),
            ("img投稿追加ガード", "IMG2CHAN_POST_PERSONA_GUARD"),
        ):
            value = str(cfg(key, "") or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        if not parts:
            return ""
        guidance = "\n".join(parts)
        max_chars = max(cfg_int("IMG2CHAN_CHARACTER_GUIDANCE_MAX_CHARS", 1600), 200)
        return truncate_text(guidance, max_chars)

    def _img2chan_post_delay_sec(self) -> float:
        min_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MIN_DELAY_SEC", 60.0), 0.0)
        max_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MAX_DELAY_SEC", 1800.0), min_delay)
        return random.uniform(min_delay, max_delay)

    def _img2chan_post_generation_retries(self) -> int:
        return max(cfg_int("IMG2CHAN_POST_GENERATION_RETRIES", 1), 0)

    def _img2chan_post_generation_models(self) -> list[str]:
        models: list[str] = []
        for raw in (
            cfg("IMG2CHAN_POST_GENERATION_MODEL", cfg("OLLAMA_MODEL", "")),
            cfg("IMG2CHAN_POST_FALLBACK_MODEL", cfg("OLLAMA_MIDDLE_MODEL", "")),
        ):
            model = str(raw or "").strip()
            if model and model not in models:
                models.append(model)
        return models

    def _img2chan_promotion_enabled(self) -> bool:
        return cfg_bool("IMG2CHAN_PROMOTION_ENABLED", False)

    def _img2chan_promotion_probability(self) -> float:
        return max(0.0, min(cfg_float("IMG2CHAN_PROMOTION_PROBABILITY", 0.0), 1.0))

    def _img2chan_promotion_min_interval_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_PROMOTION_MIN_INTERVAL_SEC", 0.0), 0.0)

    def _img2chan_promotion_invite_url(self) -> str:
        invite_url = str(cfg("IMG2CHAN_PROMOTION_INVITE_URL", "") or "").strip()
        if invite_url and re.match(r"^discord\.gg/[A-Za-z0-9_-]+$", invite_url, flags=re.IGNORECASE):
            return f"https://{invite_url}"
        return invite_url

    def _img2chan_promotion_message(self) -> str:
        message = str(cfg("IMG2CHAN_PROMOTION_MESSAGE", "") or "").strip()
        if message:
            return message
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            return f"普段はDiscordの{display_name}でも喋ってる。"
        return "普段はDiscordでも喋ってる。"

    def _should_attach_img2chan_promotion(self) -> bool:
        if not self._img2chan_promotion_enabled():
            return False
        if not self._img2chan_promotion_invite_url():
            return False
        probability = self._img2chan_promotion_probability()
        if probability <= 0.0:
            return False
        last_sent_at = float(getattr(self, "_img2chan_last_promotion_at", 0.0) or 0.0)
        min_interval_sec = self._img2chan_promotion_min_interval_sec()
        if last_sent_at > 0.0 and min_interval_sec > 0.0 and time.time() - last_sent_at < min_interval_sec:
            return False
        return random.random() < probability

    def _log_img2chan_thread_analysis(
        self,
        *,
        context: str,
        url: str,
        thread: dict[str, Any],
        message: discord.Message | None = None,
    ) -> None:
        helper = _get_img2chan_helper()
        formatter = getattr(helper, "format_img_thread_log_summary", None) if helper is not None else None
        try:
            if callable(formatter):
                summary = formatter(
                    thread,
                    max_replies=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_REPLIES", 5), 0),
                    max_chars=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_CHARS", 1400), 300),
                )
            else:
                summary = (
                    f"IMG thread parse url={url} "
                    f"thread_no={str((thread or {}).get('thread_no') or '') or '-'} "
                    f"replies={int((thread or {}).get('total_reply_count') or 0)} "
                    f"fetched={int((thread or {}).get('fetched_reply_count') or 0)} "
                    f"op={truncate_text(str((thread or {}).get('op_text') or '').strip(), 160)!r}"
                )
        except Exception as e:
            log.warning("img2chan thread analysis log format failed url=%s err=%r", url, e)
            return

        log.info(
            "img2chan thread analysis context=%s channel=%s author=%s\n%s",
            context,
            channel_id(message) if message is not None else None,
            author_id(message) if message is not None else None,
            summary,
        )

    def _mark_img2chan_promotion_sent(self) -> None:
        self._img2chan_last_promotion_at = time.time()

    def _maybe_append_img2chan_promotion(
        self,
        helper: Any,
        comment: str,
    ) -> tuple[str, bool, str]:
        current_comment = str(comment or "").strip()
        invite_url = self._img2chan_promotion_invite_url()
        if not current_comment or not invite_url or not self._should_attach_img2chan_promotion():
            return current_comment, False, ""

        promo_message = self._img2chan_promotion_message()
        builder = getattr(helper, "append_img_post_promotion", None) if helper is not None else None
        if callable(builder):
            promoted_comment = builder(
                current_comment,
                promo_message,
                invite_url,
                max_chars=340,
            )
        else:
            promoted_comment = "\n".join(
                part for part in (current_comment, promo_message, invite_url) if str(part or "").strip()
            )
        promoted_comment = str(promoted_comment or "").strip()
        if not promoted_comment or invite_url not in promoted_comment:
            return current_comment, False, ""
        return promoted_comment, True, invite_url

    def _build_img2chan_followup_retry_prompt(
        self,
        base_prompt: str,
        *,
        target_reply: dict[str, Any] | None = None,
    ) -> str:
        excerpt = truncate_text(
            re.sub(r"\s+", " ", str((target_reply or {}).get("text") or "").strip()),
            120,
        )
        lines = [
            str(base_prompt or "").strip(),
            "",
            "追加ルール:",
            "- 直前の候補はオウム返し寄りだったので破棄してください。",
            "- 今回返す対象レスの本文や語尾をそのままなぞるのは禁止です。",
            "- 対象レスの言い換えだけで終わらせず、自分の反応やツッコミを自分の言葉で入れてください。",
            "- 出力は引き続きJSONだけにし、comment には新しく書く本文だけを入れてください。",
        ]
        if excerpt:
            lines.append(f"- 対象レスの本文をそのまま写したら失敗です。対象レス: {excerpt}")
        return "\n".join(lines).strip()

    def _img2chan_comment_parrot_reason(
        self,
        helper: Any,
        comment: str,
        *,
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
    ) -> str:
        comment_text = str(comment or "").strip()
        if not comment_text:
            return ""

        checker = getattr(helper, "looks_like_img_reply_parrot", None) if helper is not None else None

        def _looks_like_reference_echo(reference_text: object) -> bool:
            reference = str(reference_text or "").strip()
            if not reference:
                return False
            if callable(checker):
                with contextlib.suppress(Exception):
                    if bool(checker(comment_text, reference)):
                        return True
            return looks_like_parrot_reply(reference, comment_text)

        if isinstance(target_reply, dict) and _looks_like_reference_echo(target_reply.get("text")):
            return "target_reply"

        for own_post in list(quoted_own_posts or [])[:2]:
            if isinstance(own_post, dict) and _looks_like_reference_echo(own_post.get("text")):
                return "quoted_own_post"

        return ""

    async def _maybe_schedule_img2chan_thread_post(self, message: discord.Message) -> None:
        if not cfg_bool("IMG2CHAN_AUTO_POST_ENABLED", True):
            return
        if message.guild is None:
            return
        if getattr(message.author, "bot", False):
            return
        if _get_channel_kind(message) == "none":
            return

        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "_extract_img_thread_urls_full"):
            return

        urls = list(getattr(helper, "_extract_img_thread_urls_full")(content(message)) or [])
        if not urls:
            return

        self._prune_img2chan_post_schedules()
        tasks, recent, states = self._ensure_img2chan_thread_tracking_state()

        max_urls = max(cfg_int("IMG2CHAN_AUTO_POST_MAX_URLS_PER_MESSAGE", 1), 1)
        max_active = self._img2chan_active_thread_limit()
        active_count = sum(1 for task in tasks.values() if task is not None and not task.done())
        dedup_ttl = max(cfg_float("IMG2CHAN_AUTO_POST_DEDUP_TTL_SEC", 3600.0), 60.0)
        now = time.monotonic()
        active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()

        for raw_url in urls[:max_urls]:
            normalize = getattr(helper, "normalize_img_thread_url", lambda value: str(value or "").strip())
            url = str(normalize(raw_url) or "").strip()
            if not url:
                continue
            if (
                cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True)
                and active_url
                and url != active_url
                and active_url in tasks
                and not tasks[active_url].done()
            ):
                log.info("img2chan active thread lock skip url=%s active_url=%s", url, active_url)
                continue
            if url in tasks and not tasks[url].done():
                log.info("img2chan auto post already scheduled url=%s", url)
                continue
            if now - float(recent.get(url, 0.0) or 0.0) < dedup_ttl:
                log.info("img2chan auto post dedup skip url=%s", url)
                continue
            if active_count >= max_active:
                log.info("img2chan auto post active limit skip url=%s active=%d", url, active_count)
                continue

            delay_sec = self._img2chan_post_delay_sec()
            recent[url] = now
            thread_state = self._img2chan_thread_state(url)
            thread_state["origin_channel_id"] = channel_id(message)
            thread_state["origin_message_id"] = getattr(message, "id", None)
            self._img2chan_active_thread_url = url
            task = asyncio.create_task(
                self._run_img2chan_thread_post_after_delay(message, url, delay_sec)
            )
            tasks[url] = task
            active_count += 1

            def _done_callback(done_task: asyncio.Task, *, scheduled_url: str = url) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = done_task.exception()
                    if exc is not None:
                        log.exception("img2chan auto post task failed url=%s err=%r", scheduled_url, exc)
                task_map = getattr(self, "_img2chan_post_tasks", None)
                if isinstance(task_map, dict) and task_map.get(scheduled_url) is done_task:
                    task_map.pop(scheduled_url, None)
                state_map = getattr(self, "_img2chan_thread_states", None)
                if isinstance(state_map, dict):
                    state_map.pop(scheduled_url, None)
                active_thread_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
                if active_thread_url == scheduled_url:
                    self._img2chan_active_thread_url = ""

            task.add_done_callback(_done_callback)
            log.info(
                "img2chan auto post scheduled url=%s delay_sec=%.1f channel=%s author=%s",
                url,
                delay_sec,
                channel_id(message),
                author_id(message),
            )

    async def _run_img2chan_thread_post_after_delay(
        self,
        message: discord.Message,
        url: str,
        delay_sec: float,
    ) -> None:
        thread_state = self._img2chan_thread_state(url)
        try:
            await asyncio.sleep(max(float(delay_sec), 0.0))
            await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=delay_sec,
                thread_state=thread_state,
                requester_text=content(message),
                report_result=True,
            )
            await self._monitor_img2chan_thread_session(message, url, thread_state=thread_state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("img2chan delayed post failed url=%s err=%r", url, e)
            await self._send_img2chan_post_report(
                message,
                {
                    "success": False,
                    "url": url,
                    "comment": "",
                    "error": f"処理中にエラーが発生しました: {e}",
                },
                delay_sec=delay_sec,
            )
        finally:
            reply_tasks = thread_state.get("reply_tasks")
            if isinstance(reply_tasks, dict):
                running_tasks = [task for task in reply_tasks.values() if task is not None and not task.done()]
                for task in running_tasks:
                    task.cancel()
                if running_tasks:
                    await asyncio.gather(*running_tasks, return_exceptions=True)

    async def _monitor_img2chan_thread_session(
        self,
        message: discord.Message,
        url: str,
        *,
        thread_state: dict[str, object],
    ) -> None:
        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "fetch_img_thread"):
            return
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        max_replies = self._img2chan_thread_monitor_max_replies()
        poll_sec = self._img2chan_thread_monitor_poll_sec()

        while True:
            try:
                thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=max_replies,
                    timeout_sec=timeout_sec,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("img2chan monitor fetch failed url=%s err=%r", url, e)
                await asyncio.sleep(poll_sec)
                continue

            error = str((thread or {}).get("error") or "").strip()
            if error:
                if self._img2chan_thread_dropped(error):
                    log.info("img2chan active thread finished url=%s reason=%s", url, error)
                    return
                log.warning("img2chan monitor nonterminal fetch error url=%s err=%s", url, error)
                await asyncio.sleep(poll_sec)
                continue

            self._match_img2chan_pending_own_posts(thread_state, thread)
            candidates = self._discover_img2chan_quote_reply_candidates(thread_state, thread)
            if candidates:
                self._schedule_img2chan_followup_reply_tasks(message, url, thread_state, candidates)
            await self._maybe_learn_from_img2chan_thread_feedback(message, url, thread_state, thread)
            await asyncio.sleep(poll_sec)

    async def _run_img2chan_followup_reply_task(
        self,
        message: discord.Message,
        url: str,
        *,
        thread_state: dict[str, Any],
        target_reply: dict[str, Any],
        quoted_own_posts: list[dict[str, Any]],
    ) -> None:
        target_post_no = str(target_reply.get("post_no") or "").strip()
        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight
        raw_tasks = thread_state.get("reply_tasks")
        reply_tasks: dict[Any, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
        thread_state["reply_tasks"] = reply_tasks

        try:
            result = await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=None,
                thread_state=thread_state,
                requester_text="",
                target_reply=target_reply,
                quoted_own_posts=quoted_own_posts,
                report_result=True,
            )
            if bool(result.get("success")):
                handled.add(target_post_no)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("img2chan followup reply task failed url=%s target_post_no=%s err=%r", url, target_post_no, e)
        finally:
            inflight.discard(target_post_no)
            if reply_tasks.get(target_post_no) is asyncio.current_task():
                reply_tasks.pop(target_post_no, None)

    async def _execute_img2chan_thread_post(
        self,
        message: discord.Message,
        url: str,
        *,
        delay_sec: float | None,
        thread_state: dict[str, Any] | None = None,
        requester_text: str = "",
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        report_result: bool = True,
    ) -> dict[str, Any]:
        helper = _get_img2chan_helper()
        if helper is None:
            return {
                "success": False,
                "url": url,
                "comment": "",
                "error": "img2chan helper が利用できません。",
            }

        max_replies = max(self._img2chan_post_context_replies(), self._img2chan_thread_monitor_max_replies())
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        try:
            thread = await getattr(helper, "fetch_img_thread")(
                url,
                max_replies=max_replies,
                timeout_sec=timeout_sec,
            )
        except Exception as e:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {e}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        error = str((thread or {}).get("error") or "").strip()
        if error:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {error}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        self._log_img2chan_thread_analysis(
            context="followup_fetch" if target_reply is not None else "initial_fetch",
            url=url,
            thread=thread,
            message=message,
        )

        if thread_state is not None:
            self._img2chan_track_known_replies(thread_state, thread)

        max_chars = max(cfg_int("IMG2CHAN_POST_MAX_CHARS", 120), 20)
        character_guidance = self._img2chan_character_guidance()
        social_guidance = ""
        memory_lines: list[str] = []
        habit_profile_lines: list[str] = []
        channel_summary: str = ""

        try:
            runtime = await self._get_or_build_runtime(message)
            if runtime:
                social_guidance = await self._get_current_reply_social_guidance(runtime)
                mem_lines, ch_summary = await get_relevant_memories(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(getattr(message, "guild", None), "id", None),
                    user_id=author_id(message),
                    channel_id=channel_id(message),
                    user_text=requester_text or content(message),
                )
                memory_lines = list(mem_lines or [])
                channel_summary = str(ch_summary or "")
                habit_profile_lines = await get_user_habit_profile_lines(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(getattr(message, "guild", None), "id", None),
                    user_id=author_id(message),
                )
        except Exception as e:
            log.warning("img2chan context gathering failed channel=%s err=%r", channel_id(message), e)

        if target_reply is not None and hasattr(helper, "build_img_thread_followup_post_prompt"):
            prompt = getattr(helper, "build_img_thread_followup_post_prompt")(
                thread,
                target_reply,
                quoted_own_posts=list(quoted_own_posts or []),
                bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                max_chars=max_chars,
                character_guidance=character_guidance,
                social_guidance=social_guidance,
                memory_lines=memory_lines,
                habit_profile_lines=habit_profile_lines,
                channel_summary=channel_summary,
            )
        else:
            prompt = getattr(helper, "build_img_thread_post_prompt")(
                thread,
                requester_text=requester_text or content(message),
                bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                max_chars=max_chars,
                character_guidance=character_guidance,
                social_guidance=social_guidance,
                memory_lines=memory_lines,
                habit_profile_lines=habit_profile_lines,
                channel_summary=channel_summary,
            )
        system_prompt = getattr(helper, "build_img_thread_post_system_prompt")(
            self._reply_system_prompt()
        )

        generation_models = self._img2chan_post_generation_models()
        if not generation_models:
            generation_models = [str(cfg("OLLAMA_MODEL", "") or "").strip()]
        generation_timeout_sec = max(cfg_float("IMG2CHAN_POST_GENERATION_TIMEOUT_SEC", 45.0), 10.0)

        comment = ""
        try:
            generated: dict[str, Any] | None = None
            last_generation_error: Exception | None = None
            retry_prompt = (
                self._build_img2chan_followup_retry_prompt(prompt, target_reply=target_reply)
                if target_reply is not None
                else ""
            )
            for generation_model in generation_models:
                if not generation_model:
                    continue
                prompt_variants = [("base", prompt)]
                if retry_prompt:
                    prompt_variants.append(("anti_parrot", retry_prompt))
                for prompt_variant, prompt_text in prompt_variants:
                    try:
                        candidate = await self._call_img2chan_ollama_json(
                            prompt_text,
                            purpose="post_generation",
                            model=generation_model,
                            system_prompt=system_prompt,
                            schema=getattr(helper, "IMG_THREAD_POST_COMMENT_SCHEMA", {"type": "object"}),
                            think=False,
                            retries=self._img2chan_post_generation_retries(),
                            temperature=cfg_float("IMG2CHAN_POST_TEMPERATURE", 0.45),
                            top_p=cfg_float("IMG2CHAN_POST_TOP_P", 0.8),
                            repeat_penalty=cfg_float("IMG2CHAN_POST_REPEAT_PENALTY", 1.05),
                            num_predict=max(cfg_int("IMG2CHAN_POST_NUM_PREDICT", 384), 384),
                            timeout_sec=generation_timeout_sec,
                        )
                    except Exception as e:
                        last_generation_error = e
                        log.warning(
                            "img2chan post generation failed model=%s url=%s prompt_variant=%s err=%r",
                            generation_model,
                            url,
                            prompt_variant,
                            e,
                        )
                        continue

                    candidate_raw_comment = str(candidate.get("comment") or "")
                    candidate_comment = extract_first_user_facing_reply(candidate_raw_comment.strip()) or sanitize_generated_reply(
                        candidate_raw_comment.strip()
                    )
                    candidate_comment = getattr(helper, "sanitize_img_post_text")(candidate_comment, max_chars=max_chars)
                    if not candidate_comment or looks_like_abnormal_assistant_reply(candidate_comment):
                        last_generation_error = RuntimeError("投稿本文を作れませんでした。")
                        log.warning(
                            "img2chan generated unusable comment rejected model=%s url=%s prompt_variant=%s comment=%r",
                            generation_model,
                            url,
                            prompt_variant,
                            candidate_comment,
                        )
                        continue

                    parrot_reason = self._img2chan_comment_parrot_reason(
                        helper,
                        candidate_comment,
                        target_reply=target_reply,
                        quoted_own_posts=quoted_own_posts,
                    )
                    if parrot_reason:
                        last_generation_error = RuntimeError("相手の文面をなぞる候補しか作れませんでした。")
                        log.warning(
                            "img2chan generated parrot-like comment rejected model=%s url=%s prompt_variant=%s reason=%s comment=%r",
                            generation_model,
                            url,
                            prompt_variant,
                            parrot_reason,
                            candidate_comment,
                        )
                        continue

                    generated = candidate
                    comment = candidate_comment
                    break
                if generated is not None:
                    break
            if generated is None:
                raise last_generation_error or RuntimeError("投稿文生成に失敗しました。")
            if target_reply is not None:
                target_post_no = str(target_reply.get("post_no") or "").strip()
                target_reply_no = int(target_reply.get("number") or 0)
                quote_post_numbers = [target_post_no] if target_post_no else []
                quote_reply_numbers = [] if target_post_no else ([target_reply_no] if target_reply_no > 0 else [])
            else:
                quote_post_numbers = list(generated.get("quote_post_numbers") or [])
                quote_reply_numbers = list(generated.get("quote_reply_numbers") or [])
        except Exception as e:
            reason = _compact_exception_message(e)
            result = {"success": False, "url": url, "comment": "", "error": f"投稿文生成に失敗しました: {reason}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "url": url, "comment": comment, "error": "投稿本文を作れませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if hasattr(helper, "compose_img_thread_reply_comment"):
            post_plan = getattr(helper, "compose_img_thread_reply_comment")(
                thread,
                comment,
                quote_post_numbers=quote_post_numbers,
                quote_reply_numbers=quote_reply_numbers,
                max_chars=340,
            )
            comment = str(post_plan.get("comment") or comment).strip()

        promotion_applied = False
        promotion_invite_url = ""
        comment, promotion_applied, promotion_invite_url = self._maybe_append_img2chan_promotion(
            helper,
            comment,
        )

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "url": url, "comment": comment, "error": "投稿本文を整形できませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if cfg_bool("IMG2CHAN_POST_DRY_RUN", False):
            result = {
                "success": True,
                "dry_run": True,
                "url": url,
                "comment": comment,
                "error": "",
            }
        else:
            result = await getattr(helper, "submit_img_thread_reply")(
                url,
                comment,
                email=str(cfg("IMG2CHAN_POST_EMAIL", "") or ""),
                password=str(cfg("IMG2CHAN_POST_PASSWORD", "") or ""),
                timeout_sec=timeout_sec,
                preserve_urls=[promotion_invite_url] if promotion_applied and promotion_invite_url else None,
            )

        if promotion_applied and (
            bool(result.get("dry_run"))
            or bool(result.get("success"))
            or bool(result.get("submitted"))
        ):
            self._mark_img2chan_promotion_sent()

        if bool(result.get("success")) and not bool(result.get("dry_run")) and thread_state is not None:
            self._remember_img2chan_pending_own_post(thread_state, comment)
            known_before_after = set(thread_state.get("known_reply_post_nos", set()) or [])
            try:
                after_thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=self._img2chan_thread_monitor_max_replies(),
                    timeout_sec=timeout_sec,
                )
            except Exception as e:
                log.warning("img2chan post followup fetch failed url=%s err=%r", url, e)
            else:
                after_error = str((after_thread or {}).get("error") or "").strip()
                if not after_error:
                    matched_reply = getattr(helper, "find_img_thread_reply_by_comment")(
                        after_thread,
                        comment,
                        exclude_post_numbers=known_before_after,
                    ) if hasattr(helper, "find_img_thread_reply_by_comment") else None
                    self._img2chan_track_known_replies(thread_state, after_thread)
                    if matched_reply:
                        self._register_img2chan_own_post(thread_state, matched_reply)
                        result["registered_post_no"] = str(matched_reply.get("post_no") or "").strip()
                    else:
                        matched_replies = self._match_img2chan_pending_own_posts(thread_state, after_thread)
                        if matched_replies:
                            result["registered_post_no"] = str((matched_replies[-1] or {}).get("post_no") or "").strip()
            await self._store_img2chan_post_record(
                message,
                url=url,
                comment=comment,
                target_reply=target_reply,
                registered_post_no=str(result.get("registered_post_no") or "").strip(),
                dry_run=bool(result.get("dry_run")),
            )

        if report_result:
            await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
        return result

    async def _send_img2chan_post_report(
        self,
        message: discord.Message,
        post_result: dict[str, Any],
        *,
        delay_sec: float | None = None,
    ) -> None:
        if not cfg_bool("IMG2CHAN_POST_REPORT_ENABLED", False):
            return
        helper = _get_img2chan_helper()
        if helper is not None and hasattr(helper, "format_img_post_discord_report"):
            report = getattr(helper, "format_img_post_discord_report")(post_result, delay_sec=delay_sec)
        else:
            result = dict(post_result or {})
            report = str(result.get("error") or "imgの書き込み処理が終わりました。")
        report = truncate_text(str(report or "").strip(), 1900)
        if not report:
            return
        channel = getattr(message, "channel", None)
        if channel is None or not hasattr(channel, "send"):
            return
        await channel.send(report, allowed_mentions=discord.AllowedMentions.none())
