"""OllamaChatReplyMixinの返信設定、送信、ランタイム構築、即時応答処理を担当する補助Mixin。

このファイルはDiscordメッセージから返答用runtimeを組み立て、時刻・天気などの
モデル不要な返信、返信送信、プロンプト共通ブロック、今後の発話キュー/習慣状態の
更新を扱う。通常返答や割り込み返答のLLMプロンプト生成そのものは
ollama_chat_reply_generation.pyに分離している。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr


import discord

from ..common import habit_policy, prospective_memory
from ..common.context_helpers import _build_summary_context_lines
from ..common.discord_helpers import append_context_message
from ..common.singing_helpers import compact_singing_context_lines
from ..common.memory.logic import get_user_habit_profile
from ..common.memory_logic import (
    extract_and_store_memory,
    maybe_update_channel_summary,
)
from ..common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_unusable_assistant_reply,
    truncate_lines,
)
from ..common.weather_helpers import (
    build_weather_followup_reply,
    extract_recent_weather_fact_from_context,
    extract_weather_place,
)
from . import ollama_chat_helpers as _helpers
from .ollama_chat_helpers import (
    _build_reply_context_user_text,
    _managed_channel_ids,
    _normalize_compare_text,
    _relationship_score_for_habit,
    _reset_other_channel_unreplied_count,
    author_id,
    channel_id,
    content,
    display_name,
    extract_action_style_from_intent_info,
    extract_weather_place_from_context,
    fetch_weather_summary,
    has_anon_buttons,
    is_explicit_weather_request,
    looks_like_weather_query,
    message_has_supported_media_or_links,
    prune_stale_context_cache,
    recent_context_items,
    resolve_reference_message,
    sanitize_generated_reply,
    to_context_line,
    truncate_text,
)
from .ollama_chat_texts import build_chat_reply_system_prompt
from .ollama_chat_types import EmotionState, MediaBundle, MessageRuntime
from ..common.reply_helpers import (
    _format_reply,
    _merge_user_text_with_media_analysis,
    _strip_user_echo_prefix,
    looks_like_time_query,
    build_current_time_reply,
    _build_weather_comment,
)

log = logging.getLogger("ollama_bot.ollama_chat_.reply_core")

_PUBLIC_REPLY_MODULE = f"{__package__}.ollama_chat_reply"
_default_cfg = _helpers.cfg
_default_cfg_int = _helpers.cfg_int
_default_cfg_bool = _helpers.cfg_bool
_default_cfg_managed_channel_ids = _helpers.cfg_managed_channel_ids
_default_fetch_recent_context_lines = _helpers.fetch_recent_context_lines
_default_extract_base64_images = _helpers.extract_base64_images
_default_build_message_media_context = _helpers.build_message_media_context


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)



def cfg(name: str, default: Any = None) -> Any:
    return _public_attr("cfg", _default_cfg)(name, default)


def cfg_int(name: str, default: int) -> int:
    return _public_attr("cfg_int", _default_cfg_int)(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    return _public_attr("cfg_bool", _default_cfg_bool)(name, default)


def cfg_managed_channel_ids() -> set[int]:
    return _public_attr("cfg_managed_channel_ids", _default_cfg_managed_channel_ids)()


def fetch_recent_context_lines(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("fetch_recent_context_lines", _default_fetch_recent_context_lines)(*args, **kwargs)


def extract_base64_images(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("extract_base64_images", _default_extract_base64_images)(*args, **kwargs)


def build_message_media_context(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("build_message_media_context", _default_build_message_media_context)(*args, **kwargs)


if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChatReplyCoreBase = OllamaChatProtocol
else:
    _OllamaChatReplyCoreBase = object


class OllamaChatReplyCoreMixin(_OllamaChatReplyCoreBase):

    def _reply_num_predict(self, intent_info: dict[str, Any] | None = None) -> int | None:
        if intent_info:
            source = str(intent_info.get("source") or "").strip()
            if source in ("web", "research", "img", "x"):
                value = cfg_int("OLLAMA_REPLY_NUM_PREDICT_LONG", 1536)
                return value if value > 0 else None
        default_num = 2048 if cfg_bool("OLLAMA_MAIN_MODEL_THINK", False) else 512
        value = cfg_int("OLLAMA_REPLY_NUM_PREDICT", default_num)
        return value if value > 0 else None

    def _reply_retry_num_predict(self, intent_info: dict[str, Any] | None = None) -> int | None:
        configured = cfg_int("OLLAMA_REPLY_RETRY_NUM_PREDICT", 0)
        if configured > 0:
            return configured
        base = self._reply_num_predict(intent_info) or 0
        return max(base * 2, 192) if base > 0 else 192

    def _reply_system_prompt(self) -> str:
        persona_blocks = [str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()]
        style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        if style_guard:
            persona_blocks.append("【口調ガード】\n" + style_guard)
        if extra_rule:
            persona_blocks.append("【追加ルール】\n" + extra_rule)
        persona = "\n\n".join(block for block in persona_blocks if block)
        return build_chat_reply_system_prompt(persona)

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

    def _format_reply_deliberation_note(self, result: dict[str, Any], *, trigger_reason: str) -> str:
        trigger_map = {
            "anger": "怒りが強く出ている",
            "surprise": "驚きが強く出ている",
            "curiosity": "好奇心が高まっている",
            "classifier": "分類器が熟考パスを推奨した",
        }
        max_chars = cfg_int("OLLAMA_DELIBERATION_MAX_CHARS", 120)
        items = [
            ("intent", "意図"),
            ("emotion_focus", "感情の軸"),
            ("memory_hook", "拾う記憶"),
            ("reply_strategy", "返答方針"),
            ("avoid", "避けること"),
            ("consistency_correction", "キャラ維持指示"),
        ]

        lines = [
            "※内部整理用。以下の箇条書きや見出しは最終返答に出力せず、内容だけ自然に反映してください。"
        ]
        for key, label in items:
            value = truncate_text(str(result.get(key) or "").strip(), max_chars)
            if value:
                lines.append(f"- {label}: {value}")

        trigger_text = trigger_map.get(trigger_reason, "").strip()
        if trigger_text:
            lines.append(f"- 熟考トリガー: {trigger_text}")

        if len(lines) <= 1:
            return ""

        return "[返答前の内省メモ]\n" + "\n".join(lines) + "\n[/返答前の内省メモ]"

    async def _select_action_style_for_turn(
        self,
        *,
        runtime: MessageRuntime,
        intent_info: dict[str, Any],
        deliberation_info: dict[str, Any],
        working_context: dict[str, Any],
        emotion_state: EmotionState | None,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> tuple[str, str]:
        relationship_snapshot = await self._get_user_relationship_snapshot(
            getattr(runtime, "user_id", None),
            extra_scores=incoming_emotion_scores,
            preview_with_extra_scores=True,
        )
        relationship_score = _relationship_score_for_habit(relationship_snapshot)
        context_key = habit_policy.build_context_key(
            user_id=getattr(runtime, "user_id", None),
            channel_id=getattr(runtime, "cid", None),
            topic=str((working_context or {}).get("topic", "") or ""),
            relationship_score=relationship_score,
            emotion_state=emotion_state,
        )
        base_style = extract_action_style_from_intent_info(intent_info, deliberation_info)
        allowed_styles = habit_policy.get_allowed_styles(cfg)
        habit_bonus = await habit_policy.get_habit_bonus(
            self.memory_store,
            persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            context_key=context_key,
        )
        user_profile = await get_user_habit_profile(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(runtime, "guild_id", None),
            user_id=getattr(runtime, "user_id", None),
        )
        for style, bonus in habit_policy.action_style_bonus_from_user_profile(
            user_profile,
            allowed_styles=allowed_styles,
        ).items():
            habit_bonus[style] = max(float(habit_bonus.get(style, 0.0) or 0.0), float(bonus))
        selected_style = habit_policy.rerank_with_habit(
            style=base_style,
            habit_bonus=habit_bonus,
            allowed_styles=allowed_styles,
            habit_weight=habit_policy.get_habit_weight(cfg),
        )
        return selected_style, context_key

    def _build_action_style_prompt_block(self, action_style: str) -> str:
        if not action_style:
            return ""
        instruction = habit_policy.build_action_style_instruction(action_style)
        return (
            "[今回の返答スタイル]\n"
            f"- action_style: {action_style}\n"
            f"- 指示: {instruction}"
        )

    async def _mark_future_prompts_triggered(self, runtime: MessageRuntime) -> None:
        for record in getattr(runtime, "pending_future_prompts", []) or []:
            record_id = record.get("id")
            if record_id is None:
                continue
            try:
                await prospective_memory.mark_triggered(self.memory_store, int(record_id))
            except Exception as e:
                log.warning("prospective mark_triggered failed: id=%s err=%r", record_id, e)

    def _remember_pending_habit_turn(
        self,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> None:
        if not getattr(runtime, "selected_action_style", "") or not getattr(runtime, "habit_context_key", ""):
            return
        key = (getattr(runtime, "cid", None), getattr(runtime, "user_id", None))
        self._pending_habit_turns[key] = {
            "persona": str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            "context_key": runtime.habit_context_key,
            "action_style": runtime.selected_action_style,
            "emotion_scores": dict(incoming_emotion_scores or {}),
            "used_at": time.time(),
        }

    async def _apply_action_style_to_prompt(
        self,
        prompt: str,
        *,
        runtime: MessageRuntime,
        intent_info: dict[str, Any],
        deliberation_info: dict[str, Any],
        working_context: dict[str, Any],
        emotion_state: EmotionState | None,
        pending_future_prompts: list[dict[str, Any]] | None = None,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> str:
        action_style, context_key = await self._select_action_style_for_turn(
            runtime=runtime,
            intent_info=intent_info,
            deliberation_info=deliberation_info,
            working_context=working_context,
            emotion_state=emotion_state,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        runtime.selected_action_style = action_style
        runtime.habit_context_key = context_key
        runtime.working_context = dict(working_context or {})
        runtime.pending_future_prompts = list(pending_future_prompts or [])

        prompt_block = self._build_action_style_prompt_block(action_style)
        if prompt_block:
            return f"{prompt_block}\n\n{prompt}"
        return prompt

    def _prepend_social_guidance_to_prompt(self, prompt: str, social_guidance: str) -> str:
        """Prepend social guidance (emotions/relationships) to the prompt."""
        return (
            "[現在の感情状態と相手との関係性]\n"
            f"{social_guidance}\n\n"
            f"{prompt}"
        )

    async def _store_bot_utterance(self, message: discord.Message, raw_reply: str) -> None:
        try:
            cleaned_reply = extract_first_user_facing_reply(raw_reply)
            if cleaned_reply and not looks_like_abnormal_assistant_reply(cleaned_reply):
                guild_obj = getattr(message, "guild", None)
                channel_obj = getattr(message, "channel", None)
                await self.memory_store.add_bot_utterance(
                    persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
                    guild_id=getattr(guild_obj, "id", None),
                    channel_id=getattr(channel_obj, "id", None),
                    content=cleaned_reply,
                )
        except Exception as e:
            log.exception("bot utterance save failed: %s", e)

    async def _reply_to_message(
        self,
        target_message: discord.Message,
        reply: str,
        *,
        mention_author: bool = False,
    ) -> discord.Message:
        raw_reply = str(reply or "").strip()
        payload = _format_reply(reply)
        try:
            sent = await target_message.reply(payload, mention_author=mention_author)
        except discord.NotFound:
            log.info("reply target disappeared; falling back to channel send (message_id=%s)", getattr(target_message, "id", None))
            sent = await target_message.channel.send(payload)
        except discord.HTTPException as e:
            if getattr(e, "code", None) == 50035 and "Unknown message" in str(e):
                log.info(
                    "reply target unknown; falling back to channel send (message_id=%s, code=%s)",
                    getattr(target_message, "id", None),
                    getattr(e, "code", None),
                )
                sent = await target_message.channel.send(payload)
            else:
                raise
        return sent

    async def _send_reply(self, message: discord.Message, reply: str) -> discord.Message:
        raw_reply = str(reply or "").strip()
        sent = await self._reply_to_message(message, raw_reply, mention_author=False)
        await self._store_bot_utterance(message, raw_reply)
        _reset_other_channel_unreplied_count(self, channel_id(message))
        return sent

    async def _send_provisional_reply(self, message: discord.Message, reply: str) -> discord.Message:
        return await self._reply_to_message(message, reply, mention_author=False)

    async def _send_followup_reply(self, parent_message: discord.Message, reply: str) -> discord.Message:
        raw_reply = str(reply or "").strip()
        sent = await self._reply_to_message(parent_message, raw_reply, mention_author=False)
        await self._store_bot_utterance(parent_message, raw_reply)
        return sent

    def _append_current_user_message(self, runtime: MessageRuntime, message: discord.Message) -> None:
        if runtime.cid is not None:
            append_context_message(self.channel_context_cache, message, bot_user_id=runtime.bot_user_id)

    def _append_sent_message(self, sent: discord.Message) -> None:
        sent_text = content(sent)
        if looks_like_abnormal_assistant_reply(sent_text):
            return
        append_context_message(self.channel_context_cache, sent, bot_user_id=self.bot.user.id if self.bot.user else None)

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
        effective_user_text = await _build_reply_context_user_text(
            message,
            original_user_text,
            bot_user_id=bot_user_id,
        )
        base64_images = await extract_base64_images(message)
        media_context = await build_message_media_context(message)
        media = MediaBundle(
            images=base64_images or list(media_context.get("images") or []),
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
            memory_context_lines=memory_context_lines,
            is_umigame_reply=await self._is_umigame_reply_message(message),
            is_twenty_doors_reply=await self._is_twenty_doors_reply_message(message),
        )

    async def _get_or_build_runtime(self, message: discord.Message) -> MessageRuntime | None:
        runtime = getattr(message, "_ollama_runtime", None)
        if runtime is not None:
            return runtime
        runtime = await self._build_runtime(message)
        try:
            setattr(message, "_ollama_runtime", runtime)
        except Exception:
            pass
        return runtime

    async def _handle_time(self, message: discord.Message, runtime: MessageRuntime) -> bool:
        if not looks_like_time_query(runtime.original_user_text):
            return False
        self._append_current_user_message(runtime, message)
        sent = await self._send_reply(message, build_current_time_reply(runtime.original_user_text))
        self._append_sent_message(sent)
        return True

    async def _handle_weather(self, message: discord.Message, runtime: MessageRuntime) -> bool:
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
        sent = await self._send_reply(message, reply)
        self._append_sent_message(sent)

        weather_emotion_scores, _, weather_emotion_reason = await self._get_emotion_snapshot()

        async def _bg_weather_memory() -> None:
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
                )
            except Exception as e:
                log.exception("weather memory extract failed: %s", e)

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
        
        asyncio.create_task(_bg_weather_memory())

        return True

    def _managed_channel_ids(self) -> set[int]:
        return cfg_managed_channel_ids()
