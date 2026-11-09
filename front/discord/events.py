"""Discordイベントの入口、メッセージキュー、反応、背景タスク起動を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
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
try:
    from lib.error_report import report_error
except ImportError:
    def report_error(site: str, exc: BaseException) -> None: pass


from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_float, cfg_int, cfg_int_set, cfg_managed_channel_ids, cfg_persona_namespace, cfg_primary_channel_id
from harness.agent.prompt_parser.parser import habit_policy
from front.discord.common.bot_identity import resolve_bot_identity
from front.discord.common.discord_helpers import append_context_message
from front.discord.common.message_media import message_has_image
from ollama_bot.common.emotion_helpers import emotion_scoring_enabled
from harness.agent.prompt_parser.prompt_manager.history_helpers import (
    find_recent_bot_and_user_pair,
    safe_message_content,
)
from harness.agent.tool_registry.singing_helpers import extract_song_title, is_singing_stop_command
from ollama_bot.ollama_chat_.ollama_chat_helpers import _cached_other_channel_response_allowed, _get_channel_kind, _infer_feedback_type, _is_force_response_trigger, _is_rate_limited, _looks_like_negative_habit_followup, _looks_like_reactionish_followup, _record_other_channel_message, _reset_other_channel_message_count, _should_ignore, _user_valence_improved_for_habit, looks_like_feedback_text
from harness.agent.prompt_parser.parser.reply_helpers import (
    _format_reply,
)
from harness.agent.prompt_parser.prompt_manager.context_helpers import (
    _find_last_assistant_message_text,
    _check_context_flow,
)
from ollama_bot.common.ollama_helpers import extract_first_user_facing_reply, sanitize_generated_reply, truncate_text, looks_like_abnormal_assistant_reply, looks_like_parrot_reply, release_ollama_model
from front.discord.common.discord_helpers import author_id, channel_id, content, resolve_reference_message
from front.discord.utils import clean_user_input_text
from lib.dispatch_utils import get_public_attr
from harness.agent.memory_manager.summarizer import maybe_update_channel_summary
try:
    from harness.agent.memory_manager.logic import (
        extract_and_store_memory,
        maybe_extract_passive_channel_memory,
    )
except ImportError:
    try:
        from harness.agent.memory_manager.extractor import (
            extract_and_store_memory,
            maybe_extract_passive_channel_memory,
        )
    except ImportError:
        extract_and_store_memory = None  # type: ignore[assignment]
        maybe_extract_passive_channel_memory = None  # type: ignore[assignment]
try:
    import harness.agent.memory_manager.logic as _memory_logic
    mark_prospective_triggered = getattr(_memory_logic, "mark_prospective_triggered", None)
    list_due_prospective_memories = getattr(_memory_logic, "list_due_prospective_memories", None)
except ImportError:
    mark_prospective_triggered = None
    list_due_prospective_memories = None
try:
    from lib.date_utils import is_in_quiet_window
except ImportError:
    def is_in_quiet_window(now: Any, start_hhmm: str, end_hhmm: str) -> bool:  # type: ignore[misc]
        if not start_hhmm or not end_hhmm or start_hhmm == end_hhmm:
            return False
        try:
            sh, sm = (int(x) for x in start_hhmm.strip().split(":", 1))
            eh, em = (int(x) for x in end_hhmm.strip().split(":", 1))
            start_m = sh * 60 + sm
            end_m = eh * 60 + em
            from datetime import timezone, timedelta
            jst = timezone(timedelta(hours=9))
            target_dt = now.astimezone(jst) if getattr(now, "tzinfo", None) else now.replace(tzinfo=jst)
            cur_m = target_dt.hour * 60 + target_dt.minute
            if start_m < end_m:
                return start_m <= cur_m < end_m
            else:
                return cur_m >= start_m or cur_m < end_m
        except Exception:
            return False
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import update_user_interests
from ollama_bot.common.json_compat import dumps as json_dumps
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState, MessageRuntime, UserRelationship
from front.discord.research_events import _ResearchEventMixin
from ollama_bot.ollama_chat_.events.img2chan import _Img2chanEventMixin

log = logging.getLogger("ollama_bot.ollama_chat_.events")

_PUBLIC_REPLY_MODULE = "front.discord.reply.reply"


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _OllamaChatEventsBase = OllamaChatProtocol
else:
    _OllamaChatEventsBase = object


REPLY_PRIORITY_FORCE = 1   # メンション / 返信（最優先）

REPLY_PRIORITY_NORMAL = 5  # 通常チャンネル発話

_PASSIVE_MEMORY_CUE_PATTERNS = re.compile(
    r'(?:'
    r'食べ(?:たい|よう|た|てる|ます|る|行(?:く|こう|った))|'
    r'飲(?:みたい|もう|んだ|んでる)|'
    r'飯|ごはん|昼飯|夕飯|朝飯|ステーキ|ラーメン|寿司|お刺身|刺身|そば|蕎麦|うどん|カレー|パン|パスタ|肉|魚|ピザ|スイーツ|お菓子|デザート|晩酌|酒|'
    r'今日(?:は|も)|明日(?:は|も)|今から|これから|後で|あとで|'
    r'買っ(?:た|てきた|ちゃった|たよ)|買おう|'
    r'行く(?:か|予定|つもり|ぞ)?|行っ(?:てくる|てきた|た)|出かけ(?:る|てくる)|帰っ(?:た|てきた|てる)|'
    r'見(?:た|てくる|てる)|観(?:た|てくる|てる)|遊(?:んだ|んでる)|'
    r'始め(?:た|る)|終わっ(?:た|たよ)|作っ(?:た|てる)|やっ(?:た|てる)|'
    r'して(?:る|きた|おく|みよう)|しようかな|することにした|決めた|'
    r'好(?:き|物)|嫌い|苦手|欲しい|ハマっ(?:てる|た)|推し|気になっ(?:てる|た)|予定|計画'
    r')'
)


def _looks_like_passive_memory_cue(text: str) -> bool:
    clean = re.sub(r"<@!?\d+>", "", str(text or "")).strip()
    clean = re.sub(r"https?://\S+", "", clean).strip()
    clean = re.sub(r"<a?:\w+:\d+>", "", clean).strip()
    meaningful = re.sub(r"[\s\[\]【】、。！？!?,.〜～…「」『』\"\'“”\(\)（）\-_/\\#@:;~wW草笑]+", "", clean)
    if len(meaningful) < 4:
        return False
    return bool(_PASSIVE_MEMORY_CUE_PATTERNS.search(clean))


TIER_GAME_START_PATTERNS = (
    "ティア表",
    "ティアメーカー",
    "ティアリスト",
    "tier表",
    "tier list",
    "tierlist",
    "tiergame",
    "tier_game",
    "tier",
)

TIER_GAME_START_INTENT_PATTERNS = (
    "しよう",
    "やろう",
    "やる",
    "遊ぼう",
    "遊ぶ",
    "開始",
    "スタート",
    "start",
    "募集",
    "やりたい",
    "プレイ",
)


def _looks_like_tier_game_start_text(text: str) -> bool:
    lowered = str(text or "").lower()
    cleaned = re.sub(r"^[!！\s]+", "", lowered)
    compact = re.sub(r"\s+", "", cleaned)
    if not any(pattern in lowered for pattern in TIER_GAME_START_PATTERNS):
        return False
    if compact in TIER_GAME_START_PATTERNS or compact in (
        "ティア表ゲーム",
        "ティアゲーム",
        "tier表ゲーム",
        "tiergame",
        "tier_game",
    ):
        return True
    return any(pattern in lowered for pattern in TIER_GAME_START_INTENT_PATTERNS)


class OllamaChatEventMixin(_ResearchEventMixin, _Img2chanEventMixin, _OllamaChatEventsBase):
    def _emotion_prereply_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PREREPLY_TIMEOUT_SEC", 2.0), 0.5)

    def _emotion_presence_sync_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PRESENCE_SYNC_TIMEOUT_SEC", 15.0), 5.0)

    def _emotion_update_timeout_sec(self, *, needs_scoring: bool) -> float:
        configured = cfg_float("EMOTION_UPDATE_TIMEOUT_SEC", 0.0)
        if configured > 0.0:
            return configured
        configured_reason = cfg_float("EMOTION_REASON_TIMEOUT_SEC", 15.0)
        reason_timeout = min(configured_reason, 15.0) if configured_reason > 0 else 15.0
        if needs_scoring:
            configured_score = cfg_float("EMOTION_SCORE_TIMEOUT_SEC", 25.0)
            score_timeout = min(configured_score, 25.0) if configured_score > 0 else 25.0
            return score_timeout + reason_timeout + 5.0
        return reason_timeout + 5.0

    def _is_reaction_blocked_error(self, exc: Exception) -> bool:
        if not isinstance(exc, discord.Forbidden):
            return False
        code = getattr(exc, "code", None)
        if code == 90001:
            return True
        return "Reaction blocked" in str(exc)

    async def _safe_add_reaction(self, message: discord.Message, emoji: str) -> bool:
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            if self._is_reaction_blocked_error(e):
                log.info(
                    "reaction blocked; skip add_reaction channel=%s message=%s emoji=%r",
                    channel_id(message), getattr(message, "id", None), emoji
                )
                return False
            log.exception("add reaction failed: %s", e)
            return False

    async def _safe_remove_own_reaction(self, message: discord.Message, emoji: str) -> bool:
        if not self.bot.user:
            return False
        try:
            await message.remove_reaction(emoji, self.bot.user)
            return True
        except Exception as e:
            if self._is_reaction_blocked_error(e):
                log.info(
                    "reaction blocked; skip remove_reaction channel=%s message=%s emoji=%r",
                    channel_id(message), getattr(message, "id", None), emoji
                )
                return False
            log.exception("remove reaction failed: %s", e)
            return False

    def _record_unreplied_message(self, message: discord.Message, *, bot_user_id: int | None) -> None:
        """返信を行わない（ドロップ/無視/レート制限）メッセージをコンテキストキャッシュとスレッドトラッカーに記録する。"""
        cid = channel_id(message)
        a_id = author_id(message)
        if cid is not None and a_id != bot_user_id and not getattr(message.author, "bot", False):
            append_context_message(self.channel_context_cache, message, bot_user_id=bot_user_id)
            if hasattr(self, "thread_tracker") and self.thread_tracker:
                self.thread_tracker.record_message(
                    message,
                    bot_user_id=bot_user_id,
                    bot_identity=resolve_bot_identity(bot=self.bot, message=message),
                )

    def _maybe_trigger_passive_channel_maintenance(self, message: discord.Message) -> None:
        """非返信ターンにおいて、メッセージ蓄積が一定数に達した場合にバックグラウンドで要約・関心事・スレッドを更新する。"""
        cid = channel_id(message)
        if cid is None or getattr(message.author, "bot", False):
            return

        trigger = cfg_int("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20)
        if trigger <= 0:
            return

        counts = getattr(self, "_passive_channel_message_counts", None)
        if counts is None or not isinstance(counts, dict):
            counts = {}
            setattr(self, "_passive_channel_message_counts", counts)

        count = counts.get(cid, 0) + 1
        counts[cid] = count

        if count % trigger == 0:
            log.info(
                "triggering passive channel maintenance for channel=%s (unreplied_count=%d trigger=%d)",
                cid, count, trigger,
            )
            bg_task = asyncio.create_task(self._run_passive_channel_maintenance(message))

            def _on_done(t: asyncio.Task[Any]) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = t.exception()
                    if exc:
                        log.warning("passive channel maintenance failed: %r", exc)
                        report_error("events.passive_channel_maintenance", exc)

            bg_task.add_done_callback(_on_done)

    async def _run_passive_channel_maintenance(self, message: discord.Message) -> None:
        cid = channel_id(message)
        if cid is None:
            return
        store = getattr(self, "memory_store", None)
        if store is None:
            return

        # 1. チャンネル文脈の取得
        cache = getattr(self, "channel_context_cache", None)
        context_lines: list[str] = []
        if cache and cid in cache:
            raw_items = list(cache[cid])
            context_lines = [
                str(item.get("line") if isinstance(item, dict) else item).strip()
                for item in raw_items
                if str(item.get("line") if isinstance(item, dict) else item).strip()
            ]

        # 2. チャンネル要約の更新
        try:
            update_summary_fn = _public_attr("maybe_update_channel_summary", maybe_update_channel_summary)
            await update_summary_fn(
                cfg=cfg,
                store=store,
                guild_id=getattr(message.guild, "id", None),
                channel_id=cid,
                context_lines=context_lines,
            )
        except Exception as e:
            log.warning("passive channel summary update failed: %s", e)

        # 3. ユーザー関心事の更新
        uid = author_id(message)
        user_text = clean_user_input_text(content(message))
        if uid is not None and user_text:
            try:
                update_interests_fn = _public_attr("update_user_interests", update_user_interests)
                await update_interests_fn(
                    cfg=cfg,
                    store=store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=cid,
                    user_id=uid,
                    user_text=user_text,
                    context_lines=context_lines[-8:] if context_lines else None,
                )
            except Exception as e:
                log.warning("passive user interests update failed: %s", e)

        # 4. アクティブスレッドのDB保存
        tracker = getattr(self, "thread_tracker", None)
        save_thread = getattr(store, "save_channel_thread", None)
        if tracker and callable(save_thread):
            try:
                persona_ns = cfg_persona_namespace(custom_cfg=cfg)
                get_active_threads_fn = getattr(tracker, "get_active_threads", None)
                active_threads = get_active_threads_fn(cid) if callable(get_active_threads_fn) else []
                for th in active_threads:
                    await save_thread(
                        persona=persona_ns,
                        guild_id=getattr(message.guild, "id", None),
                        channel_id=cid,
                        thread_key=getattr(th, "thread_id", str(th)),
                        topic=getattr(th, "topic", ""),
                        participants=getattr(th, "participant_names", []),
                        turns=list(getattr(th, "turns", [])),
                        last_active_ts=getattr(th, "last_active_ts", None),
                    )
            except Exception as e:
                log.warning("passive channel threads persistence failed: %s", e)

    def _maybe_schedule_passive_memory_update(self, message: discord.Message, cid: int) -> None:
        """返信見送り時（ignored=True等）に、未返信発言からの受動的長期記憶抽出・要約更新タスクをスケジュールする。"""
        if not cfg_bool("MEMORY_PASSIVE_EXTRACTION_ENABLED", True):
            return
        if getattr(self, "memory_store", None) is None:
            return
        if cid is None or getattr(message.author, "bot", False):
            return

        user_text = clean_user_input_text(content(message))
        if not user_text:
            return

        counts = getattr(self, "_passive_channel_message_counts", None)
        if counts is None or not isinstance(counts, dict):
            counts = {}
            setattr(self, "_passive_channel_message_counts", counts)

        count = counts.get(cid, 0)
        interval = cfg_int("MEMORY_PASSIVE_SUMMARY_INTERVAL", 15)
        is_interval_hit = bool(interval > 0 and count > 0 and count % interval == 0)
        is_cue = _looks_like_passive_memory_cue(user_text)

        if not (is_interval_hit or is_cue):
            return

        log.info(
            "scheduling passive memory update for channel=%s (count=%d interval_hit=%s cue_hit=%s)",
            cid, count, is_interval_hit, is_cue,
        )
        bg_task = asyncio.create_task(
            self._run_passive_memory_update(
                message,
                cid,
                user_text=user_text,
                should_update_summary=is_interval_hit,
            )
        )

        def _on_done(t: asyncio.Task[Any]) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = t.exception()
                if exc:
                    log.warning("passive memory update failed: %r", exc)
                    report_error("events._run_passive_memory_update", exc)

        bg_task.add_done_callback(_on_done)

    async def _run_passive_memory_update(
        self,
        message: discord.Message,
        cid: int,
        *,
        user_text: str,
        should_update_summary: bool = False,
    ) -> None:
        store = getattr(self, "memory_store", None)
        if store is None:
            return

        # ユーザー返信最優先譲歩（Yield）
        yield_fn = getattr(self, "_yield_to_user_replies", None)
        if callable(yield_fn):
            await yield_fn()

        bg_lock = getattr(self, "_bg_llm_lock", None)
        if bg_lock is None:
            bg_lock = asyncio.Lock()
            setattr(self, "_bg_llm_lock", bg_lock)

        async with bg_lock:
            if callable(yield_fn):
                await yield_fn()

            cache = getattr(self, "channel_context_cache", None)
            context_lines: list[str] = []
            if cache and cid in cache:
                raw_items = list(cache[cid])
                context_lines = [
                    str(item.get("line") if isinstance(item, dict) else item).strip()
                    for item in raw_items
                    if str(item.get("line") if isinstance(item, dict) else item).strip()
                ]

            uid = author_id(message)
            user_disp = str(getattr(message.author, "display_name", "") or getattr(message.author, "name", "") or "")
            user_name = getattr(message.author, "name", "")
            guild_id = getattr(message.guild, "id", None)

            # 1. 受動的長期記憶抽出
            try:
                extract_fn = _public_attr("maybe_extract_passive_channel_memory", maybe_extract_passive_channel_memory)
                if callable(extract_fn):
                    await extract_fn(
                        cfg=cfg,
                        store=store,
                        guild_id=guild_id,
                        channel_id=cid,
                        user_id=uid,
                        user_text=user_text,
                        context_lines=context_lines[-10:] if context_lines else [],
                        user_name=user_name,
                        user_display_name=user_disp,
                    )
                else:
                    extract_store_fn = _public_attr("extract_and_store_memory", extract_and_store_memory)
                    if callable(extract_store_fn):
                        await extract_store_fn(
                            cfg=cfg,
                            store=store,
                            guild_id=guild_id,
                            channel_id=cid,
                            user_id=uid,
                            user_text=user_text,
                            assistant_text="",
                            context_lines=context_lines[-10:] if context_lines else [],
                            user_name=user_name,
                            user_display_name=user_disp,
                        )
            except Exception as e:
                log.warning("passive memory extraction failed: %r", e)
                report_error("events._run_passive_memory_update", e)

            # 2. チャンネル要約の更新
            if should_update_summary:
                try:
                    update_summary_fn = _public_attr("maybe_update_channel_summary", maybe_update_channel_summary)
                    if callable(update_summary_fn):
                        await update_summary_fn(
                            cfg=cfg,
                            store=store,
                            guild_id=guild_id,
                            channel_id=cid,
                            context_lines=context_lines,
                        )
                except Exception as e:
                    log.warning("passive channel summary update failed: %r", e)
                    report_error("events._run_passive_memory_update", e)

    async def _maybe_capture_feedback(self, message: discord.Message, *, bot_user_id: int | None) -> None:
        if bot_user_id is None or getattr(message.author, "bot", False):
            return
        feedback_text = content(message)
        if not looks_like_feedback_text(feedback_text):
            return

        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id = getattr(message.guild, "id", None)
        cid = channel_id(message)
        uid = author_id(message)
        try:
            bot_msg, prev_user_msg = await find_recent_bot_and_user_pair(
                message,
                bot_user_id=bot_user_id,
                limit=12,
            )
        except Exception as e:
            log.exception("feedback history fetch failed: %s", e)
            return

        if bot_msg is None:
            return

        assistant_text = safe_message_content(bot_msg)
        if not assistant_text:
            return

        try:
            feedback_id = await self.memory_store.add_feedback_item(
                persona=persona,
                guild_id=guild_id,
                channel_id=cid,
                user_id=uid,
                original_user_text=safe_message_content(prev_user_msg),
                assistant_text=assistant_text,
                feedback_text=feedback_text,
                corrected_assistant_text=None,
                feedback_type=_infer_feedback_type(feedback_text),
                accepted=False,
            )
            log.info("feedback item saved: id=%s persona=%s channel=%s user=%s", feedback_id, persona, cid, uid)
        except Exception as e:
            log.exception("feedback save failed: %s", e)

    def _schedule_emotion_update(
        self,
        message: discord.Message,
        *,
        runtime: MessageRuntime | None = None,
        incoming_scores: dict[str, float] | None = None,
        incoming_appraisal: str | None = None,
        incoming_reaction: str | None = None,
    ) -> None:
        try:
            cid = channel_id(message)
            uid = author_id(message)
            counters = getattr(self, "_emotion_score_counters", None)
            if counters is None:
                counters = {}
                self._emotion_score_counters = counters

            interval = max(cfg_int("EMOTION_LLM_SCORE_INTERVAL", 3), 1)
            skip_peak = cfg_float("EMOTION_LLM_SCORE_SKIP_PEAK", 0.20)
            key = (cid, uid)
            count = counters.get(key, 0) + 1
            counters[key] = count

            # incoming_scores が未指定の場合は runtime / message からヒューリスティック評価
            eval_scores = incoming_scores
            if eval_scores is None:
                text = ""
                if runtime is not None:
                    text = str(getattr(runtime, "effective_user_text", "") or getattr(runtime, "original_user_text", "") or "")
                if not text and message is not None:
                    text = str(content(message) or "")
                if text:
                    heuristic_fn = getattr(self, "_heuristic_emotion_with_appraisal_for_text", None)
                    if callable(heuristic_fn):
                        try:
                            eval_scores, _, _ = heuristic_fn(text)
                        except Exception:
                            pass

            peak_val = max(eval_scores.values()) if eval_scores else 0.0
            should_llm_score = (count % interval == 0) and (peak_val >= skip_peak)

            budget = getattr(runtime, "llm_budget", None)
            if not isinstance(budget, dict):
                budget = getattr(self, "_current_turn_llm_budget", None)
            if isinstance(budget, dict):
                budget["emotion_score"] = 1 if should_llm_score else 0

            # LLM採点を行わない場合は、ヒューリスティック感情スコアをそのまま渡してPhase 1/同期反映
            # LLM採点を行う場合は、Phase 2 LLMアプレイザルが起動するよう incoming_scores を None にする
            target_scores = incoming_scores if not should_llm_score else None

            task = asyncio.create_task(
                self._run_emotion_update_for_message(
                    message,
                    runtime=runtime,
                    incoming_scores=target_scores,
                    incoming_appraisal=incoming_appraisal,
                    incoming_reaction=incoming_reaction,
                    llm_scoring=should_llm_score,
                )
            )
            log.info(
                "TRACE emotion: task scheduled channel=%s author=%s llm_scoring=%s (count=%d interval=%d peak=%.2f)",
                channel_id(message), author_id(message), should_llm_score, count, interval, peak_val,
            )

            def _done_callback(t: asyncio.Task[Any]) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = t.exception()
                    if exc is not None:
                        log.exception("TRACE emotion: task failed channel=%s author=%s err=%r", channel_id(message), author_id(message), exc)
                        report_error("events.emotion_task_callback", exc)

            task.add_done_callback(_done_callback)
        except Exception as e:
            log.exception("TRACE emotion: task schedule failed channel=%s author=%s err=%r", channel_id(message), author_id(message), e)
            report_error("events.emotion_schedule", e)

    def _log_llm_call_budget(self, runtime: MessageRuntime | None = None) -> None:
        """1ターンの返信処理完了時に、用途別LLM呼び出し回数を1行で出力する。"""
        if runtime is not None and getattr(runtime, "_llm_budget_logged", False):
            return
        if runtime is not None:
            setattr(runtime, "_llm_budget_logged", True)

        budget = getattr(runtime, "llm_budget", None)
        if not isinstance(budget, dict):
            budget = getattr(self, "_current_turn_llm_budget", None)
        if not isinstance(budget, dict):
            budget = {
                "analyzer": 1,
                "research_decision": 0,
                "emotion_score": 0,
                "main": 1,
                "memory": 1,
                "thought": 0,
            }
        log.info(
            "[LLM_CALL_BUDGET] turn calls: analyzer=%d research_decision=%d emotion_score=%d main=%d memory=%d thought=%d",
            int(budget.get("analyzer", 0)),
            int(budget.get("research_decision", 0)),
            int(budget.get("emotion_score", 0)),
            int(budget.get("main", 0)),
            int(budget.get("memory", 0)),
            int(budget.get("thought", 0)),
        )

    def _background_task_needs_restart(self, task: asyncio.Task | None) -> bool:
        if task is None:
            return True
        return bool(task.done())

    def _start_background_task_if_needed(
        self,
        attr_name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]] | None,
    ) -> None:
        if coro_factory is None or not callable(coro_factory):
            return
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
        if cfg_bool("HUTABA_PROACTIVE_POST_ENABLED", True):
            self._start_background_task_if_needed(
                "_img2chan_proactive_post_task",
                getattr(self, "_img2chan_proactive_post_loop", None),
            )
        self._start_background_task_if_needed("_game_pool_replenish_task", self._game_pool_replenish_loop)
        self._start_background_task_if_needed("_queue_worker_task", self._reply_queue_worker)
        if cfg_bool("PROSPECTIVE_FOLLOWUP_ENABLED", True):
            self._start_background_task_if_needed(
                "_prospective_followup_task",
                self._prospective_followup_loop,
            )

    async def _prospective_followup_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval_sec = max(float(cfg("PROSPECTIVE_FOLLOWUP_INTERVAL_SEC", 900.0) or 900.0), 60.0)
        while not self.bot.is_closed():
            await asyncio.sleep(interval_sec)
            try:
                if not cfg_bool("PROSPECTIVE_FOLLOWUP_ENABLED", True):
                    continue

                # 1. ビジー状態の確認と譲歩
                is_busy_fn = getattr(self, "_is_user_reply_busy", None)
                if callable(is_busy_fn) and is_busy_fn():
                    log.info("_prospective_followup_loop: user reply is busy; yielding to next turn")
                    continue

                yield_fn = getattr(self, "_yield_to_user_replies", None)
                if callable(yield_fn):
                    await yield_fn()

                store = getattr(self, "memory_store", None)
                if store is None:
                    continue

                # 2. 期日到来レコードの取得
                persona = cfg_persona_namespace(custom_cfg=cfg) if callable(cfg_persona_namespace) else str(cfg("OLLAMA_BOT_PERSONA", "default") or "default")
                from datetime import datetime, timezone, timedelta
                now_iso = datetime.now(timezone(timedelta(hours=9))).isoformat()

                due_records: list[dict[str, Any]] = []
                list_due_fn = _public_attr("list_due_prospective_memories", list_due_prospective_memories)
                due_res = None
                if hasattr(store, "list_due_prospective_memories"):
                    due_res = store.list_due_prospective_memories(persona=persona, now_iso=now_iso, limit=20)
                elif callable(list_due_fn):
                    try:
                        due_res = list_due_fn(store, persona=persona, now_iso=now_iso, limit=20)
                    except TypeError:
                        due_res = list_due_fn(persona=persona, now_iso=now_iso, limit=20)

                if inspect.isawaitable(due_res):
                    due_records = (await due_res) or []
                elif isinstance(due_res, list):
                    due_records = due_res
                else:
                    due_records = []

                if not due_records:
                    continue

                # 3. チャンネルごとに1件
                channel_records: dict[int, dict[str, Any]] = {}
                for rec in due_records:
                    cid = rec.get("channel_id")
                    if cid and cid not in channel_records:
                        channel_records[cid] = rec

                for cid, record in channel_records.items():
                    if callable(is_busy_fn) and is_busy_fn():
                        log.info("_prospective_followup_loop: user reply became busy; aborting remaining channels")
                        break

                    channel = self.bot.get_channel(cid)
                    if channel is None:
                        try:
                            channel = await self.bot.fetch_channel(cid)
                        except Exception as e:
                            log.warning("_prospective_followup_loop: fetch_channel(%s) failed: %r", cid, e)
                            continue

                    if not hasattr(channel, "send"):
                        continue

                    record_id = record.get("id")
                    target_user_id = record.get("target_user_id")
                    intent = str(record.get("intent") or "")
                    cue_topic_tag = str(record.get("cue_topic_tag") or "")

                    user_display = "ユーザー"
                    if target_user_id and hasattr(self.bot, "get_user"):
                        u = self.bot.get_user(target_user_id)
                        if u:
                            user_display = str(getattr(u, "display_name", None) or getattr(u, "name", "ユーザー") or "ユーザー")

                    # 4. 人格システムプロンプトを用いて自発発話（フォローアップ、応援、労い、リマインド等）を生成
                    sys_prompt = self._reply_system_prompt() if hasattr(self, "_reply_system_prompt") else ""
                    prompt = (
                        f"【自発的な声掛け・フォローアップ発話の作成】\n"
                        f"{user_display}さんとの以前の会話や予定を踏まえ、自発的に話しかけるメッセージを1つ作成してください。\n"
                        f"・対象トピック: {cue_topic_tag or intent}\n"
                        f"・発話の目的・意図: {intent}\n\n"
                        f"【絶対遵守ルール】\n"
                        f"- 上記の「発話の目的・意図」や「対象トピック」の文面そのものを復唱・引用しないでください。\n"
                        f"- 発話の目的・意図（例: 結果を尋ねる、当日の応援、労い、リマインド、約束の話題など）に沿って、自然で親しみやすい会話文（1〜2文程度）にしてください。\n"
                        f"- 唐突感を和らげるため、「そういえば〜」「今日って〜」「この前の〜」など自然な前置きを適宜用いてください。\n"
                        f"- あなた自身のキャラクター設定と口調を厳格に維持してください。\n"
                    )

                    call_model_fn = getattr(self, "_call_model", None)
                    speech_text: str = ""
                    if callable(call_model_fn):
                        speech_text = await call_model_fn(prompt, system_prompt=sys_prompt)
                    else:
                        from ollama_bot.ollama_chat_.ollama_chat_helpers import call_ollama
                        model_name = str(cfg("OLLAMA_MODEL", "") or "").strip()
                        speech_text = await call_ollama(
                            prompt,
                            system_prompt=sys_prompt,
                            model=model_name,
                            temperature=float(cfg("OLLAMA_REPLY_TEMPERATURE", 0.4) or 0.4),
                        )

                    speech_text = sanitize_generated_reply(speech_text)
                    if looks_like_abnormal_assistant_reply(speech_text) or not speech_text.strip():
                        log.warning("_prospective_followup_loop: generated abnormal reply for record %s", record_id)
                        continue

                    # 5. 送信成功後に mark_prospective_triggered(record_id) を実行。失敗時は最大3回まで再試行。
                    send_success = False
                    for attempt in range(1, 4):
                        try:
                            await channel.send(speech_text)
                            send_success = True
                            break
                        except Exception as e:
                            log.warning("_prospective_followup_loop: channel.send failed (attempt %d/3) for record %s: %r", attempt, record_id, e)
                            if attempt < 3:
                                await asyncio.sleep(1.0 * attempt)

                    if send_success and record_id is not None:
                        mark_fn = _public_attr("mark_prospective_triggered", mark_prospective_triggered)
                        mark_res = None
                        if callable(mark_fn):
                            try:
                                mark_res = mark_fn(record_id)
                            except TypeError:
                                mark_res = mark_fn(store, record_id)
                        elif hasattr(store, "mark_prospective_triggered"):
                            mark_res = store.mark_prospective_triggered(record_id)
                        if inspect.isawaitable(mark_res):
                            await mark_res
                        log.info("_prospective_followup_loop: successfully sent followup and marked triggered for record %s in channel %s", record_id, cid)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("_prospective_followup_loop error: %r", e)
                report_error("events._prospective_followup_loop", e)

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
                    persona=cfg_persona_namespace(custom_cfg=cfg),
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
            persona=str(pending.get("persona") or cfg_persona_namespace(custom_cfg=cfg)),
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
        llm_scoring: bool = True,
    ) -> None:
        if not emotion_scoring_enabled():
            return

        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        from front.discord.common.bot_identity import is_message_addressed_to_other_bot
        if is_message_addressed_to_other_bot(message, bot_user_id=bot_user_id, bot=self.bot):
            log.debug("TRACE emotion: skipping update because message is addressed to another bot cid=%s", channel_id(message))
            return

        try:
            log.info("TRACE emotion: start channel=%s author=%s", channel_id(message), author_id(message))
            runtime_for_emotion = runtime or await asyncio.wait_for(self._get_or_build_runtime(message), timeout=20.0)
            log.info("TRACE emotion: runtime built channel=%s author=%s", channel_id(message), author_id(message))

            # 直前のアシスタント返答テキストを runtime_for_emotion に補完（Bot自身の怯え・驚き反応をヒューリスティックに反映するため）
            bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
            cache = getattr(self, "channel_context_cache", {})
            if not getattr(runtime_for_emotion, "assistant_text", "") and message is not None:
                try:
                    last_asst = await _find_last_assistant_message_text(
                        message,
                        bot_user_id=bot_user_id,
                        cache=cache,
                    )
                    if last_asst:
                        setattr(runtime_for_emotion, "assistant_text", last_asst)
                except Exception:
                    pass

            # Phase 1: 返信確定直後の即時ヒューリスティック感情更新（ゼロ遅延での感情遷移保証）
            if incoming_scores is None:
                try:
                    await self._apply_heuristic_emotion_immediate(runtime_for_emotion)
                except Exception as e:
                    log.warning("TRACE emotion: immediate heuristic update failed: %r", e)

            # 返信キュー待機チェック: 後続メッセージがある場合は重いLLM感情採点をスキップ
            reply_queue = getattr(self, "_reply_queue", None)
            queue_size = reply_queue.qsize() if reply_queue is not None else 0
            if queue_size > 0 and incoming_scores is None:
                log.info(
                    "TRACE emotion: skipping background LLM scoring due to pending reply queue (qsize=%d); heuristic applied channel=%s author=%s",
                    queue_size, channel_id(message), author_id(message),
                )
                return

            if not llm_scoring:
                log.info(
                    "TRACE emotion: skipping background LLM scoring due to interval/peak gate; heuristic applied channel=%s author=%s",
                    channel_id(message), author_id(message),
                )
                return

            bg_lock = getattr(self, "_bg_llm_lock", None)
            if bg_lock is None:
                bg_lock = asyncio.Lock()
                setattr(self, "_bg_llm_lock", bg_lock)

            state_timeout = self._emotion_update_timeout_sec(needs_scoring=incoming_scores is None)
            try:
                async with bg_lock:
                    reaction_result = await asyncio.wait_for(
                        self._update_emotion_state(
                            runtime_for_emotion,
                            incoming_scores=incoming_scores,
                            incoming_appraisal=incoming_appraisal,
                            incoming_reaction=incoming_reaction,
                        ),
                        timeout=state_timeout,
                    )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning(
                    "emotion state update timed out or cancelled: channel=%s author=%s needs_scoring=%s timeout=%.1f (heuristic retained dominant=%s)",
                    channel_id(message),
                    author_id(message),
                    incoming_scores is None,
                    state_timeout,
                    self.emotion_state.dominant_emotion,
                )
                return
            
            if reaction_result == "NONE" or reaction_result is None:
                log.info(
                    "TRACE emotion: state update skipped (reason=cancelled_or_none) channel=%s author=%s",
                    channel_id(message),
                    author_id(message),
                )
                return

            if reaction_result in ("GOOD", "BAD"):
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
            report_error("events._run_emotion_update_for_message", e)

    async def _is_tier_game_start_request(self, message: discord.Message) -> bool:
        text = str(content(message) or "")
        if not _looks_like_tier_game_start_text(text):
            return False

        bot_user_id = self.bot.user.id if self.bot.user else None
        if bot_user_id is None:
            return False
        if any(getattr(user, "id", None) == bot_user_id for user in (getattr(message, "mentions", None) or [])):
            return True

        ref_msg = await resolve_reference_message(message)
        if ref_msg and author_id(ref_msg) == bot_user_id:
            return True

        # 専用チャンネル（TIER_GAME_CHANNEL_ID）での発言であればメンションなしでも開始可能
        tier_channel_id = int(cfg("TIER_GAME_CHANNEL_ID", 0) or 0)
        if tier_channel_id > 0 and channel_id(message) == tier_channel_id:
            from front.discord.common.bot_identity import is_target_of_command
            return is_target_of_command(self.bot, message)

        return False

    async def _start_tier_game_from_message(self, message: discord.Message) -> bool:
        get_cog_fn = getattr(self.bot, "get_cog", None)
        if not callable(get_cog_fn):
            return False
        tier_cog = get_cog_fn("TierGameCog")
        if tier_cog is None:
            return False
        get_context_fn = getattr(self.bot, "get_context", None)
        if not callable(get_context_fn):
            return False
        try:
            ctx = await get_context_fn(message)
        except Exception:
            return False
        cmd = getattr(tier_cog, "cmd_tier_game", None)
        if callable(cmd):
            log.info(
                "TRACE on_message: invoking TierGameCog.cmd_tier_game from chat text channel=%s author=%s",
                channel_id(message),
                author_id(message),
            )
            await cmd(ctx)
            return True
        return False

    def _is_quiet_hours(self, now: Any = None) -> bool:
        """指定時刻（省略時は現在JST時刻）が静かな時間帯（BOT_QUIET_HOURS_ENABLED）に含まれるかを判定する。"""
        if not cfg_bool("BOT_QUIET_HOURS_ENABLED", False):
            return False
        start = str(cfg("BOT_QUIET_HOURS_START", "01:00") or "01:00").strip()
        end = str(cfg("BOT_QUIET_HOURS_END", "07:00") or "07:00").strip()
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        target_now = now or datetime.now(jst)
        return is_in_quiet_window(target_now, start, end)

    def _get_quiet_hours_delay(self) -> float:
        """静かな時間帯のメンション・直接リプライ用応答遅延時間（秒）を算出する。"""
        min_delay = max(0.0, float(cfg("BOT_QUIET_HOURS_MIN_DELAY_SEC", 2.0) or 2.0))
        max_delay = max(min_delay, float(cfg("BOT_QUIET_HOURS_MAX_DELAY_SEC", 5.0) or 5.0))
        if max_delay <= min_delay:
            return min_delay
        return random.uniform(min_delay, max_delay)

    def _reply_num_predict(
        self,
        intent_info: dict[str, Any] | None = None,
        *,
        prompt_text: str = "",
        system_prompt: str = "",
        think: bool = False,
    ) -> int | None:
        """返信生成の num_predict を決定する。静かな時間帯（quiet hours）は通常の半分に抑制する。"""
        base_fn = getattr(super(), "_reply_num_predict", None)
        if callable(base_fn):
            base_val = base_fn(
                intent_info=intent_info,
                prompt_text=prompt_text,
                system_prompt=system_prompt,
                think=think,
            )
        else:
            base_val = int(cfg("OLLAMA_REPLY_NUM_PREDICT", 256) or 256)

        if base_val is None:
            base_val = 256

        if self._is_quiet_hours():
            ratio = cfg_float("BOT_QUIET_HOURS_NUM_PREDICT_RATIO", 0.5)
            min_tokens = cfg_int("BOT_QUIET_HOURS_MIN_NUM_PREDICT", 64)
            reduced_val = max(min_tokens, int(base_val * ratio))
            quiet_max = cfg_int("BOT_QUIET_HOURS_MAX_NUM_PREDICT", 0)
            if quiet_max > 0:
                reduced_val = min(reduced_val, quiet_max)
            log.info("quiet hours num_predict reduced: %s -> %s (ratio=%.2f)", base_val, reduced_val, ratio)
            return reduced_val

        return base_val

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            # 1. 他BotおよびBot自身（bot=True）の発言は最上流で早期スキップ（自己ループ・他Botクロストーク防止）
            if getattr(message.author, "bot", False):
                log.debug(
                    "TRACE on_message: ignoring bot message cid=%s author=%s",
                    channel_id(message),
                    author_id(message),
                )
                return

            # 他Bot宛てのメッセージ（他Botへのメンション、他Botへの返信、他Bot名での呼びかけ）は最上流で完全遮断
            # （ログ汚染・混在防止、感情値・感情理由・Presenceの誤更新防止、不要返信の遮断）
            bot_user_id = self.bot.user.id if self.bot.user else None
            from front.discord.common.bot_identity import is_message_addressed_to_other_bot
            if is_message_addressed_to_other_bot(message, bot_user_id=bot_user_id, bot=self.bot):
                log.debug(
                    "TRACE on_message: message is addressed to another bot, skipping cid=%s author=%s",
                    channel_id(message),
                    author_id(message),
                )
                return

            a_id = author_id(message)
            ignored_user_ids = cfg_int_set("IGNORE_USER_IDS")
            if a_id in ignored_user_ids:
                log.debug("TRACE on_message: ignore user drop channel=%s author=%s", channel_id(message), a_id)
                return

            # 管理者テキストコマンド (!bot start / !bot stop / !bot status) の判定
            msg_text = content(message).strip()
            is_admin_cmd = bool(msg_text.startswith("!bot"))
            if is_admin_cmd:
                if hasattr(self, "_handle_admin_text_command"):
                    handled = await self._handle_admin_text_command(message)
                    if handled:
                        return

            # プレフィックスコマンド判定：有効なテキストコマンドならAI返信キューへの投入を抑止
            if msg_text.startswith("!") or msg_text.startswith("！"):
                get_context_fn = getattr(self.bot, "get_context", None)
                if callable(get_context_fn):
                    with contextlib.suppress(Exception):
                        ctx = await get_context_fn(message)
                        if getattr(ctx, "valid", False):
                            log.info("TRACE on_message: message is a valid command (%s), skipping AI chat pipeline", getattr(ctx, "command", None))
                            return

            # Bot停止中（スタンバイ中）ガード: AIチャット返信・自発発言等を完全にスキップ
            if not getattr(self, "is_active", True):
                log.debug("TRACE on_message: bot is inactive (stopped). skipping message cid=%s author=%s", channel_id(message), a_id)
                return

            cid = channel_id(message)
            bot_user_id = self.bot.user.id if self.bot.user else None
            kind = _get_channel_kind(message)

            # 歌唱中チャンネル、またはゲーム中チャンネル（うみがめ・20doors）であるかの判定
            channel_umigame_active = (cid in self.umigame_states) if (cid is not None and hasattr(self, "umigame_states")) else False
            channel_twenty_doors_busy = (
                self._is_twenty_doors_busy_channel(cid)
                if (cid is not None and hasattr(self, "_is_twenty_doors_busy_channel"))
                else False
            )
            is_singing_active = bool(
                cid is not None
                and hasattr(self, "is_singing_active_in_channel")
                and self.is_singing_active_in_channel(cid)
            )

            # 2. 担当チャンネル外（kind == "none"）かつ 進行中ゲーム等でもないメッセージは最上流で早期スキップ
            # （他Botや他チャンネルで行われている会話・返信のログ混信および不要なAPI通信を完全に遮断）
            if kind == "none" and not channel_umigame_active and not channel_twenty_doors_busy and not is_singing_active:
                log.debug("TRACE on_message: non-target channel message skipped cid=%s author=%s", cid, a_id)
                return

            force_response = await _is_force_response_trigger(message, bot_user_id)

            # このBotが関与するチャンネル・メッセージのみINFOログを出力
            log.info(
                "TRACE on_message: entered guild=%s channel=%s author=%s kind=%s content=%r",
                getattr(message.guild, "id", None), cid, a_id, kind, content(message)
            )

            # 発言者のプロファイルを最新情報で更新・追跡
            if message.guild is not None:
                try:
                    await self._record_message_author_profile(message)
                except Exception as e:
                    log.debug("record message author profile skipped: %r", e)

            # 歌唱中チャンネルでの中断判定：停止指示があれば即座にキャンセル
            if is_singing_active and is_singing_stop_command(content(message)):
                log.info("TRACE on_message: singing stop command received channel=%s author=%s", cid, a_id)
                stopped = await self._stop_singing_from_message(message)
                if stopped:
                    return

            # 画像が含まれるメッセージはカウントやレートリミットの対象外とする
            has_image = message_has_image(message)

            # サブチャンネル（other）における通常メッセージ数の記録（クールダウン・間隔制御用）
            if kind == "other" and a_id != bot_user_id and cid is not None and not has_image:
                current_other_count = _record_other_channel_message(self, cid)
                log.info("TRACE on_message: other channel message recorded cid=%s count=%d", cid, current_other_count)
            try:
                await self._maybe_capture_feedback(message, bot_user_id=bot_user_id)
            except Exception as e:
                log.exception("feedback capture failed: %s", e)

            try:
                await self._maybe_schedule_img2chan_thread_post(message)
            except Exception as e:
                log.warning("img2chan auto post schedule skipped channel=%s author=%s err=%r", channel_id(message), a_id, e)

            rate_limited = False
            if kind != "none" and a_id != bot_user_id and not has_image:
                rate_limited = _is_rate_limited(self.channel_rate_limit_hits, message)
            if rate_limited:
                self._record_unreplied_message(message, bot_user_id=bot_user_id)
                log.info("TRACE on_message: rate limit drop channel=%s author=%s content=%r", channel_id(message), a_id, content(message))
                return

            # 静かな時間帯（生活リズム）における通常発言（非メンション）の抑制（BAN-139）
            if not force_response and self._is_quiet_hours():
                self._record_unreplied_message(message, bot_user_id=bot_user_id)
                log.info("message skipped: reason=quiet_hours (non-mention ignored during sleep window)")
                return

            ignored, ch_kind = _should_ignore(message, bot_user_id, force_response=force_response, cog=self)
            log.info(
                "TRACE on_message: after _should_ignore channel=%s author=%s ignored=%s kind=%s",
                channel_id(message), a_id, ignored, ch_kind
            )
            if ignored:
                if kind != "none" and a_id != bot_user_id and not getattr(message.author, "bot", False):
                    self._record_unreplied_message(message, bot_user_id=bot_user_id)
                    self._maybe_trigger_passive_channel_maintenance(message)
                    if cid is not None:
                        self._maybe_schedule_passive_memory_update(message, cid)
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

            if await self._is_tier_game_start_request(message):
                handled = await self._start_tier_game_from_message(message)
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
                    self._record_unreplied_message(message, bot_user_id=bot_user_id)
                    log.info(
                        "TRACE on_message: priority queue full for force reply (size=%d/%d) drop channel=%s author=%s",
                        current_qsize, max_force_queue_size, channel_id(message), a_id,
                    )
                    return
            else:
                if self._queue_blocked or current_qsize >= max_queue_size:
                    self._queue_blocked = True
                    self._record_unreplied_message(message, bot_user_id=bot_user_id)
                    log.info(
                        "TRACE on_message: queue full/blocked (size=%d/%d) drop normal reply channel=%s author=%s",
                        current_qsize, max_queue_size, channel_id(message), a_id,
                    )
                    return
                # サブチャンネル自発返信の同一チャンネル重複防止
                # 既にキュー内待機中または処理中の自発返信がある場合はスキップ
                if kind == "other" and cid is not None:
                    active_cids = getattr(self, "_active_normal_reply_cids", None)
                    if active_cids is not None and cid in active_cids:
                        self._record_unreplied_message(message, bot_user_id=bot_user_id)
                        log.info(
                            "TRACE on_message: normal reply already active/queued for channel=%s drop channel=%s author=%s",
                            cid, cid, a_id,
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

            if not force_response and cid is not None:
                active_cids = getattr(self, "_active_normal_reply_cids", None)
                if active_cids is not None:
                    active_cids.add(cid)
                if kind == "other":
                    _reset_other_channel_message_count(self, cid)
                    log.info("TRACE on_message: reset other channel message count on normal reply enqueue cid=%s", cid)

            self._reply_queue_seq = getattr(self, "_reply_queue_seq", 0) + 1
            await self._reply_queue.put((priority, self._reply_queue_seq, message, force_response))
            refresh_fn = getattr(self, "_refresh_user_reply_done_event", None)
            if callable(refresh_fn):
                refresh_fn()
            else:
                reply_evt = getattr(self, "_user_reply_done_event", None)
                if reply_evt is not None:
                    reply_evt.clear()

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
        self._active_user_reply_turn_count = getattr(self, "_active_user_reply_turn_count", 0) + 1
        reply_evt = getattr(self, "_user_reply_done_event", None)
        if reply_evt is not None:
            reply_evt.clear()
        try:
            a_id = author_id(message)
            cid = channel_id(message)
            bot_user_id = self.bot.user.id if self.bot.user else None
            from front.discord.common.bot_identity import is_message_addressed_to_other_bot
            if is_message_addressed_to_other_bot(message, bot_user_id=bot_user_id, bot=self.bot):
                log.debug("TRACE _process_queued_message: message is addressed to another bot, skipping cid=%s author=%s", cid, a_id)
                return

            if not force_response and getattr(self, "_is_stale_reply", None) and self._is_stale_reply(message, is_direct=False):
                log.info(
                    "TRACE _process_queued_message: queued message is stale before processing, skipping cid=%s author=%s message_id=%s",
                    cid, a_id, getattr(message, "id", None),
                )
                return

            channel_umigame_active = cid in self.umigame_states
            try:
                runtime: MessageRuntime | None = await self._get_or_build_runtime(message)
                if runtime is None:
                    return
                turn_budget = {
                    "analyzer": 1,
                    "research_decision": 0,
                    "emotion_score": 0,
                    "main": 1,
                    "memory": 1,
                    "thought": 0,
                }
                setattr(runtime, "llm_budget", turn_budget)
                setattr(self, "_current_turn_llm_budget", turn_budget)
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

                # 静かな時間帯（生活リズム）におけるメンション・直接リプライの人間らしい応答遅延
                if force_response and self._is_quiet_hours():
                    delay = self._get_quiet_hours_delay()
                    log.info("quiet hours delay applied: delay=%.2fs channel=%s author=%s", delay, cid, a_id)
                    await asyncio.sleep(delay)

                log.info("TRACE on_message: before _send_contextual_reaction channel=%s author=%s force_response=%s", cid, a_id, force_response)
                await self._send_contextual_reaction(
                    message,
                    runtime=runtime,
                    incoming_emotion_scores=incoming_scores,
                    force_response=force_response,
                )
                log.info("TRACE on_message: after _send_contextual_reaction channel=%s author=%s", cid, a_id)

                if not emotion_already_updated:
                    self._schedule_emotion_update(
                        message,
                        runtime=runtime,
                        incoming_scores=emotion_update_scores if emotion_update_scores is not None else incoming_scores,
                        incoming_appraisal=emotion_update_appraisal,
                        incoming_reaction=incoming_reaction,
                    )
                self._log_llm_call_budget(runtime)
            except Exception as e:
                log.exception("_process_queued_message failed channel=%s author=%s: %s", cid, a_id, e)
                report_error("events._process_queued_message", e)
                if not getattr(message.author, "bot", False):
                    with contextlib.suppress(Exception):
                        await message.reply("AI応答でエラーが発生しました。\n", mention_author=False)
        finally:
            self._active_user_reply_turn_count = max(
                0, getattr(self, "_active_user_reply_turn_count", 1) - 1
            )
            refresh_fn = getattr(self, "_refresh_user_reply_done_event", None)
            if callable(refresh_fn):
                refresh_fn()
            elif reply_evt is not None and getattr(self, "_active_user_reply_count", 0) == 0 and self._active_user_reply_turn_count == 0:
                reply_evt.set()

    async def _reply_queue_worker(self) -> None:
        """優先度付きキューから返答タスクを優先度順・到着順に取り出して処理するワーカーループ。"""
        log.info("reply queue worker started")
        while True:
            try:
                priority, _seq, message, force_response = await self._reply_queue.get()
                cid = channel_id(message)
                log.info(
                    "TRACE queue worker: dequeue (priority=%d remaining=%d) channel=%s author=%s",
                    priority, self._reply_queue.qsize(), cid, author_id(message),
                )
                try:
                    await self._process_queued_message(message, force_response=force_response)
                finally:
                    if not force_response and cid is not None:
                        active_cids = getattr(self, "_active_normal_reply_cids", None)
                        if active_cids is not None:
                            active_cids.discard(cid)
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
                report_error("events._reply_queue_worker", e)

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
                await self._sync_emotion_presence(force=True)
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
                try:
                    if hasattr(guild, "chunk"):
                        await guild.chunk()
                except Exception as e:
                    log.debug("guild.chunk failed for %s: %r", getattr(guild, "id", None), e)
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
