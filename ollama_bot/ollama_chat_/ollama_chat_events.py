"""Discordイベントの入口、メッセージキュー、反応、背景タスク起動を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, Callable, Coroutine, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from lib.text_utils import compact_exception_message as _compact_exception_message


from ..common.config_helpers import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_int_set,
    cfg_managed_channel_ids,
    cfg_primary_channel_id,
)
from ..common import habit_policy
from ..common.discord_helpers import append_context_message
from ..common.emotion_helpers import emotion_scoring_enabled
from ..common.fact_check import detect_reply_action
from ..common.singing_helpers import extract_song_title, is_singing_stop_command
from .ollama_chat_helpers import (
    _cached_other_channel_response_allowed,
    _get_channel_kind,
    _infer_feedback_type,
    _is_force_response_trigger,
    _is_rate_limited,
    _looks_like_negative_habit_followup,
    _looks_like_reactionish_followup,
    _reset_other_channel_unreplied_count,
    _should_ignore,
    _user_valence_improved_for_habit,
    looks_like_feedback_text,
)
from ..common.reply_helpers import (
    _format_reply,
)
from ..common.context_helpers import (
    _find_last_assistant_message_text,
    _check_context_flow,
)
from ..common.ollama_helpers import (
    extract_first_user_facing_reply,
    sanitize_generated_reply,
    truncate_text,
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    release_ollama_model,
)
from ..common.discord_helpers import (
    author_id,
    channel_id,
    content,
    resolve_reference_message,
    remove_context_message,
)
from ..common.json_compat import dumps as json_dumps
from .ollama_chat_types import EmotionState, MessageRuntime, UserRelationship
from .events.research import _ResearchEventMixin
from .events.img2chan import _Img2chanEventMixin

log = logging.getLogger("ollama_bot.ollama_chat_.events")

if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChatEventsBase = OllamaChatProtocol
else:
    _OllamaChatEventsBase = object


REPLY_PRIORITY_FORCE = 1   # メンション / 返信（最優先）

REPLY_PRIORITY_NORMAL = 5  # 通常チャンネル発話


class OllamaChatEventMixin(_ResearchEventMixin, _Img2chanEventMixin, _OllamaChatEventsBase):
    def _background_task_needs_restart(self, task: asyncio.Task | None) -> bool:
        if task is None:
            return True
        return bool(task.done())

    def _start_background_task_if_needed(
        self,
        attr_name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        task = getattr(self, attr_name, None)
        if not self._background_task_needs_restart(task):
            return
        if task is not None and task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                exc = task.exception()
                if exc is not None:
                    log.warning("restarting background task attr=%s after failure: %r", attr_name, exc)
        setattr(self, attr_name, asyncio.create_task(coro_factory()))

    def _ensure_background_tasks_started(self) -> None:
        if cfg_bool("EMOTION_HOURLY_DECAY_ENABLED", True):
            self._start_background_task_if_needed("_emotion_decay_task", self._emotion_decay_loop)
        if cfg_bool("HABIT_LEARNING_ENABLED", True):
            self._start_background_task_if_needed("_habit_decay_task", self._habit_decay_loop)
        if cfg_bool("OLLAMA_TOPIC_LOOP_ENABLED", False):
            self._start_background_task_if_needed("topic_task", self._topic_loop)
        if cfg_bool("AGENT_TICK_ENABLED", True):
            self._start_background_task_if_needed("agent_task", self._agent_tick_loop)
        if cfg_bool("REFLECTION_ENABLED", True):
            self._start_background_task_if_needed("_reflection_task", self._reflection_loop)
        if cfg_bool("RELATIONSHIP_THOUGHT_LOOP_ENABLED", True):
            self._start_background_task_if_needed("_relationship_thought_task", self._relationship_thought_loop)
        self._start_background_task_if_needed("_game_pool_replenish_task", self._game_pool_replenish_loop)
        self._start_background_task_if_needed("_queue_worker_task", self._reply_queue_worker)

    async def _game_pool_replenish_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval_sec = max(float(cfg("GAME_POOL_REPLENISH_INTERVAL_SEC", 300.0) or 300.0), 60.0)
        target_umigame_count = int(cfg("GAME_POOL_TARGET_UMIGAME_COUNT", 3) or 3)
        target_twenty_doors_count = int(cfg("GAME_POOL_TARGET_TWENTY_DOORS_COUNT", 3) or 3)
        
        while not self.bot.is_closed():
            await asyncio.sleep(interval_sec)
            try:
                if not hasattr(self, "game_pool_manager"):
                    continue
                counts = self.game_pool_manager.get_counts()
                
                # umigame replenish
                if counts["umigame"] < target_umigame_count:
                    if hasattr(self, "_generate_umigame_for_pool"):
                        log.info("replenishing umigame pool...")
                        item = await self._generate_umigame_for_pool()
                        if item:
                            await self.game_pool_manager.push_umigame(item)
                            log.info("umigame pool replenished (count=%d)", counts["umigame"] + 1)
                
                # 20doors replenish
                if counts["twenty_doors"] < target_twenty_doors_count:
                    if hasattr(self, "_generate_twenty_doors_for_pool"):
                        log.info("replenishing twenty_doors pool...")
                        item = await self._generate_twenty_doors_for_pool()
                        if item:
                            await self.game_pool_manager.push_twenty_doors(item)
                            log.info("twenty_doors pool replenished (count=%d)", counts["twenty_doors"] + 1)
                            
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("game pool replenish loop failed: %r", e)

    async def _habit_decay_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval_sec = max(float(cfg("HABIT_DECAY_INTERVAL_SEC", 86400.0) or 86400.0), 3600.0)
        while not self.bot.is_closed():
            await asyncio.sleep(interval_sec)
            try:
                await habit_policy.decay_old_habits(
                    self.memory_store,
                    persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("habit decay loop failed: %r", e)

    async def _maybe_finalize_pending_habit_turn(
        self,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> None:
        key = (getattr(runtime, "cid", None), getattr(runtime, "user_id", None))
        pending = self._pending_habit_turns.pop(key, None)
        if not pending:
            return

        used_at_raw = pending.get("used_at", time.time())
        used_at_val = float(used_at_raw) if isinstance(used_at_raw, (int, float)) else time.time()
        elapsed_sec = max(time.time() - used_at_val, 0.0)
        pending_scores = pending.get("emotion_scores") if isinstance(pending, dict) else None
        pending_scores_dict = pending_scores if isinstance(pending_scores, dict) else None
        user_text = str(getattr(runtime, "original_user_text", "") or "")
        reward = habit_policy.compute_turn_reward(
            user_replied_quickly=elapsed_sec <= 120.0,
            conversation_continued=bool(user_text.strip()),
            no_negative_follow=not _looks_like_negative_habit_followup(user_text),
            user_valence_improved=_user_valence_improved_for_habit(
                pending_scores_dict,
                incoming_emotion_scores,
            ),
            got_reaction=_looks_like_reactionish_followup(user_text),
        )
        await habit_policy.update_habit(
            self.memory_store,
            persona=str(pending.get("persona", cfg("MEMORY_PERSONA_NAMESPACE", "default")) or "default"),
            context_key=str(pending.get("context_key", "") or ""),
            action_style=str(pending.get("action_style", "") or ""),
            reward=reward,
        )
    async def _run_emotion_update_for_message(
        self,
        message: discord.Message,
        *,
        runtime: MessageRuntime | None = None,
        incoming_scores: dict[str, float] | None = None,
        incoming_appraisal: str | None = None,
        incoming_reaction: str | None = None,
    ) -> None:
        if not emotion_scoring_enabled():
            return
        try:
            log.info("TRACE emotion: start channel=%s author=%s", channel_id(message), author_id(message))
            runtime_for_emotion = runtime or await asyncio.wait_for(self._get_or_build_runtime(message), timeout=20.0)
            log.info("TRACE emotion: runtime built channel=%s author=%s", channel_id(message), author_id(message))
            state_timeout = self._emotion_update_timeout_sec(needs_scoring=incoming_scores is None)
            try:
                reaction_result = await asyncio.wait_for(
                    self._update_emotion_state(
                        runtime_for_emotion,
                        incoming_scores=incoming_scores,
                        incoming_appraisal=incoming_appraisal,
                        incoming_reaction=incoming_reaction,
                    ),
                    timeout=state_timeout,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "emotion state update timed out: channel=%s author=%s needs_scoring=%s timeout=%.1f",
                    channel_id(message),
                    author_id(message),
                    incoming_scores is None,
                    state_timeout,
                )
                return
            
            if reaction_result and reaction_result != "NONE":
                try:
                    await asyncio.wait_for(
                        self._maybe_add_scored_reaction(message, reaction_result),
                        timeout=cfg_float("EMOTION_REACTION_TIMEOUT_SEC", 5.0),
                    )
                except asyncio.TimeoutError:
                    log.warning("async reaction add timed out: channel=%s", channel_id(message))
                except Exception as e:
                    log.warning("async reaction add failed: %r", e)

            log.info("TRACE emotion: state updated channel=%s author=%s dominant=%s", channel_id(message), author_id(message), self.emotion_state.dominant_emotion)
            presence_timeout = self._emotion_presence_sync_timeout_sec()
            try:
                await asyncio.wait_for(self._sync_emotion_presence(), timeout=presence_timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "emotion presence sync timed out: channel=%s author=%s timeout=%.1f",
                    channel_id(message),
                    author_id(message),
                    presence_timeout,
                )
                return
            log.info("TRACE emotion: presence synced channel=%s author=%s", channel_id(message), author_id(message))
        except Exception as e:
            log.exception("emotion update failed: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            log.info(
                "TRACE on_message: entered guild=%s channel=%s author=%s bot=%s content=%r",
                getattr(message.guild, "id", None), channel_id(message), author_id(message), getattr(message.author, "bot", False), content(message)
            )

            a_id = author_id(message)
            ignored_user_ids = cfg_int_set("IGNORE_USER_IDS")
            if a_id in ignored_user_ids:
                log.info("TRACE on_message: ignore user drop channel=%s author=%s content=%r", channel_id(message), a_id, content(message))
                return

            # 発言者のプロファイルを最新情報で更新・追跡
            if not getattr(message.author, "bot", False) and message.guild is not None:
                try:
                    await self._record_message_author_profile(message)
                except Exception as e:
                    log.debug("record message author profile skipped: %r", e)

            cid = channel_id(message)
            channel_umigame_active = (cid in self.umigame_states) if (cid is not None and hasattr(self, "umigame_states")) else False
            channel_twenty_doors_busy = (
                self._is_twenty_doors_busy_channel(cid)
                if (cid is not None and hasattr(self, "_is_twenty_doors_busy_channel"))
                else False
            )

            # 歌唱中チャンネルでの中断判定：停止指示があれば即座にキャンセル
            # ※ Bot自身（歌詞投稿等）や他Botのメッセージによる誤中断を防止
            if (
                cid is not None
                and not getattr(message.author, "bot", False)
                and hasattr(self, "is_singing_active_in_channel")
                and self.is_singing_active_in_channel(cid)
            ):
                if is_singing_stop_command(content(message)):
                    log.info("TRACE on_message: singing stop command received channel=%s author=%s", cid, a_id)
                    stopped = await self._stop_singing_from_message(message)
                    if stopped:
                        return

            if not channel_umigame_active and not channel_twenty_doors_busy and self._is_reply_investigate_request(message):
                target_message = await resolve_reference_message(message)
                if target_message:
                    user_instruction = self._extract_reply_investigate_instruction(message, fallback="")
                    action = detect_reply_action(user_instruction)
                    if action:
                        log.info(
                            "TRACE on_message: reply investigate trigger channel=%s author=%s action=%s target_message=%s",
                            channel_id(message),
                            a_id,
                            action,
                            getattr(target_message, "id", None),
                        )
                        await self._handle_reply_investigate(
                            message,
                            ref_msg=target_message,
                            request_mode=action,
                        )
                        return

            kind = _get_channel_kind(message)
            log.info("TRACE on_message: channel kind=%s channel=%s author=%s", kind, channel_id(message), a_id)

            bot_user_id = self.bot.user.id if self.bot.user else None
            log.info("TRACE on_message: resolved bot_user_id=%s", bot_user_id)
            try:
                log.info("TRACE on_message: before _maybe_capture_feedback channel=%s author=%s", channel_id(message), a_id)
                await self._maybe_capture_feedback(message, bot_user_id=bot_user_id)
                log.info("TRACE on_message: after _maybe_capture_feedback channel=%s author=%s", channel_id(message), a_id)
            except Exception as e:
                log.exception("feedback capture failed: %s", e)

            try:
                await self._maybe_schedule_img2chan_thread_post(message)
            except Exception as e:
                log.warning("img2chan auto post schedule skipped channel=%s author=%s err=%r", channel_id(message), a_id, e)

            rate_limited = False
            if kind != "none" and a_id != bot_user_id:
                log.info("TRACE on_message: before rate limit check channel=%s author=%s", channel_id(message), a_id)
                rate_limited = _is_rate_limited(self.channel_rate_limit_hits, message)
                log.info("TRACE on_message: after rate limit check channel=%s author=%s rate_limited=%s", channel_id(message), a_id, rate_limited)
            if rate_limited:
                log.info("TRACE on_message: rate limit drop channel=%s author=%s content=%r", channel_id(message), a_id, content(message))
                return

            log.info("TRACE on_message: before _is_force_response_trigger channel=%s author=%s", channel_id(message), a_id)
            force_response = await _is_force_response_trigger(message, bot_user_id)
            log.info("TRACE on_message: after _is_force_response_trigger channel=%s author=%s force_response=%s", channel_id(message), a_id, force_response)
            if force_response and cid is not None:
                _reset_other_channel_unreplied_count(self, cid)

            ignored, ignore_reason = _should_ignore(message, bot_user_id, force_response=force_response, cog=self)
            log.info(
                "TRACE on_message: after _should_ignore channel=%s author=%s ignored=%s reason=%s",
                channel_id(message), a_id, ignored, ignore_reason
            )
            if ignored:
                if kind != "none" and a_id != bot_user_id and not getattr(message.author, "bot", False):
                    append_context_message(self.channel_context_cache, message, bot_user_id=bot_user_id)
                return

            self._mark_human_activity(message)

            # 歌唱開始リクエスト判定（メンション等の強制返答トリガー時）
            if force_response and hasattr(self, "_handle_sing_request"):
                song_title = extract_song_title(content(message))
                if song_title:
                    log.info("TRACE on_message: singing start request channel=%s author=%s song=%r", cid, a_id, song_title)
                    handled = await self._handle_sing_request(message, song_title)
                    if handled:
                        return

            # ゲーム起動リクエスト：キューを経由せず即時処理
            if await self._is_twenty_doors_start_request(message):
                handled = await self._start_20doors_from_message(message)
                if handled:
                    return

            # ゲーム中チャンネル（うみがめ・20doors）：キューを通さず即時処理
            if channel_umigame_active or channel_twenty_doors_busy:
                await self._process_queued_message(message, force_response=force_response)
                return

            # --- 優先度付きキュー管理 ---
            priority = REPLY_PRIORITY_FORCE if force_response else REPLY_PRIORITY_NORMAL
            max_queue_size = cfg_int("REPLY_QUEUE_MAX_SIZE", 3)
            # メンション・返信は優先度が高いため、通常制限より多くのキューイングを許容する
            max_force_queue_size = max(max_queue_size * 3, 10)

            current_qsize = self._reply_queue.qsize()
            if force_response:
                if current_qsize >= max_force_queue_size:
                    log.info(
                        "TRACE on_message: priority queue full for force reply (size=%d/%d) drop channel=%s author=%s",
                        current_qsize, max_force_queue_size, channel_id(message), a_id,
                    )
                    return
            else:
                if self._queue_blocked or current_qsize >= max_queue_size:
                    self._queue_blocked = True
                    log.info(
                        "TRACE on_message: queue full/blocked (size=%d/%d) drop normal reply channel=%s author=%s",
                        current_qsize, max_queue_size, channel_id(message), a_id,
                    )
                    return

            log.info(
                "TRACE on_message: enqueue (priority=%d size=%d) channel=%s author=%s",
                priority, current_qsize + 1, channel_id(message), a_id,
            )

            bg_task = getattr(self, "_current_background_llm_task", None)
            if bg_task is not None and not bg_task.done():
                log.info("TRACE on_message: preempting background LLM task for user message channel=%s", channel_id(message))
                bg_task.cancel()

            self._reply_queue_seq = getattr(self, "_reply_queue_seq", 0) + 1
            await self._reply_queue.put((priority, self._reply_queue_seq, message, force_response))

        except Exception as e:
            log.exception("ollama chat failed: %s", e)
            if not getattr(message.author, "bot", False):
                with contextlib.suppress(Exception):
                    await message.reply("AI応答でエラーが発生しました。\n", mention_author=False)

    async def _process_queued_message(
        self,
        message: discord.Message,
        *,
        force_response: bool = False,
    ) -> None:
        """返答タスクの重処理部分。キューワーカーまたはゲームチャンネルの即時処理から呼び出される。"""
        a_id = author_id(message)
        cid = channel_id(message)
        channel_umigame_active = cid in self.umigame_states
        try:
            runtime: MessageRuntime | None = await self._get_or_build_runtime(message)
            if runtime is None:
                return
            try:
                await self.should_research_for_reply(runtime, message=message, force_response=force_response)
            except Exception as e:
                log.warning(
                    "TRACE on_message: reply research decision skipped channel=%s author=%s err=%r",
                    cid, a_id, e,
                )
            if self._should_ignore_non_twenty_doors_message(runtime):
                log.info(
                    "TRACE on_message: active 20doors unrelated message ignored channel=%s author=%s",
                    cid, a_id,
                )
                return
            if getattr(runtime, "is_twenty_doors_reply", False):
                log.info("TRACE on_message: active 20doors message channel=%s author=%s", cid, a_id)
                handled = await self._handle_active_20doors_message(message, runtime)
                log.info("TRACE on_message: active 20doors handled=%s channel=%s author=%s", handled, cid, a_id)
                if handled:
                    return
            if channel_umigame_active and not getattr(runtime, "is_umigame_reply", False):
                log.info(
                    "TRACE on_message: active umigame unrelated message ignored channel=%s author=%s",
                    cid, a_id,
                )
                return
            if getattr(runtime, "is_umigame_reply", False):
                log.info("TRACE on_message: active umigame message channel=%s author=%s", cid, a_id)
                handled = await self._handle_active_umigame_message(message, runtime)
                log.info("TRACE on_message: active umigame handled=%s channel=%s author=%s", handled, cid, a_id)
                return

            incoming_scores: dict[str, float] | None = None
            incoming_appraisal: str | None = None
            incoming_reaction: str = "NONE"
            emotion_update_scores: dict[str, float] | None = None
            emotion_update_appraisal: str | None = None
            if emotion_scoring_enabled():
                if cfg_bool("EMOTION_PREREPLY_LLM_ENABLED", False):
                    try:
                        model_getter = getattr(self, "_emotion_scorer_model_for_release", None)
                        if callable(model_getter):
                            model_name = str(model_getter() or "").strip()
                            if model_name:
                                runtime.utility_models_used.add(model_name)
                        incoming_scores, incoming_appraisal, incoming_reaction = await asyncio.wait_for(
                            self._score_emotion_with_appraisal(runtime),
                            timeout=self._emotion_prereply_timeout_sec(),
                        )
                        emotion_update_scores = incoming_scores
                        emotion_update_appraisal = incoming_appraisal
                    except asyncio.TimeoutError:
                        log.warning("TRACE on_message: pre-reply emotion scoring timed out channel=%s author=%s", cid, a_id)
                    except Exception as e:
                        log.warning("TRACE on_message: pre-reply emotion scoring failed channel=%s author=%s err=%r", cid, a_id, e)
                if incoming_scores is None:
                    fallback_emotion = getattr(self, "_heuristic_emotion_with_appraisal_for_text", None)
                    if callable(fallback_emotion):
                        text = runtime.effective_user_text or runtime.original_user_text or ""
                        incoming_scores, incoming_appraisal, incoming_reaction = fallback_emotion(text)
                        
                        # We intentionally DO NOT set emotion_update_scores = incoming_scores here.
                        # Setting it would cause synchronous _update_emotion_state to run and set emotion_already_updated=True,
                        # which skips the background LLM emotion scoring (and thus prevents emoji reactions from being generated).
                        # By leaving emotion_update_scores as None, we allow the background task to perform the full LLM evaluation.

            emotion_already_updated = False
            if emotion_update_scores is not None:
                try:
                    await self._update_emotion_state(
                        runtime,
                        incoming_scores=emotion_update_scores,
                        incoming_appraisal=emotion_update_appraisal,
                    )
                    emotion_already_updated = True
                except Exception as e:
                    log.warning("sync emotion update failed: %r", e)
            try:
                await self._maybe_finalize_pending_habit_turn(
                    runtime,
                    incoming_emotion_scores=incoming_scores,
                )
            except Exception as e:
                log.warning("habit reward update skipped: %r", e)

            log.info("TRACE on_message: before _maybe_add_scored_reaction channel=%s author=%s", cid, a_id)
            try:
                await asyncio.wait_for(
                    self._maybe_add_scored_reaction(message, incoming_reaction),
                    timeout=cfg_float("EMOTION_REACTION_TIMEOUT_SEC", 5.0),
                )
            except asyncio.TimeoutError:
                log.warning(
                    "TRACE on_message: _maybe_add_scored_reaction timed out channel=%s author=%s",
                    cid, a_id,
                )
            except Exception as e:
                log.warning(
                    "TRACE on_message: _maybe_add_scored_reaction skipped channel=%s author=%s err=%r",
                    cid, a_id, e,
                )
            log.info("TRACE on_message: after _maybe_add_scored_reaction channel=%s author=%s", cid, a_id)

            log.info("TRACE on_message: before _send_contextual_reaction channel=%s author=%s", cid, a_id)
            await self._send_contextual_reaction(message, runtime=runtime, incoming_emotion_scores=incoming_scores)
            log.info("TRACE on_message: after _send_contextual_reaction channel=%s author=%s", cid, a_id)

            if not emotion_already_updated:
                self._schedule_emotion_update(
                    message,
                    runtime=runtime,
                    incoming_scores=emotion_update_scores,
                    incoming_appraisal=emotion_update_appraisal,
                    incoming_reaction=incoming_reaction,
                )
        except Exception as e:
            log.exception("_process_queued_message failed channel=%s author=%s: %s", cid, a_id, e)
            if not getattr(message.author, "bot", False):
                with contextlib.suppress(Exception):
                    await message.reply("AI応答でエラーが発生しました。\n", mention_author=False)

    async def _reply_queue_worker(self) -> None:
        """優先度付きキューから返答タスクを優先度順・到着順に取り出して処理するワーカーループ。"""
        log.info("reply queue worker started")
        while True:
            try:
                priority, _seq, message, force_response = await self._reply_queue.get()
                log.info(
                    "TRACE queue worker: dequeue (priority=%d remaining=%d) channel=%s author=%s",
                    priority, self._reply_queue.qsize(), channel_id(message), author_id(message),
                )
                try:
                    await self._process_queued_message(message, force_response=force_response)
                finally:
                    self._reply_queue.task_done()
                    # 通常キュー上限未満になったらブロック解除
                    if self._queue_blocked and self._reply_queue.qsize() < cfg_int("REPLY_QUEUE_MAX_SIZE", 3):
                        self._queue_blocked = False
                        log.info("reply queue drained below limit: unblocking new tasks")
            except asyncio.CancelledError:
                log.info("reply queue worker cancelled")
                break
            except Exception as e:
                log.exception("reply queue worker unexpected error: %s", e)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        first_ready = not self.topic_started
        if first_ready:
            self.topic_started = True
            await self._load_emotion_state()
            await self._load_user_relationships()
            if self.emotion_state.last_human_message_ts > 0:
                self._last_managed_human_message_ts = float(self.emotion_state.last_human_message_ts)
            else:
                self._last_managed_human_message_ts = time.time()
            try:
                await self._bootstrap_emotion_base_name()
                await self._sync_emotion_presence()
            except Exception as e:
                log.warning("initial emotion presence sync failed: %r", e)
            asyncio.create_task(self._sync_all_guild_members())
        self._ensure_background_tasks_started()

    async def _record_message_author_profile(self, message: discord.Message) -> None:
        """メッセージ発言者の最新プロファイル（表示名・ロール等）をDBに記録する。"""
        if message.guild is None or not hasattr(self, "memory_store"):
            return
        author = message.author
        if not isinstance(author, (discord.Member, discord.User)):
            return

        roles: list[str] = []
        if isinstance(author, discord.Member):
            roles = [r.name for r in author.roles if r.name != "@everyone"]

        avatar_url = str(author.display_avatar.url) if hasattr(author, "display_avatar") and author.display_avatar else None
        joined_at = author.joined_at.isoformat() if hasattr(author, "joined_at") and author.joined_at else None
        created_at = author.created_at.isoformat() if hasattr(author, "created_at") and author.created_at else None

        await self.memory_store.upsert_user_profile(
            guild_id=message.guild.id,
            user_id=author.id,
            current_name=author.name,
            current_display_name=author.display_name,
            roles=roles,
            avatar_url=avatar_url,
            joined_at=joined_at,
            created_at=created_at,
        )

    async def _sync_all_guild_members(self) -> None:
        """起動時に全ギルドのメンバー情報を走査してプロファイルを初期同期する（バックグラウンド非同期タスク）。"""
        await self.bot.wait_until_ready()
        await asyncio.sleep(2.0)
        try:
            for guild in self.bot.guilds:
                for member in guild.members:
                    if getattr(member, "bot", False):
                        continue
                    try:
                        roles = [r.name for r in member.roles if r.name != "@everyone"]
                        avatar_url = str(member.display_avatar.url) if member.display_avatar else None
                        joined_at = member.joined_at.isoformat() if member.joined_at else None
                        created_at = member.created_at.isoformat() if member.created_at else None
                        await self.memory_store.upsert_user_profile(
                            guild_id=guild.id,
                            user_id=member.id,
                            current_name=member.name,
                            current_display_name=member.display_name,
                            roles=roles,
                            avatar_url=avatar_url,
                            joined_at=joined_at,
                            created_at=created_at,
                        )
                    except Exception as e:
                        log.debug("failed to sync member %s in guild %s: %r", member.id, guild.id, e)
            log.info("finished background guild members profile sync")
        except Exception as e:
            log.warning("guild members profile sync task failed: %r", e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """メンバーのニックネームやロール変更を検知してエイリアス・プロファイルを自動更新する。"""
        if getattr(after, "bot", False) or not hasattr(self, "memory_store"):
            return
        try:
            old_disp = before.display_name
            new_disp = after.display_name
            old_nick = before.nick
            new_nick = after.nick

            new_alias = None
            if old_disp != new_disp:
                new_alias = old_disp
            elif old_nick and old_nick != new_nick:
                new_alias = old_nick

            roles = [r.name for r in after.roles if r.name != "@everyone"]
            avatar_url = str(after.display_avatar.url) if after.display_avatar else None
            joined_at = after.joined_at.isoformat() if after.joined_at else None
            created_at = after.created_at.isoformat() if after.created_at else None

            await self.memory_store.upsert_user_profile(
                guild_id=after.guild.id,
                user_id=after.id,
                current_name=after.name,
                current_display_name=after.display_name,
                new_alias=new_alias,
                roles=roles,
                avatar_url=avatar_url,
                joined_at=joined_at,
                created_at=created_at,
            )
            log.info(
                "user profile updated on_member_update: guild=%s user=%s name=%s display=%s new_alias=%s",
                after.guild.id, after.id, after.name, after.display_name, new_alias,
            )
        except Exception as e:
            log.warning("on_member_update profile update failed: %r", e)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        """ユーザーのグローバル名やアカウント名変更を検知して更新する。"""
        if getattr(after, "bot", False) or not hasattr(self, "memory_store"):
            return
        try:
            old_name = before.name
            new_name = after.name
            old_global = getattr(before, "global_name", None) or old_name
            new_global = getattr(after, "global_name", None) or new_name

            new_alias = old_global if old_global != new_global else (old_name if old_name != new_name else None)

            avatar_url = str(after.display_avatar.url) if after.display_avatar else None
            created_at = after.created_at.isoformat() if after.created_at else None

            for guild in self.bot.guilds:
                if guild.get_member(after.id) is not None:
                    member = guild.get_member(after.id)
                    roles = [r.name for r in member.roles if r.name != "@everyone"] if member else []
                    disp = member.display_name if member else new_global
                    await self.memory_store.upsert_user_profile(
                        guild_id=guild.id,
                        user_id=after.id,
                        current_name=after.name,
                        current_display_name=disp,
                        new_alias=new_alias,
                        roles=roles,
                        avatar_url=avatar_url,
                        created_at=created_at,
                    )
            log.info("user profile updated on_user_update: user=%s name=%s new_alias=%s", after.id, after.name, new_alias)
        except Exception as e:
            log.warning("on_user_update profile update failed: %r", e)
