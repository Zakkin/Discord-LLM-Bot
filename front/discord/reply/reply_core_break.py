"""OllamaChatReplyCoreMixinの自発発話・話題転換（Break）返信を担当するMixin。"""
from __future__ import annotations

import contextlib
import random
import re
from typing import Any, Sequence, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from front.discord.common.bot_identity import resolve_bot_identity
from harness.agent.memory_manager.logic import (
    get_relevant_memories,
    get_user_habit_profile_lines,
)
from harness.agent.prompt_parser.parser.reply_helpers import (
    _build_unclear_intent_reply,
    _should_use_unclear_intent_fallback,
)
from harness.agent.prompt_parser.prompt_manager import (
    prospective_memory,
    user_profile_logic,
)
from harness.agent.prompt_parser.prompt_manager.budget import (
    format_core_beliefs_for_prompt,
    sanitize_memory_candidates,
)
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import (
    format_user_interests_for_prompt,
)
from harness.agent.prompt_parser.prompt_manager.working_memory import (
    build_working_context,
    format_working_context_for_prompt,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    author_id,
    choose_spontaneous_source,
    format_x_timeline_items_for_prompt,
    rotate_x_timeline_items_for_spontaneous,
)
from ollama_bot.ollama_chat_.ollama_chat_texts import build_break_prompt_parts
from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime
from front.discord.reply.reply_core_common import (
    _OllamaChatReplyCoreBase,
    _analyze_pre_reply_context,
    _find_last_assistant_message_text,
    cfg,
    cfg_float,
    cfg_int,
    fetch_recent_x_timeline_items,
    log,
    research_dispatch,
)


