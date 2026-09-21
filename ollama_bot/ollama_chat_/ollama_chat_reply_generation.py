"""OllamaChatReplyMixinの通常返答と割り込み返答のプロンプト生成を担当する補助Mixin。

このファイルはDiscord文脈、記憶、感情状態、会話習慣、X/imgなどの外部ソースを
統合してLLMへ渡すプロンプトを作り、モデル呼び出し直前までの返答生成フローを扱う。
出力の再試行ガードはollama_chat_reply_guards.py、返信後の保存処理は
ollama_chat_reply_memory.pyに分離している。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr


import discord

from ..common import prospective_memory, user_profile_logic
from ..common.config_helpers import cfg_float
from ..common.discord_helpers import author_id, channel_id
from ..common.user_interest_logic import format_user_interests_for_prompt
from ..common.memory.logic import (
    get_relevant_memories,
    get_user_habit_profile_lines,
)
from ..common.bot_identity import resolve_bot_identity
from ..common.reply_helpers import (
    _build_unclear_intent_reply,
    _should_use_unclear_intent_fallback,
)
from ..common.working_memory import build_working_context
from . import ollama_chat_helpers as _helpers
from .ollama_chat_helpers import (
    _analyze_pre_reply_context,
    _build_user_prompt,
    _format_reply,
    _merge_user_text_with_media_analysis,
    _strip_user_echo_prefix,
    append_chat_reply_output_suffix,
    build_current_time_reply,
    call_ollama,
    choose_spontaneous_source,
    format_x_timeline_items_for_prompt,
    looks_like_time_query,
    looks_like_weather_query,
    model_supports_thinking,
    rotate_x_timeline_items_for_spontaneous,
    sanitize_generated_reply,
    to_context_line,
    truncate_lines,
    truncate_text,
)
from .ollama_chat_texts import build_break_prompt_parts
from .ollama_chat_types import MessageRuntime

log = logging.getLogger("ollama_bot.ollama_chat_.reply_generation")

_PUBLIC_REPLY_MODULE = f"{__package__}.ollama_chat_reply"
_default_cfg = _helpers.cfg
_default_cfg_int = _helpers.cfg_int
_default_fetch_recent_context_lines = _helpers.fetch_recent_context_lines
_default_find_last_assistant_message_text = _helpers._find_last_assistant_message_text
_default_fetch_recent_x_timeline_items = _helpers.fetch_recent_x_timeline_items
_default_research_dispatch = _helpers.research_dispatch


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)



def cfg(name: str, default: Any = None) -> Any:
    return _public_attr("cfg", _default_cfg)(name, default)


def cfg_int(name: str, default: int) -> int:
    return _public_attr("cfg_int", _default_cfg_int)(name, default)


def fetch_recent_context_lines(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("fetch_recent_context_lines", _default_fetch_recent_context_lines)(*args, **kwargs)


def _find_last_assistant_message_text(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("_find_last_assistant_message_text", _default_find_last_assistant_message_text)(*args, **kwargs)


def fetch_recent_x_timeline_items(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("fetch_recent_x_timeline_items", _default_fetch_recent_x_timeline_items)(*args, **kwargs)


def research_dispatch(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("research_dispatch", _default_research_dispatch)(*args, **kwargs)


if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChatReplyGenerationBase = OllamaChatProtocol
else:
    _OllamaChatReplyGenerationBase = object


def _is_transient_reply_model_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True
    exc_type = type(exc)
    module = str(getattr(exc_type, "__module__", "") or "").lower()
    name = str(getattr(exc_type, "__name__", "") or "").lower()
    if "aiohttp" in module:
        return True
    return any(marker in name for marker in ("timeout", "client", "socket", "serverdisconnected"))


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


class OllamaChatReplyGenerationMixin(_OllamaChatReplyGenerationBase):
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
        try:
            return await self._call_with_typing(
                message,
                prompt_with_suffix,
                system_prompt=system_prompt,
                images=images,
                num_predict=num_predict,
            )
        except Exception as e:
            if not _is_transient_reply_model_error(e):
                raise
            log.warning(
                "reply model call failed; using fallback channel=%s message=%s err=%r",
                channel_id(message),
                getattr(message, "id", None),
                e,
            )
            return _reply_model_timeout_fallback()

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
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )
        # 会話中で言及されたサーバーメンバーの事前解決
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
        )

        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        intent_info, memory_lines, channel_summary, deliberation_info = await _analyze_pre_reply_context(
            current_user_text=user_text_for_prompt,
            last_assistant_text=last_assistant_text,
            memory_lines=raw_memory_lines,
            channel_summary=raw_channel_summary,
            preferred_emotion_tags=await self._get_preferred_memory_emotion_tags(incoming_emotion_scores),
            bot_identity=bot_identity,
            mentioned_users=resolved_mentioned_users,
        )
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
            persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            text=user_text_for_prompt,
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        break_parts = build_break_prompt_parts(
            user_text_for_prompt,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            channel_summary=channel_summary,
            memory_lines=memory_lines,
            last_assistant_text=last_assistant_text,
            target_is_ai=bool(intent_info.get("target_is_ai")),
            should_clarify=bool(intent_info.get("should_clarify")),
        )
        if mentioned_parts:
            # 対象メッセージの直後に言及メンバーブロックを挿入して最優先情報として認知させる
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
        from ..common.bot_identity import resolve_bot_identity
        bot_ident = resolve_bot_identity(bot=self.bot, message=message)
        primary_name = bot_ident.primary_name
        break_parts += [
            "",
            "【現在の対話相手】",
            f"ユーザー名: {target_name}",
            "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
            f"※相手を呼ぶ・お礼を言う場合は、必ず現在の対話相手の名前（『{target_name}』さん等）を使ってください。あなた自身の名前（{primary_name}等）で相手を呼ぶこと（例: 『ありがとう、{primary_name}』）は絶対に禁止です。",
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
            persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            text=user_text_for_prompt,
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、自分の長期記憶ではなく、今見えているXホームタイムライン直近投稿を材料にしてください。",
            "タイムラインの空気感、話題の偏り、温度感を踏まえて、Discord上で自然な1つの発言として返してください。",
            "ツイート本文の丸写し、箇条書き要約、投稿者一覧だけの出力は禁止です。",
            "外部投稿に由来する一時的な話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
            "投稿者名、表示名、ユーザーID、プロフィール風の文言に含まれる宣伝句や肩書きは、投稿本文の話題として扱わないでください。",
            "話題判断は本文を最優先にし、投稿者情報は補助情報としてのみ扱ってください。",
        ]
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
            if not text: continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({"role": "assistant" if name == "Assistant" else "user", "text": body.strip()})

        working_context = build_working_context(
            recent_turns, channel_summary="", emotion_state=current_emotion_state, last_bot_text=last_assistant_text
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store, persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            guild_id=getattr(message.guild, "id", None), channel_id=runtime.cid, user_id=author_id(message), text=user_text_for_prompt
        )
        working_context["future_prompts"] = pending_intents
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、自分の長期記憶ではなく、img.2chan.net(ふたば☆ちゃんねる)の現在の勢い上位5スレッドを材料にしてください。",
            "以下のスレッドから一番面白そうなものを1つ選んで、それを材料にDiscord上で自然な1つの発言をしてください。",
            "提示されたスレッドの中から「あなたが一番面白い・興味を惹かれたスレッド」を1つ選び、その内容を踏まえてDiscord上で自然な1つの発言として返してください。",
            "カタログの丸写し、URLの羅列、箇条書き要約は禁止です。",
            "外部の掲示板から拾ってきた話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
        ]
        if reply_style_guard: break_parts.insert(1, reply_style_guard)
        if extra_reply_rule: break_parts += ["", extra_reply_rule]
        break_parts += ["", "[img現在の勢い上位5スレッド]", img_block]

        if habit_profile_lines:
            break_parts += ["", "相手ごとの会話習慣プロファイル:", "※軽い傾向として反映し、最新の発言内容を優先してください。"]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        if runtime.media.prompt_parts: break_parts += list(runtime.media.prompt_parts)

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
        if skip_model: return prompt, "", last_assistant_text, intent_info

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
        habit_profile_lines = await get_user_habit_profile_lines(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(message.guild, "id", None),
            user_id=author_id(message),
        )

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip()
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
            if not text: continue
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

        break_parts = [
            "以下のメッセージは、直前の流れとは別話題である可能性があります。",
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "今回は、直近の会話の流れや一般的な雑談ではなく、サーバーメンバーの関心事を材料にして話題を返してください。",
            "特定のユーザーに自然に尋ねてもよいし（例: 『そういえば〇〇さん、〜〜はどうなった？』）、チャンネル全体に向けてその話題を投げかけてもよい。",
            "データベースや分析結果を機械的に読み上げるような発言は禁止です。",
            "話題の丸写しや箇条書きは禁止です。",
        ]
        if reply_style_guard: break_parts.insert(1, reply_style_guard)
        if extra_reply_rule: break_parts += ["", extra_reply_rule]
        break_parts += ["", "[サーバーメンバーの関心事]", interest_block]

        if habit_profile_lines:
            break_parts += ["", "相手ごとの会話習慣プロファイル:", "※軽い傾向として反映し、最新の発言内容を優先してください。"]
            break_parts.extend([f"- {line}" for line in habit_profile_lines])
        if runtime.media.prompt_parts: break_parts += list(runtime.media.prompt_parts)

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
        if skip_model: return prompt, "", last_assistant_text, intent_info

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
