"""OllamaChatReplyMixinの返信設定、送信、ランタイム構築、即時応答処理、LLM返信生成を担当する補助Mixin。

このファイルはDiscordメッセージから返答用runtimeを組み立て、時刻・天気などの
モデル不要な返信、返信送信、プロンプト共通ブロック、LLM返信生成、メモリ永続化、
今後の発話キュー/習慣状態の更新を扱う。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import sys
import time
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr
from lib.config_utils import cfg_persona_namespace


import discord

from harness.agent.prompt_parser.parser import habit_policy
from harness.agent.prompt_parser.prompt_manager import prospective_memory, user_profile_logic
from ..common.bot_identity import resolve_bot_identity
from ..common.discord_helpers import append_context_message, format_message_reactions
from ..common.emotion_helpers import build_memory_emotion_tags, merge_emotion_scores
from harness.agent.tool_registry.singing_helpers import compact_singing_context_lines
from harness.agent.memory_manager.post_reply_handler import handle_post_reply_memory_and_summary
from harness.agent.memory_manager.summarizer import (
    maybe_store_training_candidate as _default_maybe_store_training_candidate,
)
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import (
    format_user_interests_for_prompt,
    update_user_interests as _default_update_user_interests,
)
from harness.agent.memory_manager.logic import (
    extract_and_store_memory as _default_extract_and_store_memory,
    get_relevant_memories,
    get_user_habit_profile,
    get_user_habit_profile_lines,
    maybe_update_channel_summary as _default_maybe_update_channel_summary,
    update_user_habit_profile as _default_update_user_habit_profile,
)
from harness.agent.prompt_parser.prompt_manager.budget import (
    format_core_beliefs_for_prompt,
    sanitize_memory_candidates,
)
from ..common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_parrot_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_unusable_assistant_reply,
    truncate_lines,
)
from harness.agent.tool_registry.weather_helpers import (
    build_weather_followup_reply,
    extract_recent_weather_fact_from_context,
    extract_weather_place,
)
from . import ollama_chat_helpers as _helpers
from .ollama_chat_helpers import (
    _analyze_pre_reply_context,
    _build_reply_context_user_text,
    _build_user_prompt,
    _managed_channel_ids,
    _normalize_compare_text,
    _relationship_score_for_habit,
    _reset_other_channel_message_count,
    _reset_other_channel_unreplied_count,
    append_chat_reply_output_suffix,
    author_id,
    call_ollama,
    call_ollama_chat_raw,
    channel_id,
    choose_spontaneous_source,
    content,
    display_name,
    extract_action_style_from_intent_info,
    extract_weather_place_from_context,
    fetch_weather_summary,
    format_x_timeline_items_for_prompt,
    has_anon_buttons,
    is_explicit_weather_request,
    looks_like_weather_query,
    message_has_supported_media_or_links,
    model_supports_thinking,
    prune_stale_context_cache,
    recent_context_items,
    rotate_x_timeline_items_for_spontaneous,
    sanitize_generated_reply,
    to_context_line,
    truncate_text,
)
from .ollama_chat_texts import build_break_prompt_parts, build_chat_reply_system_prompt
from .ollama_chat_types import EmotionState, MediaBundle, MessageRuntime, ReplyModelTimeoutError
from harness.agent.prompt_parser.parser.reply_helpers import (
    _build_unclear_intent_reply,
    _format_reply,
    _merge_user_text_with_media_analysis,
    _should_use_unclear_intent_fallback,
    _strip_user_echo_prefix,
    looks_like_time_query,
    build_current_time_reply,
    _build_weather_comment,
)
from harness.agent.prompt_parser.prompt_manager.working_memory import build_working_context

log = logging.getLogger("ollama_bot.ollama_chat_.reply_core")

_PUBLIC_REPLY_MODULE = f"{__package__}.ollama_chat_reply"
_default_cfg = _helpers.cfg
_default_cfg_int = _helpers.cfg_int
_default_cfg_float = _helpers.cfg_float
_default_cfg_bool = _helpers.cfg_bool
_default_cfg_managed_channel_ids = _helpers.cfg_managed_channel_ids
_default_fetch_recent_context_lines = _helpers.fetch_recent_context_lines
_default_find_last_assistant_message_text = _helpers._find_last_assistant_message_text
_default_fetch_recent_x_timeline_items = _helpers.fetch_recent_x_timeline_items
_default_research_dispatch = _helpers.research_dispatch
_default_extract_base64_images = _helpers.extract_base64_images
_default_build_message_media_context = _helpers.build_message_media_context
_default_resolve_reference_message = _helpers.resolve_reference_message


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)



def cfg(name: str, default: Any = None) -> Any:
    fn = _public_attr("cfg", _default_cfg)
    return _default_cfg(name, default) if fn is cfg else fn(name, default)


def cfg_int(name: str, default: int = 0) -> int:
    fn = _public_attr("cfg_int", _default_cfg_int)
    return _default_cfg_int(name, default) if fn is cfg_int else fn(name, default)


def cfg_float(name: str, default: float = 0.0) -> float:
    fn = _public_attr("cfg_float", _default_cfg_float)
    return _default_cfg_float(name, default) if fn is cfg_float else fn(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    fn = _public_attr("cfg_bool", _default_cfg_bool)
    return _default_cfg_bool(name, default) if fn is cfg_bool else fn(name, default)


def cfg_managed_channel_ids() -> set[int]:
    fn = _public_attr("cfg_managed_channel_ids", _default_cfg_managed_channel_ids)
    return _default_cfg_managed_channel_ids() if fn is cfg_managed_channel_ids else fn()


def fetch_recent_context_lines(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("fetch_recent_context_lines", _default_fetch_recent_context_lines)
    return _default_fetch_recent_context_lines(*args, **kwargs) if fn is fetch_recent_context_lines else fn(*args, **kwargs)


def _find_last_assistant_message_text(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_find_last_assistant_message_text", _default_find_last_assistant_message_text)
    return _default_find_last_assistant_message_text(*args, **kwargs) if fn is _find_last_assistant_message_text else fn(*args, **kwargs)


def fetch_recent_x_timeline_items(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("fetch_recent_x_timeline_items", _default_fetch_recent_x_timeline_items)
    return _default_fetch_recent_x_timeline_items(*args, **kwargs) if fn is fetch_recent_x_timeline_items else fn(*args, **kwargs)


def research_dispatch(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("research_dispatch", _default_research_dispatch)
    return _default_research_dispatch(*args, **kwargs) if fn is research_dispatch else fn(*args, **kwargs)


def extract_base64_images(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("extract_base64_images", _default_extract_base64_images)
    return _default_extract_base64_images(*args, **kwargs) if fn is extract_base64_images else fn(*args, **kwargs)


def build_message_media_context(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("build_message_media_context", _default_build_message_media_context)
    return _default_build_message_media_context(*args, **kwargs) if fn is build_message_media_context else fn(*args, **kwargs)


def resolve_reference_message(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("resolve_reference_message", _default_resolve_reference_message)
    return _default_resolve_reference_message(*args, **kwargs) if fn is resolve_reference_message else fn(*args, **kwargs)


def extract_and_store_memory(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("extract_and_store_memory", _default_extract_and_store_memory)
    return _default_extract_and_store_memory(*args, **kwargs) if fn is extract_and_store_memory else fn(*args, **kwargs)


def maybe_store_training_candidate(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("maybe_store_training_candidate", _default_maybe_store_training_candidate)
    return _default_maybe_store_training_candidate(*args, **kwargs) if fn is maybe_store_training_candidate else fn(*args, **kwargs)


def update_user_habit_profile(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("update_user_habit_profile", _default_update_user_habit_profile)
    return _default_update_user_habit_profile(*args, **kwargs) if fn is update_user_habit_profile else fn(*args, **kwargs)


def update_user_interests(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("update_user_interests", _default_update_user_interests)
    return _default_update_user_interests(*args, **kwargs) if fn is update_user_interests else fn(*args, **kwargs)


def _build_summary_context_lines(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_build_summary_context_lines", _helpers._build_summary_context_lines)
    return _helpers._build_summary_context_lines(*args, **kwargs) if fn is _build_summary_context_lines else fn(*args, **kwargs)


def maybe_update_channel_summary(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("maybe_update_channel_summary", _default_maybe_update_channel_summary)
    return _default_maybe_update_channel_summary(*args, **kwargs) if fn is maybe_update_channel_summary else fn(*args, **kwargs)


def _is_context_size_exceeded_error(exc: Exception) -> bool:
    """Ollamaのコンテキストサイズ超過（HTTP 400 Bad Request / exceed_context_size_error 等）かを判定する。"""
    try:
        from ..common.ollama_helpers import OllamaContextSizeExceededError
        if isinstance(exc, OllamaContextSizeExceededError):
            return True
    except Exception:
        pass

    err_str = str(exc).lower()
    if any(marker in err_str for marker in (
        "exceed_context_size",
        "context length",
        "context size",
        "maximum context",
        "too many tokens",
    )):
        return True

    status = getattr(exc, "status", None)
    if status == 400:
        return True

    return False


def _is_transient_reply_model_error(exc: Exception) -> bool:
    """一時的なネットワーク／サーバー障害であり、待機後に再送すべきかを判定する。"""
    if _is_context_size_exceeded_error(exc):
        return False

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True

    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientResponseError):
            if 400 <= exc.status < 500 and exc.status not in (408, 429):
                return False
            if exc.status in (408, 429, 500, 502, 503, 504):
                return True
            return False
        if isinstance(exc, (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError)):
            return True
    except Exception:
        pass

    status = getattr(exc, "status", None)
    if status is not None and isinstance(status, int):
        if 400 <= status < 500 and status not in (408, 429):
            return False
        if status in (408, 429, 500, 502, 503, 504):
            return True

    exc_type = type(exc)
    name = str(getattr(exc_type, "__name__", "") or "").lower()
    if "response" in name and "error" in name:
        return False
    if any(marker in name for marker in ("timeout", "socket", "serverdisconnected", "connection")):
        return True

    module = str(getattr(exc_type, "__module__", "") or "").lower()
    if "aiohttp" in module:
        return True
    return False


def _reply_model_timeout_fallback() -> str:
    fallback = str(cfg("OLLAMA_MODEL_TIMEOUT_FALLBACK", "") or "").strip()
    if fallback:
        return fallback
    fallback = str(
        cfg(
            "OLLAMA_EMPTY_REPLY_FALLBACK",
            "今ちょっと応答が重い。もう一回言ってくれ。",
        )
        or ""
    ).strip()
    return fallback or "今ちょっと応答が重い。もう一回言ってくれ。"



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
            persona=cfg_persona_namespace(custom_cfg=cfg),
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
            "persona": cfg_persona_namespace(custom_cfg=cfg),
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
                    persona=cfg_persona_namespace(custom_cfg=cfg),
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
        except discord.Forbidden as e:
            # 160002: Cannot reply without permission to read message history
            # 50013: Missing Permissions (general send/reply permission)
            err_code = getattr(e, "code", None)
            if err_code in (160002, 50013):
                log.warning(
                    "reply forbidden (code=%s); falling back to channel send (message_id=%s channel=%s)",
                    err_code,
                    getattr(target_message, "id", None),
                    getattr(getattr(target_message, "channel", None), "id", None),
                )
                sent = await target_message.channel.send(payload)
            else:
                raise
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

    def _resolve_send_other_names(
        self,
        message: discord.Message | None,
        target_name: str,
    ) -> set[str]:
        """送信直前にサニタイズ対象とする他者名（直近会話話者、返信元作成者、メンション対象等）を収集する。"""
        other_names: set[str] = set()
        clean_target = (target_name or "").strip()
        if message is None:
            return other_names

        cid = getattr(getattr(message, "channel", None), "id", None)
        # 1. チャンネル直近キャッシュから取得
        if cid:
            cached_items = recent_context_items(
                self.channel_context_cache,
                cid,
                before_message=message,
                max_age_sec=3600,
            )
            for item in cached_items:
                line = str(item.get("line") or "")
                if ":" in line:
                    speaker = line.split(":", 1)[0].strip()
                    if speaker and speaker != clean_target:
                        other_names.add(speaker)

        # 2. 返信元の親メッセージ（reference）の作成者名を無条件で追加
        ref = getattr(message, "reference", None)
        if ref:
            ref_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
            if ref_msg is not None:
                ref_author = display_name(ref_msg).strip()
                if ref_author and ref_author != clean_target:
                    other_names.add(ref_author)
                ref_raw_author = getattr(getattr(ref_msg, "author", None), "name", "")
                if ref_raw_author and ref_raw_author != clean_target:
                    other_names.add(ref_raw_author)
            mid = getattr(ref, "message_id", None)
            if mid and cid:
                q = self.channel_context_cache.get(cid)
                if q:
                    for itm in q:
                        if itm.get("id") == mid:
                            line = str(itm.get("line") or "")
                            if ":" in line:
                                spk = line.split(":", 1)[0].strip()
                                if spk and spk != clean_target:
                                    other_names.add(spk)

        # 3. メンションされているユーザーの名前を追加
        for m in getattr(message, "mentions", []) or []:
            m_name = display_name(m).strip()
            if m_name and m_name != clean_target:
                other_names.add(m_name)
            raw_name = getattr(m, "name", "")
            if raw_name and raw_name != clean_target:
                other_names.add(raw_name)

        # 4. スレッド作成者等の追跡
        ch = getattr(message, "channel", None)
        if ch is not None:
            thread_owner = getattr(ch, "owner", None)
            if thread_owner is not None:
                to_name = display_name(thread_owner).strip()
                if to_name and to_name != clean_target:
                    other_names.add(to_name)

        # 5. 発言者本人の生アカウント名（サーバーニックネームと乖離している場合の多重防御）
        author = getattr(message, "author", None)
        if author is not None:
            raw_author = getattr(author, "name", "")
            if raw_author and raw_author != clean_target:
                other_names.add(raw_author)

        return other_names

    async def _send_reply(self, message: discord.Message, reply: str) -> discord.Message:
        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(message) if message is not None else "").strip()
        other_names = self._resolve_send_other_names(message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(message, "content", "") or ""),
        )
        sent = await self._reply_to_message(message, raw_reply, mention_author=False)
        await self._store_bot_utterance(message, raw_reply)
        return sent

    async def _send_provisional_reply(self, message: discord.Message, reply: str) -> discord.Message:
        return await self._reply_to_message(message, reply, mention_author=False)

    async def _send_followup_reply(self, parent_message: discord.Message, reply: str) -> discord.Message:
        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(parent_message) if parent_message is not None else "").strip()
        other_names = self._resolve_send_other_names(parent_message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(parent_message, "content", "") or ""),
        )
        sent = await self._reply_to_message(parent_message, raw_reply, mention_author=False)
        await self._store_bot_utterance(parent_message, raw_reply)
        return sent

    def _append_current_user_message(self, runtime: MessageRuntime, message: discord.Message) -> None:
        if runtime.cid is not None:
            append_context_message(self.channel_context_cache, message, bot_user_id=runtime.bot_user_id)
            if hasattr(self, "thread_tracker") and self.thread_tracker:
                self.thread_tracker.record_message(
                    message,
                    bot_user_id=runtime.bot_user_id,
                    bot_identity=resolve_bot_identity(bot=self.bot, message=message),
                )

    def _append_sent_message(self, sent: discord.Message) -> None:
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

    def _episode_importance_score(
        self,
        *,
        user_text: str,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> float:
        peak = max((float(v or 0.0) for v in (incoming_emotion_scores or {}).values()), default=0.0)
        text = str(user_text or "").strip()
        reply = str(assistant_text or "").strip()
        importance = peak * 0.7
        if len(text) >= 30:
            importance += 0.08
        if any(marker in text for marker in ("助けて", "つらい", "しんどい", "怖い", "やばい", "ごめん", "ありがとう", "嬉しい", "かなしい", "悲しい")):
            importance += 0.12
        if any(marker in reply for marker in ("大丈夫", "落ち着", "まず", "なら", "してみ", "確認")):
            importance += 0.08
        return max(0.0, min(1.0, importance))

    def _episode_source_label(self, source: str, research_result: dict[str, Any] | None = None) -> str:
        mode = str((research_result or {}).get("mode") or source or "").strip().lower()
        if mode == "x_timeline":
            return "Xタイムライン"
        if mode == "img_top5":
            return "IMG勢い上位"
        if mode == "img_thread":
            return "IMGスレッド"
        if mode in {"browser_read_url", "browser_search", "simple_search"}:
            return "Web調査"
        if mode == "x":
            return "Xタイムライン"
        if mode == "img":
            return "IMG勢い上位"
        return "Discord"

    def _summarize_bot_action_for_episode(self, runtime: MessageRuntime, assistant_text: str) -> str:
        action_style = str(getattr(runtime, "selected_action_style", "") or "").strip()
        style_map = {
            "tease_then_answer": "軽くツッコミを入れてから答えた",
            "give_steps_first": "結論や手順を先に示した",
            "short_reaction": "短く反応した",
            "direct_reply": "率直に答えた",
            "validate_first": "まず受け止めてから答えた",
            "clarify_gently": "やわらかく確認しながら返した",
        }
        if action_style in style_map:
            return style_map[action_style]
        cleaned = truncate_text(str(assistant_text or "").strip(), 80)
        return cleaned or "返答した"

    def _build_episode_what_text(
        self,
        *,
        runtime: MessageRuntime,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> str:
        parts: list[str] = []
        user_text = truncate_text(str(runtime.original_user_text or "").strip(), 220)
        if user_text:
            parts.append(f"Discord発言: {user_text}")

        result = dict(research_result or {})
        context = dict(episode_context or {})
        query = str(result.get("query") or context.get("source_query") or "").strip()
        if query:
            parts.append(f"参照先: {truncate_text(query, 180)}")

        source_summary = str(result.get("summary") or context.get("source_summary") or "").strip()
        if source_summary:
            parts.append(f"参照内容: {truncate_text(source_summary, 420)}")

        if not parts and source:
            parts.append(f"source={source}")
        return "\n".join(parts)[:600]

    async def _maybe_store_episode_memory(
        self,
        *,
        message: discord.Message,
        runtime: MessageRuntime,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> None:
        if not cfg_bool("MEMORY_EPISODE_ENABLED", True):
            return
        add_episode = getattr(self.memory_store, "add_episode", None)
        if not callable(add_episode):
            return

        importance = self._episode_importance_score(
            user_text=runtime.original_user_text,
            assistant_text=assistant_text,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        source_label = self._episode_source_label(source, research_result)
        if cfg_bool("MEMORY_EPISODE_STORE_ALL_TURNS", True):
            importance = max(importance, float(cfg("MEMORY_EPISODE_BASE_IMPORTANCE", 0.42) or 0.42))
        elif importance < float(cfg("MEMORY_EPISODE_STORE_THRESHOLD", 0.62) or 0.62):
            return

        emotion_tags = build_memory_emotion_tags(incoming_emotion_scores or {}, max_tags=3, min_score=0.30)
        user_text = truncate_text(runtime.original_user_text, 120)
        action_summary = self._summarize_bot_action_for_episode(runtime, assistant_text)
        _, reactions_list = format_message_reactions(message, bot_user_id=runtime.bot_user_id)
        bot_reacts = [r for r in reactions_list if r.get("is_bot")]
        if bot_reacts:
            react_names = "・".join(r["emoji"] for r in bot_reacts)
            action_summary = f"{react_names}でリアクションし、{action_summary}"
        if source_label == "Discord":
            summary = f"{user_text} に対して、{action_summary}。".strip()
        else:
            summary = f"{source_label}を見て、{user_text or 'Discord上の流れ'} に対して、{action_summary}。".strip()
        tags = list(emotion_tags)
        source_tag = str((research_result or {}).get("mode") or source or "discord").strip().lower() or "discord"
        for tag in ("episode", source_tag):
            if tag not in tags:
                tags.append(tag)

        bot_action_text = truncate_text(assistant_text or action_summary, 600)
        if bot_reacts and not any(r["emoji"] in bot_action_text for r in bot_reacts):
            bot_action_text = f"[{' '.join(r['emoji'] for r in bot_reacts)}] {bot_action_text}"

        await add_episode(
            persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            summary=summary,
            what=self._build_episode_what_text(
                runtime=runtime,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            ),
            bot_action=bot_action_text,
            user_reaction=None,
            emotion_before=incoming_emotion_scores or None,
            emotion_after=None,
            importance=importance,
            tags=tags,
        )

    async def _store_and_update_summary(
        self,
        message: discord.Message,
        sent: discord.Message,
        *,
        runtime: MessageRuntime,
        context_lines: list[str],
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None = None,
        spontaneous_source: str | None = None,
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> None:
        if looks_like_abnormal_assistant_reply(assistant_text):
            return
        skip_long_term_memory = str(spontaneous_source or "").strip().lower() == "x"
        source = (
            str((research_result or {}).get("mode") or "").strip()
            or str(spontaneous_source or "").strip()
            or "discord"
        )

        cfg_fn = _public_attr("cfg", cfg)
        trimmed_context_lines = truncate_lines(
            context_lines,
            max_lines=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
            max_chars_per_line=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
            max_total_chars=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
        )
        memory_context_lines = self._trim_memory_context_lines(
            runtime.memory_context_lines or context_lines
        ) or trimmed_context_lines

        get_snapshot = getattr(self, "_get_emotion_snapshot", None)
        if callable(get_snapshot):
            snap = get_snapshot()
            if inspect.isawaitable(snap):
                snap = await snap
            emotion_scores, _, emotion_reason = snap if isinstance(snap, tuple) and len(snap) >= 3 else ({}, None, "")
        else:
            emotion_scores, emotion_reason = {}, ""
        emotion_scores = merge_emotion_scores(emotion_scores, incoming_emotion_scores)

        if not skip_long_term_memory:
            try:
                extract_fn = _public_attr("extract_and_store_memory", extract_and_store_memory)
                await extract_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    emotion_scores=emotion_scores,
                    emotion_reason=emotion_reason,
                )
            except Exception as e:
                log.exception("memory extract failed: %s", e)

        try:
            await self._maybe_store_episode_memory(
                message=message,
                runtime=runtime,
                assistant_text=assistant_text,
                incoming_emotion_scores=incoming_emotion_scores,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            )
        except Exception as e:
            log.exception("episode memory store failed: %s", e)

        highest_emotion_score = max(emotion_scores.values()) if emotion_scores else 0.0
        if highest_emotion_score >= 0.8:
            deep_reflection = getattr(self, "_run_deep_reflection_once", None)
            if callable(deep_reflection):
                _reflection_task = asyncio.create_task(deep_reflection(
                    channel_id=runtime.cid,
                    emotion_scores=emotion_scores,
                    emotion_reason=emotion_reason or "不明",
                ))
                def _on_reflection_done(t: asyncio.Task[Any]) -> None:
                    if t.exception():
                        log.warning("deep reflection task failed: %r", t.exception())

                _reflection_task.add_done_callback(_on_reflection_done)

        candidate_saved = False
        if not skip_long_term_memory:
            try:
                store_candidate_fn = _public_attr("maybe_store_training_candidate", maybe_store_training_candidate)
                candidate_saved = await store_candidate_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=trimmed_context_lines,
                    looks_like_reasoning_leak=looks_like_reasoning_leak,
                    looks_like_parrot_reply=looks_like_parrot_reply,
                    is_weather_reply=runtime.is_weather_reply,
                )
            except Exception as e:
                log.exception("training candidate save failed: %s", e)
                candidate_saved = False

        if candidate_saved:
            try:
                update_habit_fn = _public_attr("update_user_habit_profile", update_user_habit_profile)
                await update_habit_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    candidate_reason="auto-saved training candidate",
                )
            except Exception as e:
                log.exception("habit profile update failed: %s", e)

        try:
            build_summary_fn = _public_attr("_build_summary_context_lines", _build_summary_context_lines)
            summary_context_lines = await build_summary_fn(
                message,
                sent,
                base_context_lines=memory_context_lines,
                bot_user_id=runtime.bot_user_id,
                fetch_limit=max(
                    int(cfg_fn("MEMORY_SUMMARY_CONTEXT_LINES", cfg_fn("MEMORY_LONG_CONTEXT_LINES", 30)) or 30),
                    int(cfg_fn("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20),
                ),
            )
        except Exception as e:
            log.exception("summary context build failed: %s", e)
            summary_context_lines = memory_context_lines

        try:
            update_summary_fn = _public_attr("maybe_update_channel_summary", maybe_update_channel_summary)
            await update_summary_fn(
                cfg=cfg_fn,
                store=self.memory_store,
                guild_id=getattr(message.guild, "id", None),
                channel_id=runtime.cid,
                context_lines=summary_context_lines,
            )
        except Exception as e:
            log.exception("channel summary update failed: %s", e)

        try:
            mark_future = getattr(self, "_mark_future_prompts_triggered", None)
            if callable(mark_future):
                await mark_future(runtime)
        except Exception as e:
            log.exception("prospective trigger update failed: %s", e)

        if not skip_long_term_memory:
            remember_habit = getattr(self, "_remember_pending_habit_turn", None)
            if callable(remember_habit):
                remember_habit(runtime, incoming_emotion_scores=incoming_emotion_scores)

    async def _get_user_aliases_for_message(self, message: discord.Message) -> tuple[int | None, list[str]]:
        uid = author_id(message)
        if uid is None:
            return None, []
        try:
            from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
            aliases = await get_user_aliases_from_store(
                uid,
                guild=getattr(message, "guild", None),
                memory_store=getattr(self, "memory_store", None),
            )
            return uid, aliases
        except Exception:
            return uid, []

    async def _execute_reply_model_single_attempt(
        self,
        message: discord.Message,
        prompt_with_suffix: str,
        *,
        system_prompt: str,
        images: list[str] | None,
        num_predict: int | None,
        raw_prompt: str | None = None,
    ) -> str:
        # ハーネスオーケストレーター経由での実行
        AgentOrchestrator = None
        OrchestratorConfig = None
        tool_registry = None
        try:
            from harness.agent import (
                AgentOrchestrator as _AO,
                OrchestratorConfig as _OC,
                get_default_registry as _GDR,
                register_builtin_tools as _RBT,
            )
            AgentOrchestrator = _AO
            OrchestratorConfig = _OC
            if callable(_RBT):
                tool_registry = _RBT(enable_web_search=True)
            elif callable(_GDR):
                tool_registry = _GDR()
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
            )

        async def _discord_llm_chat(messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            tools = kwargs.get("tools")
            num_ctx_val = kwargs.get("num_ctx") or cfg_int("OLLAMA_NUM_CTX", 16384)
            if tools:
                raw_res = await call_ollama_chat_raw(
                    messages=messages,
                    tools=tools,
                    model=str(kwargs.get("model") or cfg("OLLAMA_MODEL", "") or "").strip() or None,
                    think=bool(kwargs.get("think", False)),
                    timeout_sec=kwargs.get("timeout_sec"),
                    temperature=kwargs.get("temperature"),
                    num_predict=num_predict if num_predict is not None else kwargs.get("num_predict"),
                    num_ctx=num_ctx_val,
                )
                raw_content = getattr(raw_res, "content", "") if not isinstance(raw_res, dict) else str(raw_res.get("content") or "")
                tool_calls = getattr(raw_res, "tool_calls", None) if not isinstance(raw_res, dict) else raw_res.get("tool_calls")
                msg_payload: dict[str, Any] = {"role": "assistant", "content": raw_content}
                if tool_calls:
                    msg_payload["tool_calls"] = tool_calls
                return {"message": msg_payload}

            last_user_msg = messages[-1]["content"] if messages else prompt_with_suffix
            raw_out = await self._call_with_typing(
                message,
                last_user_msg,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
            )
            return {"message": {"role": "assistant", "content": raw_out}}

        try:
            orch = AgentOrchestrator(
                config=OrchestratorConfig(
                    max_turns=cfg_int("ORCHESTRATOR_MAX_TURNS", 3),
                    model_name=str(cfg("OLLAMA_MODEL", "") or "").strip(),
                    think=str(cfg("OLLAMA_MAIN_MODEL_THINK", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
                    timeout_sec=cfg_float("OLLAMA_TIMEOUT_SEC", 180.0),
                    num_ctx=cfg_int("OLLAMA_NUM_CTX", 16384),
                ),
                tool_registry=tool_registry,
                llm_client=_discord_llm_chat,
            )
            target_name = display_name(message).strip()
            latest_ref = await resolve_reference_message(message)
            cid = getattr(getattr(message, "channel", None), "id", None)
            cached_items = recent_context_items(self.channel_context_cache, cid, before_message=message, max_age_sec=3600) if cid else []
            other_names = {
                item["line"].split(":", 1)[0].strip()
                for item in cached_items
                if item.get("line") and ":" in item["line"]
                and item["line"].split(":", 1)[0].strip() != target_name
            }
            if latest_ref is not None:
                ref_author = display_name(latest_ref).strip()
                if ref_author and ref_author != target_name:
                    other_names.add(ref_author)

            orch_res = await orch.run(
                user_prompt=raw_prompt or prompt_with_suffix,
                system_prompt=system_prompt,
                context={
                    "target_name": target_name,
                    "other_names": other_names,
                    "channel_id": cid,
                    "user_id": getattr(getattr(message, "author", None), "id", None),
                },
            )
            if orch_res.final_response:
                return orch_res.final_response
            log.warning("orchestrator returned empty response; falling back to direct model call")
            return await self._call_with_typing(
                message,
                prompt_with_suffix,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
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
            )

    async def _try_compact_recovery_reply(
        self,
        message: discord.Message,
        *,
        system_prompt: str,
        images: list[str] | None,
        num_predict: int | None,
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
            )
            return sanitize_generated_reply(reply.strip())
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
    ) -> str:
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
                )
                if reply:
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

    async def _generate_break_reply(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
        allow_unclear_intent_fallback: bool = True,
        spontaneous_source: str | None = None,
    ) -> tuple[str, str, str, dict[str, Any]]:
        source = str(spontaneous_source or "").strip().lower()
        if source == "auto":
            source = choose_spontaneous_source()

        if source == "x":
            try:
                return await self._generate_break_reply_from_x(
                    message,
                    runtime,
                    incoming_emotion_scores=incoming_emotion_scores,
                    skip_model=skip_model,
                )
            except Exception as e:
                log.warning("x spontaneous break reply failed, fallback to memory: %r", e)

        if source == "img":
            try:
                return await self._generate_break_reply_from_img(
                    message,
                    runtime,
                    incoming_emotion_scores=incoming_emotion_scores,
                    skip_model=skip_model,
                )
            except Exception as e:
                log.warning("img spontaneous break reply failed, fallback to memory: %r", e)

        if source == "user_interest":
            try:
                return await self._generate_break_reply_from_user_interest(
                    message,
                    runtime,
                    incoming_emotion_scores=incoming_emotion_scores,
                    skip_model=skip_model,
                )
            except Exception as e:
                log.warning("user_interest spontaneous break reply failed, fallback to memory: %r", e)

        return await self._generate_break_reply_from_memory(
            message,
            runtime,
            incoming_emotion_scores=incoming_emotion_scores,
            skip_model=skip_model,
            allow_unclear_intent_fallback=allow_unclear_intent_fallback,
        )

    async def _generate_break_reply_from_memory(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
        allow_unclear_intent_fallback: bool = True,
    ) -> tuple[str, str, str, dict[str, Any]]:
        async with self._emotion_lock:
            current_emotion_state = self.emotion_state
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        user_text_for_prompt = (runtime.effective_user_text or runtime.original_user_text or runtime.media.analysis_text or "（本文なし）").strip()
        raw_memory_lines, raw_channel_summary = await get_relevant_memories(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            preferred_emotion_tags=await self._get_preferred_memory_emotion_tags(incoming_emotion_scores),
            user_text=user_text_for_prompt,
        )
        extra_memories = [m for m in runtime.recalled_topic_memories if m]
        for em in reversed(extra_memories):
            if em not in raw_memory_lines:
                raw_memory_lines.insert(0, em)
        raw_memory_lines = sanitize_memory_candidates(raw_memory_lines, last_assistant_text=last_assistant_text)

        core_beliefs = await self._get_core_beliefs_for_prompt(message)
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )
        guild_id_val = getattr(message.guild, "id", None)
        user_relationships_val = getattr(self, "user_relationships", None)
        author_name_val = getattr(getattr(message, "author", None), "name", "")
        author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
        exclude_names = [author_disp_val, author_name_val] if author_disp_val or author_name_val else None

        resolved_mentioned_users, mentioned_parts = await user_profile_logic.resolve_and_format_mentioned_users(
            user_text=user_text_for_prompt,
            context_lines=runtime.context_lines,
            guild_id=guild_id_val,
            exclude_user_id=author_id(message),
            exclude_names=exclude_names,
            memory_store=self.memory_store,
            user_relationships=user_relationships_val,
            cfg_fn=cfg,
            current_user_name=author_disp_val,
            guild=getattr(message, "guild", None),
        )
        if not resolved_mentioned_users and runtime.matched_subject_member:
            msm = runtime.matched_subject_member
            disp_name = str(msm.get("current_display_name") or msm.get("current_name") or "").strip()
            uid = int(msm.get("user_id") or 0)
            if disp_name and uid != author_id(message):
                resolved_mentioned_users = [{
                    "user_id": uid,
                    "current_display_name": disp_name,
                    "current_name": str(msm.get("current_name") or ""),
                    "matched_name": disp_name,
                    "is_alias": False,
                    "is_self": False,
                    "aliases": list(msm.get("aliases") or []),
                    "roles": list(msm.get("roles") or []),
                }]
                mentioned_parts = user_profile_logic.format_mentioned_users_for_prompt(
                    resolved_mentioned_users,
                    relationships=user_relationships_val,
                    user_text=user_text_for_prompt,
                    context_lines=runtime.context_lines,
                    current_user_name=author_disp_val,
                )

        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        bot_user_id_val = getattr(getattr(self.bot, "user", None), "id", None)
        ref_obj = getattr(message, "reference", None)
        ref_msg_obj = getattr(ref_obj, "resolved", None) if ref_obj else None
        gen_is_reply_to_bot = bool(
            ref_msg_obj
            and getattr(ref_msg_obj, "author", None)
            and getattr(ref_msg_obj.author, "id", None) == bot_user_id_val
        ) if bot_user_id_val else False

        intent_info, memory_lines, channel_summary, deliberation_info = await _analyze_pre_reply_context(
            current_user_text=user_text_for_prompt,
            last_assistant_text=last_assistant_text,
            memory_lines=raw_memory_lines,
            channel_summary=raw_channel_summary,
            preferred_emotion_tags=await self._get_preferred_memory_emotion_tags(incoming_emotion_scores),
            is_reply_to_bot=gen_is_reply_to_bot,
            bot_identity=bot_identity,
            mentioned_users=resolved_mentioned_users,
            current_user_name=author_disp_val,
        )
        memory_lines = sanitize_memory_candidates(memory_lines, last_assistant_text=last_assistant_text)
        intent_info = {**intent_info, "spontaneous_source": "memory"}
        recent_turns: list[dict[str, Any]] = []
        for line in runtime.context_lines[-8:]:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({
                "role": "assistant" if name == "Assistant" else "user",
                "text": body.strip(),
            })
        working_context = build_working_context(
            recent_turns,
            channel_summary=channel_summary,
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store,
            persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            text=user_text_for_prompt,
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        primary_name = bot_identity.primary_name
        break_parts = build_break_prompt_parts(
            user_text_for_prompt,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            channel_summary=channel_summary,
            memory_lines=memory_lines,
            last_assistant_text=last_assistant_text,
            target_is_ai=bool(intent_info.get("target_is_ai")),
            should_clarify=bool(intent_info.get("should_clarify")),
            target_name=target_name,
        )
        if mentioned_parts:
            insert_idx = 2
            if len(break_parts) >= insert_idx:
                break_parts = break_parts[:insert_idx] + ["", *mentioned_parts] + break_parts[insert_idx:]
            else:
                break_parts += ["", *mentioned_parts]

        if habit_profile_lines:
            break_parts += [
                "",
                "相手ごとの会話習慣プロファイル:",
                "※軽い傾向として反映し、最新の発言内容を優先してください。",
            ]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        if runtime.media.prompt_parts:
            break_parts += list(runtime.media.prompt_parts)

        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        primary_name = bot_identity.primary_name
        target_uid, target_aliases = await self._get_user_aliases_for_message(message)
        if core_beliefs:
            break_parts.extend(
                format_core_beliefs_for_prompt(
                    core_beliefs,
                    current_user_name=target_name,
                    aliases=target_aliases,
                    target_user_id=target_uid,
                )
            )
        break_parts += [
            "",
            "【現在の対話相手】",
            f"サーバーニックネーム（表示名）: 『{target_name}』さん",
            "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
            f"※相手を呼ぶ・お礼を言う場合は、必ず相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。アカウント名や、あなた自身の名前（{primary_name}等）で相手を呼ぶこと（例: 『ありがとう、{primary_name}』）は絶対に禁止です。",
        ]

        prompt = "\n".join(break_parts)
        prompt = self._prepend_social_guidance_to_prompt(prompt, social_guidance)
        prompt = await self._apply_action_style_to_prompt(
            prompt,
            runtime=runtime,
            intent_info=intent_info,
            deliberation_info=deliberation_info,
            working_context=working_context,
            emotion_state=current_emotion_state,
            pending_future_prompts=pending_intents,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        if allow_unclear_intent_fallback and _should_use_unclear_intent_fallback(user_text_for_prompt, intent_info):
            return prompt, _build_unclear_intent_reply(), last_assistant_text, intent_info

        if any(v for v in deliberation_info.values()):
            deliberation_note = self._format_reply_deliberation_note(deliberation_info, trigger_reason="事前に統合分析しました")
            if deliberation_note:
                prompt = f"{prompt}\n\n{deliberation_note}"
        if skip_model:
            return prompt, "", last_assistant_text, intent_info
        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=self._reply_system_prompt(),
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info),
        )
        return prompt, reply, last_assistant_text, intent_info

    async def _generate_break_reply_from_x(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
    ) -> tuple[str, str, str, dict[str, Any]]:
        async with self._emotion_lock:
            current_emotion_state = self.emotion_state

        last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        user_text_for_prompt = (runtime.effective_user_text or runtime.original_user_text or runtime.media.analysis_text or "（本文なし）").strip()
        core_beliefs = await self._get_core_beliefs_for_prompt(message)
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )
        timeline_limit = max(cfg_int("SPONTANEOUS_X_TIMELINE_POSTS", 10), 1)
        timeline_items = await fetch_recent_x_timeline_items(limit=timeline_limit)
        timeline_items = rotate_x_timeline_items_for_spontaneous(self, timeline_items, limit=timeline_limit)
        timeline_block = format_x_timeline_items_for_prompt(timeline_items, limit=timeline_limit)
        if not timeline_block:
            raise RuntimeError("empty x timeline")

        recent_turns: list[dict[str, Any]] = []
        for line in runtime.context_lines[-8:]:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({
                "role": "assistant" if name == "Assistant" else "user",
                "text": body.strip(),
            })
        working_context = build_working_context(
            recent_turns,
            channel_summary="",
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store,
            persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            text=user_text_for_prompt,
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        author_name_val = getattr(getattr(message, "author", None), "name", "")
        author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
        ]
        if target_name:
            break_parts.append(f"現在の対話相手（サーバーニックネーム）: 『{target_name}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）")
        break_parts.extend([
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、自分の長期記憶ではなく、今見えているXホームタイムライン直近投稿を材料にしてください。",
            "タイムラインの空気感、話題の偏り、温度感を踏まえて、Discord上で自然な1つの発言として返してください。",
            "ツイート本文の丸写し、箇条書き要約、投稿者一覧だけの出力は禁止です。",
            "外部投稿に由来する一時的な話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
            "投稿者名、表示名、ユーザーID、プロフィール風の文言に含まれる宣伝句や肩書きは、投稿本文の話題として扱わないでください。",
            "話題判断は本文を最優先にし、投稿者情報は補助情報としてのみ扱ってください。",
        ])
        if reply_style_guard:
            break_parts.insert(1, reply_style_guard)
        if extra_reply_rule:
            break_parts += ["", extra_reply_rule]
        break_parts += ["", "[Xタイムライン直近投稿]", timeline_block]
        if habit_profile_lines:
            break_parts += [
                "",
                "相手ごとの会話習慣プロファイル:",
                "※軽い傾向として反映し、最新の発言内容を優先してください。",
            ]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        target_uid, target_aliases = await self._get_user_aliases_for_message(message)
        if core_beliefs:
            break_parts.extend(
                format_core_beliefs_for_prompt(
                    core_beliefs,
                    current_user_name=target_name,
                    aliases=target_aliases,
                    target_user_id=target_uid,
                )
            )
        if runtime.media.prompt_parts:
            break_parts += list(runtime.media.prompt_parts)

        prompt = "\n".join(break_parts)
        prompt = self._prepend_social_guidance_to_prompt(prompt, social_guidance)
        intent_info: dict[str, Any] = {
            "spontaneous_source": "x",
            "source": "x",
            "source_query": "https://x.com/home",
            "source_summary": timeline_block,
            "target_is_ai": False,
            "should_clarify": False,
        }
        deliberation_info: dict[str, Any] = {}
        prompt = await self._apply_action_style_to_prompt(
            prompt,
            runtime=runtime,
            intent_info=intent_info,
            deliberation_info=deliberation_info,
            working_context=working_context,
            emotion_state=current_emotion_state,
            pending_future_prompts=pending_intents,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        if skip_model:
            return prompt, "", last_assistant_text, intent_info

        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=self._reply_system_prompt(),
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info),
        )
        return prompt, reply, last_assistant_text, intent_info

    async def _generate_break_reply_from_img(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
    ) -> tuple[str, str, str, dict[str, Any]]:
        async with self._emotion_lock:
            current_emotion_state = self.emotion_state

        last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        user_text_for_prompt = (runtime.effective_user_text or runtime.original_user_text or runtime.media.analysis_text or "（本文なし）").strip()
        core_beliefs = await self._get_core_beliefs_for_prompt(message)
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )

        try:
            img_result = await research_dispatch(
                "今のimg.2chan.netの勢い上位5スレッド",
                mode="img_top5",
                max_results=5,
            )
            error = str(img_result.get("error") or "").strip()
            img_block = str(img_result.get("summary") or "").strip()
            if error:
                raise RuntimeError(error)
            if not img_block:
                raise RuntimeError("empty img top 5")
        except Exception as e:
            raise RuntimeError(f"failed to fetch img top 5: {e}")

        recent_turns: list[dict[str, Any]] = []
        for line in runtime.context_lines[-8:]:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({"role": "assistant" if name == "Assistant" else "user", "text": body.strip()})

        working_context = build_working_context(
            recent_turns, channel_summary="", emotion_state=current_emotion_state, last_bot_text=last_assistant_text
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store, persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None), channel_id=runtime.cid, user_id=author_id(message), text=user_text_for_prompt
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

        author_name_val = getattr(getattr(message, "author", None), "name", "")
        author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
        ]
        if target_name:
            break_parts.append(f"現在の対話相手（サーバーニックネーム）: 『{target_name}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）")
        break_parts.extend([
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、自分の長期記憶ではなく、img.2chan.net(ふたば☆ちゃんねる)の現在の勢い上位5スレッドを材料にしてください。",
            "以下のスレッドから一番面白そうなものを1つ選んで、それを材料にDiscord上で自然な1つの発言をしてください。",
            "提示されたスレッドの中から「あなたが一番面白い・興味を惹かれたスレッド」を1つ選び、その内容を踏まえてDiscord上で自然な1つの発言として返してください。",
            "カタログの丸写し、URLの羅列、箇条書き要約は禁止です。",
            "外部の掲示板から拾ってきた話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
        ])
        if reply_style_guard:
            break_parts.insert(1, reply_style_guard)
        if extra_reply_rule:
            break_parts += ["", extra_reply_rule]
        break_parts += ["", "[img現在の勢い上位5スレッド]", img_block]

        if habit_profile_lines:
            break_parts += ["", "相手ごとの会話習慣プロファイル:", "※軽い傾向として反映し、最新の発言内容を優先してください。"]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        target_uid, target_aliases = await self._get_user_aliases_for_message(message)
        if core_beliefs:
            break_parts.extend(
                format_core_beliefs_for_prompt(
                    core_beliefs,
                    current_user_name=target_name,
                    aliases=target_aliases,
                    target_user_id=target_uid,
                )
            )
        if runtime.media.prompt_parts:
            break_parts += list(runtime.media.prompt_parts)

        prompt = "\n".join(break_parts)
        prompt = self._prepend_social_guidance_to_prompt(prompt, social_guidance)
        intent_info: dict[str, Any] = {
            "spontaneous_source": "img",
            "source": "img",
            "source_query": "https://img.2chan.net/b/futaba.php?mode=cat&sort=6",
            "source_summary": img_block,
            "target_is_ai": False,
            "should_clarify": False,
        }
        deliberation_info: dict[str, Any] = {}

        prompt = await self._apply_action_style_to_prompt(
            prompt, runtime=runtime, intent_info=intent_info, deliberation_info=deliberation_info,
            working_context=working_context, emotion_state=current_emotion_state,
            pending_future_prompts=pending_intents, incoming_emotion_scores=incoming_emotion_scores,
        )
        if skip_model:
            return prompt, "", last_assistant_text, intent_info

        reply = await self._call_reply_model_or_fallback(
            message, prompt, system_prompt=self._reply_system_prompt(), images=runtime.media.images, num_predict=self._reply_num_predict(intent_info=intent_info)
        )
        return prompt, reply, last_assistant_text, intent_info

    async def _generate_break_reply_from_user_interest(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
    ) -> tuple[str, str, str, dict[str, Any]]:
        async with self._emotion_lock:
            current_emotion_state = self.emotion_state

        last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        user_text_for_prompt = (runtime.effective_user_text or runtime.original_user_text or runtime.media.analysis_text or "（本文なし）").strip()
        core_beliefs = await self._get_core_beliefs_for_prompt(message)
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )

        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id_val = getattr(message.guild, "id", None)
        interests = await self.memory_store.list_user_interests(
            persona=persona,
            guild_id=guild_id_val,
            channel_id=runtime.cid,
            limit=5,
            min_interest_level=cfg_float("USER_INTERESTS_MIN_SCORE", 0.3),
        )
        if not interests:
            raise RuntimeError("no user interests found")

        selected_interests = random.sample(interests, min(len(interests), random.randint(1, 2)))
        interest_block = format_user_interests_for_prompt(selected_interests)
        if not interest_block:
            raise RuntimeError("empty user interests block")

        for it in selected_interests:
            if it.get("id"):
                with contextlib.suppress(Exception):
                    await self.memory_store.touch_user_interest(int(it["id"]))

        recent_turns: list[dict[str, Any]] = []
        for line in runtime.context_lines[-8:]:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({"role": "assistant" if name == "Assistant" else "user", "text": body.strip()})

        working_context = build_working_context(
            recent_turns, channel_summary="", emotion_state=current_emotion_state, last_bot_text=last_assistant_text
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store, persona=persona,
            guild_id=guild_id_val, channel_id=runtime.cid, user_id=author_id(message), text=user_text_for_prompt
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

        author_name_val = getattr(getattr(message, "author", None), "name", "")
        author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
        ]
        if target_name:
            break_parts.append(f"現在の対話相手（サーバーニックネーム）: 『{target_name}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）")
        break_parts.extend([
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、直近の会話の流れや一般的な雑談ではなく、サーバーメンバーの関心事を材料にして話題を返してください。",
            "特定のユーザーに自然に尋ねてもよいし（例: 『そういえば〇〇さん、〜〜はどうなった？』）、チャンネル全体に向けてその話題を投げかけてもよい。",
            "データベースや分析結果を機械的に読み上げるような発言は禁止です。",
            "話題の丸写しや箇条書きは禁止です。",
        ])
        if reply_style_guard:
            break_parts.insert(1, reply_style_guard)
        if extra_reply_rule:
            break_parts += ["", extra_reply_rule]
        break_parts += ["", "[サーバーメンバーの関心事]", interest_block]

        if habit_profile_lines:
            break_parts += ["", "相手ごとの会話習慣プロファイル:", "※軽い傾向として反映し、最新の発言内容を優先してください。"]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        target_uid, target_aliases = await self._get_user_aliases_for_message(message)
        if core_beliefs:
            break_parts.extend(
                format_core_beliefs_for_prompt(
                    core_beliefs,
                    current_user_name=target_name,
                    aliases=target_aliases,
                    target_user_id=target_uid,
                )
            )
        if runtime.media.prompt_parts:
            break_parts += list(runtime.media.prompt_parts)

        prompt = "\n".join(break_parts)
        prompt = self._prepend_social_guidance_to_prompt(prompt, social_guidance)
        intent_info: dict[str, Any] = {
            "spontaneous_source": "user_interest",
            "source": "user_interest",
            "source_query": "user_interests",
            "source_summary": interest_block,
            "target_is_ai": False,
            "should_clarify": False,
        }
        deliberation_info: dict[str, Any] = {}

        prompt = await self._apply_action_style_to_prompt(
            prompt, runtime=runtime, intent_info=intent_info, deliberation_info=deliberation_info,
            working_context=working_context, emotion_state=current_emotion_state,
            pending_future_prompts=pending_intents, incoming_emotion_scores=incoming_emotion_scores,
        )
        if skip_model:
            return prompt, "", last_assistant_text, intent_info

        reply = await self._call_reply_model_or_fallback(
            message, prompt, system_prompt=self._reply_system_prompt(), images=runtime.media.images, num_predict=self._reply_num_predict(intent_info=intent_info)
        )
        return prompt, reply, last_assistant_text, intent_info

    async def _generate_normal_reply(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
        allow_unclear_intent_fallback: bool = True,
    ) -> tuple[str, str, str, dict[str, Any], list[int], list[str]]:
        async with self._emotion_lock:
            current_emotion_state = self.emotion_state
        core_beliefs = await self._get_core_beliefs_for_prompt(message)
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )
        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        prompt, handled_message_ids, context_lines, last_assistant_text, intent_info, deliberation_info, prompt_state = await _build_user_prompt(
            message,
            self.channel_context_cache,
            self.memory_store,
            bot_user_id=runtime.bot_user_id,
            emotion_state=current_emotion_state,
            core_beliefs=core_beliefs,
            effective_user_text=runtime.effective_user_text,
            media_prompt_parts=runtime.media.prompt_parts,
            habit_profile_lines=habit_profile_lines,
            fetch_recent_context_lines=lambda m: fetch_recent_context_lines(
                m,
                limit=cfg_int("CONTEXT_WINDOW_MESSAGES", 7),
                bot_user_id=runtime.bot_user_id,
                max_age_sec=max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0),
            ),
            find_last_assistant_message_text=_find_last_assistant_message_text,
            preferred_emotion_tags=await self._get_preferred_memory_emotion_tags(incoming_emotion_scores),
            user_relationships=getattr(self, "user_relationships", None),
            bot_identity=bot_identity,
            thread_tracker=getattr(self, "thread_tracker", None),
        )
        runtime.working_context = dict(prompt_state.get("working_context") or {})
        runtime.pending_future_prompts = list(prompt_state.get("pending_intents") or [])

        if allow_unclear_intent_fallback and _should_use_unclear_intent_fallback(runtime.effective_user_text or runtime.original_user_text, intent_info):
            return prompt, _build_unclear_intent_reply(), last_assistant_text, intent_info, handled_message_ids, context_lines

        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        prompt = self._prepend_social_guidance_to_prompt(prompt, social_guidance)
        prompt = await self._apply_action_style_to_prompt(
            prompt,
            runtime=runtime,
            intent_info=intent_info,
            deliberation_info=deliberation_info,
            working_context=dict(prompt_state.get("working_context") or {}),
            emotion_state=current_emotion_state,
            pending_future_prompts=list(prompt_state.get("pending_intents") or []),
            incoming_emotion_scores=incoming_emotion_scores,
        )

        if any(v for v in deliberation_info.values()):
            deliberation_note = self._format_reply_deliberation_note(deliberation_info, trigger_reason="事前に統合分析しました")
            if deliberation_note:
                prompt = f"{prompt}\n\n{deliberation_note}"
        if skip_model:
            return prompt, "", last_assistant_text, intent_info, handled_message_ids, context_lines

        reply_content = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=self._reply_system_prompt(),
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info),
        )

        return prompt, reply_content, last_assistant_text, intent_info, handled_message_ids, context_lines


OllamaChatReplyGenerationMixin = OllamaChatReplyCoreMixin