class ReplyBreakMixin(_OllamaChatReplyCoreBase):
    """自発発話・ブレイク返信の生成（メモリ、X、IMG、ユーザー関心）を担当するMixin。"""

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
        self._ensure_background_llm_serialization()
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
            context_lines=runtime.context_lines,
        )
        topic_origin_val = str((deliberation_info or {}).get("topic_origin") or "").strip()
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
        active_threads_data, current_thread_topic = self._get_thread_context_for_working_memory(
            message, runtime, bot_identity
        )
        if topic_origin_val:
            topic_origin_lower = topic_origin_val.lower()
            filtered_active_threads: list[dict[str, Any]] = []
            for t in active_threads_data:
                t_topic = str(t.get("topic") or "").strip()
                participants = [str(p).strip() for p in (t.get("participants") or []) if str(p).strip()]
                overlap = False
                if t_topic and (t_topic.lower() in topic_origin_lower or topic_origin_lower in t_topic.lower()):
                    overlap = True
                else:
                    for p in participants:
                        p_lower = p.lower()
                        if p_lower in topic_origin_lower:
                            overlap = True
                            break
                        p_sub = re.sub(r"[\s\W_]+", "", p_lower)
                        if len(p_sub) >= 2 and p_sub in topic_origin_lower:
                            overlap = True
                            break
                if not overlap:
                    filtered_active_threads.append(t)
            active_threads_data = filtered_active_threads
            if not current_thread_topic or current_thread_topic in ("雑談", "general"):
                current_thread_topic = topic_origin_val

        working_context = build_working_context(
            recent_turns,
            channel_summary=channel_summary,
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
            active_threads=active_threads_data,
            current_thread_topic=current_thread_topic,
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
        working_context["is_nsfw"] = bool(
            getattr(message.channel, "is_nsfw", lambda: False)()
            if hasattr(message, "channel") and callable(getattr(message.channel, "is_nsfw", None))
            else getattr(getattr(message, "channel", None), "nsfw", False)
        )
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        target_name = author_disp_val or f"ユーザー_{author_id(message)}"
        primary_name = bot_identity.primary_name
        reply_system = self._reply_system_prompt()
        eff_style_guard = reply_style_guard if reply_style_guard and reply_style_guard not in reply_system else ""
        eff_extra_rule = extra_reply_rule if extra_reply_rule and extra_reply_rule not in reply_system else ""
        try:
            break_parts = build_break_prompt_parts(
                user_text_for_prompt,
                reply_style_guard=eff_style_guard,
                extra_reply_rule=eff_extra_rule,
                channel_summary=channel_summary,
                memory_lines=memory_lines,
                last_assistant_text=last_assistant_text,
                target_is_ai=bool(intent_info.get("target_is_ai")),
                should_clarify=bool(intent_info.get("should_clarify")),
                target_name=target_name,
                context_lines=runtime.context_lines[-5:],
                topic_origin=topic_origin_val,
            )
        except TypeError:
            try:
                break_parts = build_break_prompt_parts(
                    user_text_for_prompt,
                    reply_style_guard=eff_style_guard,
                    extra_reply_rule=eff_extra_rule,
                    channel_summary=channel_summary,
                    memory_lines=memory_lines,
                    last_assistant_text=last_assistant_text,
                    target_is_ai=bool(intent_info.get("target_is_ai")),
                    should_clarify=bool(intent_info.get("should_clarify")),
                    target_name=target_name,
                    context_lines=runtime.context_lines[-5:],
                )
            except TypeError:
                break_parts = build_break_prompt_parts(
                    user_text_for_prompt,
                    reply_style_guard=eff_style_guard,
                    extra_reply_rule=eff_extra_rule,
                    channel_summary=channel_summary,
                    memory_lines=memory_lines,
                    last_assistant_text=last_assistant_text,
                    target_is_ai=bool(intent_info.get("target_is_ai")),
                    should_clarify=bool(intent_info.get("should_clarify")),
                    target_name=target_name,
                )
        if topic_origin_val:
            for idx, part in enumerate(break_parts):
                if "以下のメッセージは、直前の流れとは別話題である可能性があります。" in part:
                    break_parts[idx] = part.replace(
                        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
                        f"以下のメッセージは、直前の会話（{topic_origin_val}）を踏まえた発言・ツッコミです。",
                    )
        if mentioned_parts:
            insert_idx = 2
            if len(break_parts) >= insert_idx:
                break_parts = break_parts[:insert_idx] + ["", *mentioned_parts] + break_parts[insert_idx:]
            else:
                break_parts += ["", *mentioned_parts]

        working_context_lines = format_working_context_for_prompt(working_context)
        if working_context_lines:
            break_parts += ["", *working_context_lines]

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
        prompt = self._apply_different_author_boundary_note(prompt, message)
        reply_system = self._reply_system_prompt()
        prompt = self._clean_duplicate_prompt_instructions(prompt, reply_system)
        log.info(
            "_generate_break_reply_from_memory: building break prompt (sys_chars=%d, user_chars=%d)",
            len(reply_system),
            len(prompt),
        )
        if skip_model:
            return prompt, "", last_assistant_text, intent_info
        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=reply_system,
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info, prompt_text=prompt, system_prompt=reply_system),
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

        # 自己言及リコール向け言及根拠メモの構築
        ref_notes: list[str] = []
        for item in timeline_items[:3]:
            author = item.get("handle") or item.get("author") or "不明"
            text = (item.get("text") or "").strip().replace("\n", " ")[:80]
            ref_notes.append(f"Xタイムラインで見た投稿: @{author}: {text}")
        if ref_notes:
            runtime.utterance_reference_note = "\n".join(ref_notes)

        if ref_notes and hasattr(self, "memory_store") and bool(cfg("ACTIVITY_LOG_ENABLED", True)):
            add_act_fn = getattr(self.memory_store, "add_bot_activity", None)
            if callable(add_act_fn):
                try:
                    combined_obs = " | ".join(ref_notes)
                    await add_act_fn(
                        persona=cfg_persona_namespace(custom_cfg=cfg),
                        front="x",
                        kind="observed",
                        summary=combined_obs[:400],
                        guild_id=None,
                        channel_id=None,
                    )
                except Exception as e:
                    log.debug("failed to record x observation activity: %s", e)

        recent_turns: list[dict[str, Any]] = []
        recent_lines = runtime.context_lines[-5:] if runtime.context_lines else []
        for line in recent_lines:
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
        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        active_threads_data, current_thread_topic = self._get_thread_context_for_working_memory(
            message, runtime, bot_identity
        )
        channel_summary = ""
        if self.memory_store and runtime.cid is not None:
            with contextlib.suppress(Exception):
                summary_record = await self.memory_store.get_channel_summary(
                    persona=cfg_persona_namespace(custom_cfg=cfg),
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                )
                if summary_record and summary_record.get("summary"):
                    channel_summary = str(summary_record["summary"]).strip()

        working_context = build_working_context(
            recent_turns,
            channel_summary=channel_summary,
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
            active_threads=active_threads_data,
            current_thread_topic=current_thread_topic,
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
        working_context["is_nsfw"] = bool(
            getattr(message.channel, "is_nsfw", lambda: False)()
            if hasattr(message, "channel") and callable(getattr(message.channel, "is_nsfw", None))
            else getattr(getattr(message, "channel", None), "nsfw", False)
        )
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_system = self._reply_system_prompt()
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        eff_style_guard = reply_style_guard if reply_style_guard and reply_style_guard not in reply_system else ""
        eff_extra_rule = extra_reply_rule if extra_reply_rule and extra_reply_rule not in reply_system else ""
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
        if eff_style_guard:
            break_parts.insert(1, eff_style_guard)
        if eff_extra_rule:
            break_parts += ["", eff_extra_rule]
        break_parts += ["", "[Xタイムライン直近投稿]", timeline_block]
        working_context_lines = format_working_context_for_prompt(working_context)
        if working_context_lines:
            break_parts += ["", *working_context_lines]
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

        reply_system = self._reply_system_prompt()
        prompt = self._clean_duplicate_prompt_instructions(prompt, reply_system)
        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=reply_system,
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info, prompt_text=prompt, system_prompt=reply_system),
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
        recent_lines = runtime.context_lines[-5:] if runtime.context_lines else []
        for line in recent_lines:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({"role": "assistant" if name == "Assistant" else "user", "text": body.strip()})

        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        active_threads_data, current_thread_topic = self._get_thread_context_for_working_memory(
            message, runtime, bot_identity
        )
        channel_summary = ""
        if self.memory_store and runtime.cid is not None:
            with contextlib.suppress(Exception):
                summary_record = await self.memory_store.get_channel_summary(
                    persona=cfg_persona_namespace(custom_cfg=cfg),
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                )
                if summary_record and summary_record.get("summary"):
                    channel_summary = str(summary_record["summary"]).strip()

        working_context = build_working_context(
            recent_turns,
            channel_summary=channel_summary,
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
            active_threads=active_threads_data,
            current_thread_topic=current_thread_topic,
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store, persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None), channel_id=runtime.cid, user_id=author_id(message), text=user_text_for_prompt
        )
        working_context["future_prompts"] = pending_intents
        working_context["is_nsfw"] = bool(
            getattr(message.channel, "is_nsfw", lambda: False)()
            if hasattr(message, "channel") and callable(getattr(message.channel, "is_nsfw", None))
            else getattr(getattr(message, "channel", None), "nsfw", False)
        )
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_system = self._reply_system_prompt()
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        eff_style_guard = reply_style_guard if reply_style_guard and reply_style_guard not in reply_system else ""
        eff_extra_rule = extra_reply_rule if extra_reply_rule and extra_reply_rule not in reply_system else ""

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
        if eff_style_guard:
            break_parts.insert(1, eff_style_guard)
        if eff_extra_rule:
            break_parts += ["", eff_extra_rule]
        break_parts += ["", "[img現在の勢い上位5スレッド]", img_block]
        working_context_lines = format_working_context_for_prompt(working_context)
        if working_context_lines:
            break_parts += ["", *working_context_lines]

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

        reply_system = self._reply_system_prompt()
        prompt = self._clean_duplicate_prompt_instructions(prompt, reply_system)
        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=reply_system,
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info, prompt_text=prompt, system_prompt=reply_system),
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
        recent_lines = runtime.context_lines[-5:] if runtime.context_lines else []
        for line in recent_lines:
            text = str(line or "").strip()
            if not text:
                continue
            if ": " in text:
                name, body = text.split(": ", 1)
            else:
                name, body = "", text
            recent_turns.append({"role": "assistant" if name == "Assistant" else "user", "text": body.strip()})

        bot_identity = resolve_bot_identity(bot=self.bot, message=message)
        active_threads_data, current_thread_topic = self._get_thread_context_for_working_memory(
            message, runtime, bot_identity
        )
        channel_summary = ""
        if self.memory_store and runtime.cid is not None:
            with contextlib.suppress(Exception):
                summary_record = await self.memory_store.get_channel_summary(
                    persona=persona,
                    guild_id=guild_id_val,
                    channel_id=runtime.cid,
                )
                if summary_record and summary_record.get("summary"):
                    channel_summary = str(summary_record["summary"]).strip()

        working_context = build_working_context(
            recent_turns,
            channel_summary=channel_summary,
            emotion_state=current_emotion_state,
            last_bot_text=last_assistant_text,
            active_threads=active_threads_data,
            current_thread_topic=current_thread_topic,
        )
        pending_intents = await prospective_memory.match_cues(
            store=self.memory_store, persona=persona,
            guild_id=guild_id_val, channel_id=runtime.cid, user_id=author_id(message), text=user_text_for_prompt
        )
        working_context["future_prompts"] = pending_intents
        working_context["is_nsfw"] = bool(
            getattr(message.channel, "is_nsfw", lambda: False)()
            if hasattr(message, "channel") and callable(getattr(message.channel, "is_nsfw", None))
            else getattr(getattr(message, "channel", None), "nsfw", False)
        )
        social_guidance = await self._get_current_reply_social_guidance(runtime, incoming_emotion_scores)
        reply_system = self._reply_system_prompt()
        reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        eff_style_guard = reply_style_guard if reply_style_guard and reply_style_guard not in reply_system else ""
        eff_extra_rule = extra_reply_rule if extra_reply_rule and extra_reply_rule not in reply_system else ""

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
        if eff_style_guard:
            break_parts.insert(1, eff_style_guard)
        if eff_extra_rule:
            break_parts += ["", eff_extra_rule]
        break_parts += ["", "[サーバーメンバーの関心事]", interest_block]
        working_context_lines = format_working_context_for_prompt(working_context)
        if working_context_lines:
            break_parts += ["", *working_context_lines]

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

        reply_system = self._reply_system_prompt()
        prompt = self._clean_duplicate_prompt_instructions(prompt, reply_system)
        reply = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=reply_system,
            images=runtime.media.images,
            num_predict=self._reply_num_predict(intent_info=intent_info, prompt_text=prompt, system_prompt=reply_system),
        )
        return prompt, reply, last_assistant_text, intent_info

