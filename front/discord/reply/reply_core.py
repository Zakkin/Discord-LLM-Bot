"""OllamaChatReplyCoreMixinの合成クラスおよび返信生成コア。

本モジュールは分割された各機能Mixin（prompts, runtime, sending, episode, model_call, break）を
多重継承によって合成し、返信生成の司令塔である _generate_normal_reply を提供する。
また、外部互換性のために必要な全公開シンボルを再エクスポートする。
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any, Sequence, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from front.discord.common.bot_identity import resolve_bot_identity
from front.discord.common.frustration import count_anger_markers
from front.discord.reply.context_compact import compact_context_lines
from front.discord.reply.semantic_guard import assess_reply_risk, inspect_reply_semantics
from harness.agent.memory_manager.logic import get_user_habit_profile_lines
from harness.agent.prompt_parser.parser.reply_helpers import (
    _build_unclear_intent_reply,
    _should_use_unclear_intent_fallback,
)
from harness.agent.prompt_parser.prompt_manager.denied_claims import list_denied_claims
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    author_id,
    content,
)
from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime

# 分割先 Mixin
from front.discord.reply.reply_core_prompts import ReplyPromptMixin
from front.discord.reply.reply_core_runtime import ReplyRuntimeMixin
from front.discord.reply.reply_core_sending import ReplySendingMixin
from front.discord.reply.reply_core_episode import ReplyEpisodeMixin
from front.discord.reply.reply_core_model_call import ReplyModelCallMixin
from front.discord.reply.reply_core_break import ReplyBreakMixin

try:
    from harness.agent.prompt_parser.parser.validation import find_ungrounded_person_names
    from harness.agent.prompt_parser.parser.cleaner import strip_ungrounded_person_sentences
except ImportError:
    # harness側の実装未完了時でも単体動作するフォールバック
    _PERSON_FULLNAME_RE = re.compile(r"[ァ-ヶー]{2,}・[ァ-ヶー]{2,}")
    _KATAKANA_TOKEN_RE = re.compile(r"[ァ-ヶー]{2,}")
    _PERSON_CONTEXT_PATTERN = re.compile(r"(?:の二番煎じ|みたいな|級の|選手|投手|打者|監督|コーチ)")

    def find_ungrounded_person_names(
        comment: str,
        allowed_terms: set[str],
        *,
        stopwords: set[str] | None = None,
        persona_names: set[str] | None = None,
    ) -> list[str]:
        raw = str(comment or "").strip()
        violations: list[str] = []
        seen: set[str] = set()
        for m in _PERSON_FULLNAME_RE.finditer(raw):
            name = m.group(0).strip()
            if name not in allowed_terms and not all(p in allowed_terms for p in name.split("・")):
                if name not in seen:
                    seen.add(name)
                    violations.append(name)
        p_names = set(persona_names or ())
        s_words = set(stopwords or ())
        sentences = re.split(r"(?<=[。！？!?\n])", raw)
        for s in sentences:
            s_clean = s.strip()
            if not s_clean or not _PERSON_CONTEXT_PATTERN.search(s_clean):
                continue
            for km in _KATAKANA_TOKEN_RE.finditer(s_clean):
                token = km.group(0).strip()
                if len(token) < 2 or token in allowed_terms or token in s_words or token in p_names:
                    continue
                if any(token in v for v in violations):
                    continue
                if token not in seen:
                    seen.add(token)
                    violations.append(token)
        return violations

    def strip_ungrounded_person_sentences(text: str, ungrounded_names: Sequence[str]) -> str:
        raw = str(text or "").strip()
        if not raw or not ungrounded_names:
            return raw
        sentences = re.split(r"(?<=[。！？!?\n])", raw)
        kept = [s.strip() for s in sentences if s.strip() and not any(n in s for n in ungrounded_names)]
        res = "\n".join(kept).strip()
        res = re.sub(r"([。！？!?])+[、，]+", r"\1", res)
        return re.sub(r"^[、，\s]+", "", res)

# 共通定義・プロキシ関数・後方互換シンボルの再エクスポート
from front.discord.reply.reply_core_common import (
    ReplyTargetDeletedError,
    _AIZUCHI_PATTERN,
    _OllamaChatReplyCoreBase,
    _PUBLIC_REPLY_MODULE,
    _REPLY_CORE_MODULE,
    _build_summary_context_lines,
    _default_add_prospective,
    _default_build_message_media_context,
    _default_cfg,
    _default_cfg_bool,
    _default_cfg_float,
    _default_cfg_int,
    _default_cfg_managed_channel_ids,
    _default_extract_and_store_memory,
    _default_extract_base64_images,
    _default_fetch_recent_context_lines,
    _default_fetch_recent_x_timeline_items,
    _default_find_last_assistant_message_text,
    _default_list_pending_prospective_memories,
    _default_maybe_store_training_candidate,
    _default_maybe_update_channel_summary,
    _default_research_dispatch,
    _default_resolve_reference_message,
    _default_update_user_habit_profile,
    _default_update_user_interests,
    _find_last_assistant_message_text,
    _is_context_size_exceeded_error,
    _is_transient_reply_model_error,
    _public_attr,
    _reply_model_timeout_fallback,
    _split_reply_text,
    add_prospective,
    build_message_media_context,
    call_ollama_json,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_managed_channel_ids,
    extract_and_store_memory,
    extract_base64_images,
    fetch_recent_context_lines,
    fetch_recent_x_timeline_items,
    list_pending_prospective_memories,
    log,
    maybe_store_training_candidate,
    maybe_update_channel_summary,
    report_error,
    research_dispatch,
    resolve_reference_message,
    update_user_habit_profile,
    update_user_interests,
    _analyze_pre_reply_context,
    _build_user_prompt,
    call_ollama_chat_raw,
    release_ollama_model,
    build_different_author_boundary_note,
    collect_same_author_burst_texts,
    format_context_lines_with_relation_labels,
    format_discord_context_line_with_relation,
    format_message_reactions,
    looks_like_incomplete_topic_opener,
    looks_like_meta_ignorance_reply,
    sanitize_meta_ignorance_reply,
)


class OllamaChatReplyCoreMixin(
    ReplyPromptMixin,
    ReplyRuntimeMixin,
    ReplySendingMixin,
    ReplyEpisodeMixin,
    ReplyModelCallMixin,
    ReplyBreakMixin,
    _OllamaChatReplyCoreBase,
):
    """分割された返信コアMixinの合成クラス。

    全56メソッドの実装は各機能Mixinに委譲され、本体は全体の司令塔である
    _generate_normal_reply のみを保持する。
    """

    async def _generate_normal_reply(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
        skip_model: bool = False,
        allow_unclear_intent_fallback: bool = True,
    ) -> tuple[str, str, str, dict[str, Any], list[int], list[str]]:
        self._ensure_background_llm_serialization()
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

        async def _fetch_annotated_and_compact_lines(m: discord.Message) -> list[str]:
            raw_lines = await fetch_recent_context_lines(
                m,
                limit=cfg_int("CONTEXT_WINDOW_MESSAGES", 7),
                bot_user_id=runtime.bot_user_id,
                max_age_sec=max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0),
            )
            res_lines = list(raw_lines)
            # 注釈適用
            if cfg("CORRECTION_UPTAKE_ENABLED", True):
                try:
                    from harness.agent.prompt_parser.prompt_manager.denied_claims import annotate_context_lines, list_denied_claims
                    p_name = getattr(bot_identity, "primary_name", "default")
                    c_key = str(getattr(m.channel, "id", "default"))
                    claims = list_denied_claims(persona=p_name, channel_key=c_key)
                    res_lines = annotate_context_lines(res_lines, claims)
                except Exception as e:
                    log.debug("annotate_context_lines skipped: %r", e)
            # 圧縮適用
            if cfg("CONTEXT_COMPACT_ENABLED", True):
                try:
                    from .context_compact import compact_context_lines
                    t_lines = int(cfg("CONTEXT_COMPACT_TRIGGER_LINES", 40) or 40)
                    k_recent = int(cfg("CONTEXT_COMPACT_KEEP_RECENT", 12) or 12)
                    res_lines = compact_context_lines(res_lines, trigger_lines=t_lines, keep_recent=k_recent)
                except Exception as e:
                    log.debug("compact_context_lines skipped: %r", e)
            return res_lines

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
            fetch_recent_context_lines=_fetch_annotated_and_compact_lines,
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
        prompt = self._apply_different_author_boundary_note(prompt, message)
        if skip_model:
            return prompt, "", last_assistant_text, intent_info, handled_message_ids, context_lines

        user_raw_text = runtime.effective_user_text or runtime.original_user_text or content(message)
        adaptive_think = self._should_enable_adaptive_thinking(intent_info, user_text=user_raw_text)
        think_override = True if adaptive_think else None
        is_think = bool(adaptive_think or cfg_bool("OLLAMA_MAIN_MODEL_THINK", False))

        reply_system = self._reply_system_prompt()
        prompt = self._clean_duplicate_prompt_instructions(prompt, reply_system)
        num_predict_val = self._reply_num_predict(
            intent_info=intent_info,
            prompt_text=prompt,
            system_prompt=reply_system,
            think=is_think,
        )

        reply_content = await self._call_reply_model_or_fallback(
            message,
            prompt,
            system_prompt=reply_system,
            images=runtime.media.images,
            num_predict=num_predict_val,
            think_override=think_override,
        )

        # 送信前意味検査と泥沼化防止ガード
        if cfg_bool("SEMANTIC_GUARD_ENABLED", True) and reply_content:
            try:
                from .semantic_guard import assess_reply_risk, inspect_reply_semantics
                p_name = getattr(bot_identity, "primary_name", "default")
                c_key = str(getattr(message.channel, "id", "default"))
                denied_claims: list[dict[str, str]] = []
                if cfg("CORRECTION_UPTAKE_ENABLED", True):
                    try:
                        from harness.agent.prompt_parser.prompt_manager.denied_claims import list_denied_claims
                        denied_claims = list_denied_claims(persona=p_name, channel_key=c_key)
                    except Exception as e:
                        log.debug("list_denied_claims skipped: %r", e)

                risk = assess_reply_risk(
                    reply_text=reply_content,
                    intent_info=intent_info,
                    denied_claims=denied_claims,
                    previous_reply_text=last_assistant_text,
                )

                async def _inspect_llm_call(prompt_text: str) -> Any:
                    middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm")
                    schema = {
                        "type": "object",
                        "properties": {
                            "reasserts_denied": {"type": "boolean"},
                            "subject_swap": {"type": "boolean"},
                            "contradicts_latest_user": {"type": "boolean"},
                            "ok": {"type": "boolean"},
                        },
                        "required": ["reasserts_denied", "subject_swap", "contradicts_latest_user", "ok"],
                    }
                    return await call_ollama_json(
                        prompt_text,
                        system_prompt="あなたは会話の整合性を検証する意味検査AIです。",
                        schema=schema,
                        model=middle_model,
                        temperature=0.0,
                        think=False,
                        timeout_sec=float(cfg("SEMANTIC_GUARD_TIMEOUT_SEC", 15.0) or 15.0),
                    )

                user_text_val = runtime.effective_user_text or runtime.original_user_text or content(message)

                violation = False
                violation_reason = ""
                if risk.get("substring_hit"):
                    violation = True
                    violation_reason = f"substring_hit: {risk['substring_hit']}"
                elif risk.get("needs_check"):
                    insp = await inspect_reply_semantics(
                        llm_caller_fn=_inspect_llm_call,
                        user_text=user_text_val,
                        bot_previous_text=last_assistant_text or "",
                        generated_reply=reply_content,
                        denied_claims=denied_claims,
                    )
                    if not insp.get("ok", True):
                        violation = True
                        violation_reason = f"semantic_violation: {insp}"

                if violation:
                    log.warning("[SEMANTIC_GUARD] Reply violation detected (%s). Retrying generation with deliberation note...", violation_reason)
                    delib_correction = f"\n\n[再考注記: 直前の返信候補はユーザーの指摘や前提と矛盾（{violation_reason}）していたため破棄されました。ユーザーの発言を受け止め、否定された前提を繰り返さず、短く簡潔に返信してください。]"
                    retry_prompt = f"{prompt}{delib_correction}"
                    retry_cleaned = self._clean_duplicate_prompt_instructions(retry_prompt, reply_system)
                    retry_reply = await self._call_reply_model_or_fallback(
                        message,
                        retry_cleaned,
                        system_prompt=reply_system,
                        images=runtime.media.images,
                        num_predict=num_predict_val,
                        think_override=think_override,
                    )

                    retry_risk = assess_reply_risk(
                        reply_text=retry_reply,
                        intent_info=intent_info,
                        denied_claims=denied_claims,
                        previous_reply_text=last_assistant_text,
                    )
                    retry_violation = False
                    if retry_risk.get("substring_hit"):
                        retry_violation = True
                    elif retry_risk.get("needs_check"):
                        retry_insp = await inspect_reply_semantics(
                            llm_caller_fn=_inspect_llm_call,
                            user_text=user_text_val,
                            bot_previous_text=last_assistant_text or "",
                            generated_reply=retry_reply,
                            denied_claims=denied_claims,
                        )
                        retry_violation = not retry_insp.get("ok", True)

                    if retry_violation:
                        log.warning("[SEMANTIC_GUARD] Retry reply still violated semantics. Falling back to reaction fallback.")
                        safe_add = getattr(self, "_safe_add_reaction", None)
                        fallback_emoji = "🙏"
                        if callable(safe_add):
                            await safe_add(message, fallback_emoji)
                        elif hasattr(message, "add_reaction"):
                            with contextlib.suppress(Exception):
                                await message.add_reaction(fallback_emoji)
                        reply_content = ""
                    else:
                        reply_content = retry_reply
            except Exception as e:
                log.warning("[SEMANTIC_GUARD] Semantic guard execution failed: %r", e)

        # 固有名詞グラウンディング検査（文脈外の架空人名・捏造比喩の遮断）
        if reply_content and bool(cfg("DISCORD_NAME_GROUNDING_ENABLED", True)):
            try:
                # 会話履歴およびユーザー発言・ボット名から許可語彙集合を構築
                allowed_context_terms: set[str] = set()
                if runtime.original_user_text:
                    for token in re.findall(r"[ァ-ヶーa-zA-Z0-9_\u4e00-\u9fff]{2,}", runtime.original_user_text):
                        allowed_context_terms.add(token)
                if runtime.effective_user_text and runtime.effective_user_text != runtime.original_user_text:
                    for token in re.findall(r"[ァ-ヶーa-zA-Z0-9_\u4e00-\u9fff]{2,}", runtime.effective_user_text):
                        allowed_context_terms.add(token)
                history_list = getattr(runtime, "history", None)
                if history_list:
                    for h in history_list:
                        h_content = str(getattr(h, "content", None) or (h.get("content", "") if isinstance(h, dict) else "") or "")
                        for token in re.findall(r"[ァ-ヶーa-zA-Z0-9_\u4e00-\u9fff]{2,}", h_content):
                            allowed_context_terms.add(token)
                ctx_lines = getattr(runtime, "context_lines", None)
                if ctx_lines:
                    for line in ctx_lines:
                        for token in re.findall(r"[ァ-ヶーa-zA-Z0-9_\u4e00-\u9fff]{2,}", str(line)):
                            allowed_context_terms.add(token)
                bot_name = str(cfg("BOT_NAME", "コイト") or "コイト").strip()
                if bot_name:
                    allowed_context_terms.add(bot_name)

                ungrounded = find_ungrounded_person_names(
                    reply_content,
                    allowed_context_terms,
                    persona_names={bot_name, "私", "僕", "俺", "コイト"},
                )
                if ungrounded:
                    log.warning("[DISCORD_GUARD] 文脈外固有名詞の疑いを検知: names=%s reply=%r", ungrounded, reply_content)
                    names_str = "』『".join(ungrounded)
                    delib_note = (
                        f"\n\n[再考注記: 直前の返信候補には会話の文脈に存在しない人物名『{names_str}』が含まれていました。"
                        "会話履歴に登場した単語だけを使うか、人名を出さずに描写だけで返信してください。「〜の二番煎じ」のような言い回しで新しい人名を作らないでください。]"
                    )
                    retry_prompt = f"{prompt}{delib_note}"
                    retry_cleaned = self._clean_duplicate_prompt_instructions(retry_prompt, reply_system)
                    retry_reply = await self._call_reply_model_or_fallback(
                        message,
                        retry_cleaned,
                        system_prompt=reply_system,
                        images=runtime.media.images,
                        num_predict=num_predict_val,
                        think_override=think_override,
                    )
                    retry_ungrounded = find_ungrounded_person_names(
                        retry_reply,
                        allowed_context_terms,
                        persona_names={bot_name, "私", "僕", "俺", "コイト"},
                    )
                    if not retry_ungrounded:
                        reply_content = retry_reply
                    elif bool(cfg("DISCORD_NAME_GROUNDING_STRIP_FALLBACK", True)):
                        stripped = strip_ungrounded_person_sentences(retry_reply, retry_ungrounded)
                        if stripped:
                            log.info("[DISCORD_GUARD] 捏造名を含む文を除去して採用: stripped=%r", stripped)
                            reply_content = stripped
            except Exception as e:
                log.warning("[DISCORD_GUARD] 固有名詞グラウンディング検査中に例外発生（処理継続）: %r", e)

        # 短文化謝罪（苛立ち・訂正時の泥沼化防止）
        if reply_content:
            try:
                from front.discord.common.frustration import count_anger_markers
                u_text = runtime.effective_user_text or runtime.original_user_text or content(message)
                anger_count = count_anger_markers(u_text)
                is_correction = bool(intent_info and intent_info.get("user_correction"))
                reply_content = self._sanitize_frustrated_apology(reply_content, is_correction=is_correction, anger_count=anger_count)
            except Exception as e:
                log.debug("sanitize_frustrated_apology skipped: %r", e)

        return prompt, reply_content, last_assistant_text, intent_info, handled_message_ids, context_lines




OllamaChatReplyGenerationMixin = OllamaChatReplyCoreMixin

__all__ = [
    "OllamaChatReplyCoreMixin",
    "OllamaChatReplyGenerationMixin",
    "ReplyPromptMixin",
    "ReplyRuntimeMixin",
    "ReplySendingMixin",
    "ReplyEpisodeMixin",
    "ReplyModelCallMixin",
    "ReplyBreakMixin",
    "ReplyTargetDeletedError",
    "_split_reply_text",
    "_OllamaChatReplyCoreBase",
    "cfg",
    "cfg_int",
    "cfg_float",
    "cfg_bool",
    "fetch_recent_context_lines",
    "_find_last_assistant_message_text",
    "fetch_recent_x_timeline_items",
    "research_dispatch",
    "extract_and_store_memory",
    "maybe_store_training_candidate",
    "update_user_habit_profile",
    "update_user_interests",
    "_build_summary_context_lines",
    "maybe_update_channel_summary",
    "report_error",
    "_analyze_pre_reply_context",
    "_build_user_prompt",
    "call_ollama_chat_raw",
    "release_ollama_model",
    "build_different_author_boundary_note",
    "collect_same_author_burst_texts",
    "format_context_lines_with_relation_labels",
    "format_discord_context_line_with_relation",
    "format_message_reactions",
    "looks_like_incomplete_topic_opener",
    "looks_like_meta_ignorance_reply",
    "sanitize_meta_ignorance_reply",
    "log",
]
