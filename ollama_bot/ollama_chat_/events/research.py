"""返信前リサーチの要否判定、検索実行、調査結果へのフィードバック記録を担当するMixin。"""
import asyncio
import contextlib
import logging
import re
import time
from typing import Any, Optional

import discord

from ..ollama_chat_helpers import (
    _cached_other_channel_response_allowed,
    _get_channel_kind,
    _infer_feedback_type,
    _is_force_response_trigger,
    _reply_model_timeout_fallback,
    looks_like_feedback_text,
)


def should_send_timeout_fallback(
    *,
    is_primary_channel: bool = False,
    is_direct: bool = False,
    force_response: bool = False,
) -> bool:
    try:
        import ollama_bot.ollama_chat_.ollama_chat_helpers as helpers
        return helpers.should_send_timeout_fallback(
            is_primary_channel=is_primary_channel,
            is_direct=is_direct,
            force_response=force_response,
        )
    except Exception:
        pass
    return bool(is_primary_channel or is_direct or force_response)
from harness.agent.prompt_parser.prompt_manager.context_helpers import (
    _check_context_flow,
    _find_last_assistant_message_text,
)
from ...common.bot_identity import resolve_bot_identity
from ...common.discord_helpers import author_id, channel_id, content, display_name
from lib.discord_utils import strip_discord_mentions
from harness.agent.prompt_parser.prompt_manager.history_helpers import safe_message_content, find_recent_bot_and_user_pair
from ...common.ollama_helpers import (
    extract_first_user_facing_reply,
    sanitize_generated_reply,
    truncate_text,
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    _normalize_compare_text,
    call_ollama_json,
    release_ollama_model,
    should_skip_unknown_reply,
    looks_like_unknown_or_incomprehension_reply,
    sanitize_unknown_denial_for_known_subject,
)
from harness.agent.tool_registry.umigame_helpers import (
    _build_umigame_clear_append_message,
    _looks_like_umigame_clear,
)
from harness.agent.tool_registry.web_research import (
    build_img_thread_direct_reply,
    build_unknown_topic_fallback_reply,
    check_memory_for_topic,
    clean_img_thread_fragment,
    detect_deepdive_followup,
    detect_reply_research_rules,
    research_dispatch,
)
from ..ollama_chat_texts import build_reply_research_decider_prompt, build_reply_research_decider_system_prompt, build_research_grounded_reply_prompt
from ...common.config_helpers import cfg, cfg_bool, cfg_int, cfg_float, cfg_primary_channel_id, cfg_persona_namespace
from ..ollama_chat_types import MessageRuntime, ReplyModelTimeoutError

log = logging.getLogger("ollama_bot.ollama_chat")

REPLY_RESEARCH_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "needs_research": {"type": "boolean"},
        "mode": {"type": "string"},
        "recency_flag": {"type": "boolean"},
        "unknown_term_flag": {"type": "boolean"},
        "source_request": {"type": "boolean"},
        "reason": {"type": "string"},
        "search_query": {"type": "string"},
        "provisional_reply": {"type": "string"},
    },
    "required": [
        "needs_research",
        "mode",
        "recency_flag",
        "unknown_term_flag",
        "source_request",
        "reason",
        "search_query",
        "provisional_reply",
    ],
    "additionalProperties": False,
}

_TOPIC_OPINION_EVASION_LACK_PATTERN = re.compile(
    r"(?:詳しく|しら|知ら|分から|わから|やったこと)(?:な|無)くて|"
    r"(?:詳しく|しら|知ら|分から|わから|やったこと)(?:な|無)い(?:から|ので|んだ|んです|んす|からさ)?|"
    r"全然(?:詳しく|知ら|分から|わから)|"
    r"さっぱり(?:詳しく|知ら|分から|わから)",
    re.IGNORECASE,
)
_TOPIC_OPINION_EVASION_APOLOGY_PATTERN = re.compile(
    r"(?:ごめん|すみません|すいません|申し訳|許して|"
    r"教えられ|答えられ)(?:ない|へん|ません)?|"
    r"わからない|わかんない|知らな(?:い|ん)|お役に立て",
    re.IGNORECASE,
)


def _looks_like_topic_opinion_evasion_reply(reply_text: str) -> bool:
    text = str(reply_text or "").strip()
    if not text:
        return False
    if not _TOPIC_OPINION_EVASION_LACK_PATTERN.search(text):
        return False
    return bool(_TOPIC_OPINION_EVASION_APOLOGY_PATTERN.search(text))


def _build_topic_opinion_retry_prompt(base_prompt: str) -> str:
    return (
        f"{base_prompt}\n\n"
        "[回答の再生成指示（不知・謝罪逃げの禁止）]\n"
        "直前の回答は『詳しくないからごめんなさい』『わからない』と不知を理由に回答を放棄・謝罪してしまっていました。\n"
        "あなた自身に体験や詳しい知識がなくても全く問題ありません。\n"
        "外部調査結果にある世間の評判やそれぞれの特徴・面白さ（演出の楽しさ、予想やレースの迫力、ゲーム性の違いなど）を踏まえて、"
        "『ネットの評判だと〜らしいよ』『それぞれ〜なところが人気みたい』と客観的な評判を紹介する形で、相手の質問に必ず答えてください。\n"
        "謝罪や『詳しくないからわからない』という回答放棄は絶対に禁止です。\n"
        "設定されたキャラクターの口調を維持しつつ、セリフ本文のみを出力してください。"
    )

from typing import TYPE_CHECKING

from lib.discord_utils import strip_discord_mentions
from ...common.bot_identity import resolve_bot_identity
from harness.agent.prompt_parser.prompt_manager.research_prompts import (
    append_research_citations as _append_research_citations_fn,
    build_research_citation_suffix as _build_research_citation_suffix_fn,
    build_research_context_text,
    build_research_reply_base_prompt,
    build_research_break_base_prompt_text,
    research_citation_sources as _research_citation_sources_fn,
)

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _ResearchEventBase = OllamaChatProtocol
else:
    _ResearchEventBase = object


class _ResearchEventMixin(_ResearchEventBase):
    def _build_research_context_text(self, runtime: MessageRuntime) -> str:
        return build_research_context_text(
            context_lines=runtime.context_lines,
            effective_user_text=runtime.effective_user_text or "",
            original_user_text=runtime.original_user_text or "",
        )

    def _build_research_reply_base_prompt(
        self,
        runtime: MessageRuntime,
        *,
        message: discord.Message | None = None,
        last_assistant_text: str = "",
        intent_info: dict[str, Any] | None = None,
        topic_break: bool = False,
    ) -> str:
        raw_user_text = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()
        user_text_for_prompt = strip_discord_mentions(raw_user_text).strip() or raw_user_text

        bot_ident = resolve_bot_identity(bot=getattr(self, "bot", None), message=message)
        primary_name = bot_ident.primary_name
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        target_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
            or "ユーザー"
        )

        return build_research_reply_base_prompt(
            user_text_for_prompt=user_text_for_prompt,
            target_name=target_name,
            bot_primary_name=primary_name,
            bot_user_id=bot_user_id,
            topic_break=topic_break,
            last_assistant_text=last_assistant_text,
            intent_info=intent_info,
            media_prompt_parts=list(runtime.media.prompt_parts) if runtime.media.prompt_parts else None,
        )

    async def _build_research_break_base_prompt(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        raw_user_text = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()
        user_text_for_prompt = strip_discord_mentions(raw_user_text).strip() or raw_user_text

        bot_ident = resolve_bot_identity(bot=getattr(self, "bot", None), message=message)
        primary_name = bot_ident.primary_name
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        target_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
            or "ユーザー"
        )

        prompt = build_research_break_base_prompt_text(
            user_text_for_prompt=user_text_for_prompt,
            target_name=target_name,
            bot_primary_name=primary_name,
            bot_user_id=bot_user_id,
        )

        core_beliefs: list[str] = []
        get_core_beliefs = getattr(self, "_get_core_beliefs_for_prompt", None)
        if callable(get_core_beliefs):
            with contextlib.suppress(Exception):
                core_beliefs = await get_core_beliefs(message)
        if core_beliefs:
            from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
            target_uid = author_id(message)
            target_aliases: list[str] = []
            if target_uid is not None:
                try:
                    from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
                    target_aliases = await get_user_aliases_from_store(
                        target_uid,
                        guild=getattr(message, "guild", None),
                        memory_store=getattr(self, "memory_store", None),
                    )
                except Exception:
                    pass
            belief_lines = format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=target_name,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
            if belief_lines:
                prompt = "\n".join([prompt, *belief_lines])

        social_guidance = ""
        get_social_guidance = getattr(self, "_get_current_reply_social_guidance", None)
        if callable(get_social_guidance):
            with contextlib.suppress(Exception):
                social_guidance = await get_social_guidance(runtime, incoming_emotion_scores)
        prepend_social_guidance = getattr(self, "_prepend_social_guidance_to_prompt", None)
        if social_guidance and callable(prepend_social_guidance):
            prompt = prepend_social_guidance(prompt, social_guidance)

        intent_info = {
            "intent": "request",
            "target_is_ai": False,
            "should_clarify": False,
            "spontaneous_source": "",
        }
        return prompt, "", intent_info

    def _research_citation_sources(self, research_result: dict[str, Any] | None) -> list[dict[str, Any]]:
        return _research_citation_sources_fn(research_result)

    def _build_research_citation_suffix(self, research_result: dict[str, Any] | None) -> str:
        return _build_research_citation_suffix_fn(research_result)

    def _append_research_citations(self, reply: str, research_result: dict[str, Any] | None) -> str:
        return _append_research_citations_fn(reply, research_result)

    def _disabled_research_decision(self) -> dict[str, Any]:
        return {
            "needs_research": False,
            "mode": "simple_search",
            "query": "",
            "reason": "",
            "provisional_reply": "",
            "recency_flag": False,
            "unknown_term_flag": False,
            "source_request": False,
            "url_present": False,
            "x_timeline_flag": False,
            "x_search_flag": False,
            "conversational_followup": False,
            "rule_reasons": [],
        }

    def _research_decision_timeout_sec(self) -> float:
        return max(cfg_float("REPLY_RESEARCH_DECISION_TIMEOUT_SEC", 6.0), 1.0)

    def _default_research_provisional_text(self, decision: dict[str, Any] | None = None) -> str:
        if not cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False):
            return ""
        payload = dict(decision or {})
        if bool(payload.get("url_present")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_URL_REPLY", "その先を少し見てくる。") or "その先を少し見てくる。"
            ).strip()
        if bool(payload.get("x_timeline_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_X_TIMELINE_REPLY", "今のXを少し見てくる。")
                or "今のXを少し見てくる。"
            ).strip()
        if bool(payload.get("recency_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_RECENCY_REPLY", "今の情報を少し見てくる。")
                or "今の情報を少し見てくる。"
            ).strip()
        if bool(payload.get("unknown_term_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_UNKNOWN_REPLY", "そこは少し確認してくる。")
                or "そこは少し確認してくる。"
            ).strip()
        return str(cfg("REPLY_RESEARCH_PROVISIONAL_REPLY", "少し調べてから返す。") or "少し調べてから返す。").strip()

    def _finalize_research_provisional_text(
        self,
        text: object,
        *,
        user_text: str,
        decision: dict[str, Any] | None = None,
    ) -> str:
        raw_text = str(text or "").strip()
        if raw_text.upper() == "SKIP":
            return "SKIP"

        cleaned = truncate_text(raw_text, max(cfg_int("REPLY_PROVISIONAL_MAX_CHARS", 40), 10))
        if not cleaned:
            return self._default_research_provisional_text(decision)
        if looks_like_abnormal_assistant_reply(cleaned):
            return self._default_research_provisional_text(decision)
        if looks_like_parrot_reply(user_text, cleaned):
            return self._default_research_provisional_text(decision)
        return cleaned

    def _choose_research_provisional_text(
        self,
        *,
        model_result: dict[str, Any],
        base_decision: dict[str, Any],
        user_text: str,
    ) -> str:
        if not cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False):
            return ""
        model_text = str(model_result.get("provisional_reply") or "").strip()
        if model_text.upper() == "SKIP":
            candidate = "SKIP"
        elif cfg_bool("REPLY_RESEARCH_USE_MODEL_PROVISIONAL", False) and model_text:
            candidate = model_text
        else:
            candidate = str(base_decision.get("provisional_reply") or "")
        return self._finalize_research_provisional_text(
            candidate,
            user_text=user_text,
            decision=base_decision,
        )

    def _normalize_research_mode(self, mode: object, *, fallback: str = "simple_search") -> str:
        normalized = str(mode or "").strip().lower()
        if normalized in {"simple_search", "browser_search", "browser_read_url", "x_timeline", "x_search", "img_top5", "img_thread"}:
            return normalized
        return fallback

    def _should_use_research_reply(self, decision: dict[str, Any] | None) -> bool:
        return bool(isinstance(decision, dict) and decision.get("needs_research"))

    def _research_summary_looks_like_login_wall(self, summary: str) -> bool:
        lowered = str(summary or "").lower()
        return any(
            phrase in lowered
            for phrase in (
                "don't miss what's happening",
                "don’t miss what’s happening",
                "people on x are the first to know",
                "something went wrong, but don't fret",
                "something went wrong, but don’t fret",
                "some privacy related extensions may cause issues on x.com",
            )
        )

    def _is_research_result_insufficient(self, research_result: dict[str, Any] | None) -> bool:
        if not isinstance(research_result, dict):
            return True
        if str(research_result.get("error") or "").strip():
            return True
        summary = str(research_result.get("summary") or "").strip()
        if not summary or summary in {"検索結果が見つかりませんでした。", "URLの読み取りに失敗しました。"}:
            return True
        if self._research_summary_looks_like_login_wall(summary):
            return True
        if not list(research_result.get("sources") or []) and summary.startswith("検索がタイムアウトしました。"):
            return True
        return False

    def _build_research_failure_reply(
        self,
        decision: dict[str, Any] | None,
        research_result: dict[str, Any] | None,
    ) -> str:
        payload = dict(decision or {})
        if bool(payload.get("topic_opinion_flag")):
            topic = str(payload.get("target_subject") or payload.get("query") or "").strip()
            persona = cfg_persona_namespace(custom_cfg=cfg)
            user_text = str(payload.get("original_user_text") or "").strip()
            return build_unknown_topic_fallback_reply(
                user_text,
                target_subject=topic,
                persona=persona,
            )

        result = dict(research_result or {})
        reason = str(result.get("error") or "").strip()
        summary = str(result.get("summary") or "").strip()
        if not reason and summary in {"検索結果が見つかりませんでした。", "URLの読み取りに失敗しました。"}:
            reason = summary
        if not reason:
            reason = "十分な情報が取れなかった"

        action = "確認"
        if bool(payload.get("url_present")):
            action = "URL先まで確認"
        elif bool(payload.get("x_search_flag")) or payload.get("mode") == "x_search":
            action = "X（Twitter）の投稿を確認"
        elif bool(payload.get("x_timeline_flag")):
            action = "Xのタイムラインを確認"
        elif bool(payload.get("recency_flag")):
            action = "今の情報を確認"
        elif bool(payload.get("unknown_term_flag")):
            action = "その語を確認"

        reason_for_reply = reason.rstrip("。.!！？")
        template = str(
            cfg(
                "REPLY_RESEARCH_FAILURE_TEMPLATE",
                "{action}したが、十分な情報が取れなかった。理由は {reason}。",
            )
            or "{action}したが、十分な情報が取れなかった。理由は {reason}。"
        )
        try:
            reply = template.format(action=action, reason=reason_for_reply)
        except Exception:
            reply = f"{action}したが、十分な情報が取れなかった。理由は {reason_for_reply}。"
        return truncate_text(reply, 150)

    def _research_reply_num_predict(self) -> int | None:
        base = cfg_int("OLLAMA_REPLY_NUM_PREDICT", 128)
        reply_num_predict = getattr(self, "_reply_num_predict", None)
        if callable(reply_num_predict):
            with contextlib.suppress(Exception):
                resolved = reply_num_predict()
                if resolved is not None:
                    base = int(resolved)
        configured = cfg_int("REPLY_RESEARCH_NUM_PREDICT", 384)
        value = max(base, configured)
        return value if value > 0 else None

    def _is_configured_failure_reply(self, reply: str) -> bool:
        normalized = _normalize_compare_text(reply)
        if not normalized:
            return True
        fallback_values = (
            cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "今のはうまく返せなかった。もう一回言ってくれ。"),
            cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。"),
        )
        return any(normalized == _normalize_compare_text(str(value or "")) for value in fallback_values)

    def _clean_img_thread_fragment(self, text: object, *, max_chars: int = 80) -> str:
        return clean_img_thread_fragment(text, max_chars=max_chars)

    def _build_img_thread_direct_reply(self, research_result: dict[str, Any] | None) -> str:
        return build_img_thread_direct_reply(research_result)

    async def should_research_for_reply(
        self,
        runtime: MessageRuntime,
        *,
        message: discord.Message | None = None,
        force_response: bool = False,
    ) -> dict[str, Any]:
        if not cfg_bool("REPLY_RESEARCH_ENABLED", True):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        if getattr(runtime, "is_umigame_reply", False) or getattr(runtime, "is_twenty_doors_reply", False):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        if (
            message is not None
            and _get_channel_kind(message) == "other"
            and not force_response
            and not _cached_other_channel_response_allowed(message, cog=self)
        ):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        cid = channel_id(message) if message is not None else getattr(runtime, "cid", None)
        channel_cache = getattr(self, "_channel_research_cache", {})
        last_research_cache = channel_cache.get(cid) if isinstance(channel_cache, dict) and cid is not None else None
        deepdive_info = detect_deepdive_followup(runtime.original_user_text, last_research_cache)
        if deepdive_info.get("is_deepdive"):
            deepdive_mode = str(deepdive_info.get("deepdive_mode") or "").strip()
            target_url = str(deepdive_info.get("target_url") or "").strip()
            mode = "img_thread" if deepdive_mode == "img_thread" else "x_timeline"
            decision = {
                "needs_research": True,
                "mode": mode,
                "query": target_url or str((last_research_cache or {}).get("query") or ""),
                "reason": truncate_text(str(deepdive_info.get("reason") or "直前調査の深掘り"), 40),
                "provisional_reply": "",
                "recency_flag": mode == "x_timeline",
                "unknown_term_flag": False,
                "source_request": False,
                "url_present": bool(target_url),
                "x_timeline_flag": mode == "x_timeline",
                "rule_reasons": ["deepdive_followup"],
            }
            runtime.reply_research_decision = decision
            return decision

        context_text = self._build_research_context_text(runtime)
        rule_info = detect_reply_research_rules(runtime.original_user_text, context_text)

        # ステップ0: サーバーメンバー（ユーザー名）の最優先照合（User-First Resolution）
        # 「雑菌についてどう思う？」「ブルードラシャンについては？」等の問いかけにおいて、
        # まずDiscordサーバーに該当するユーザーがいないかを最優先で確認する。
        # ユーザー名と一般名詞・固有名詞が被る場合、サーバーメンバーを優先してWeb検索を遮断する。
        gid = getattr(getattr(message, "guild", None), "id", None)
        memory_store = getattr(self, "memory_store", None)
        subject = str(rule_info.get("target_subject") or rule_info.get("candidate_term") or "").strip()
        user_text_for_search = (runtime.original_user_text or "").strip()
        matched_member = None

        if gid is not None and (subject or bool(rule_info.get("topic_opinion_flag")) or bool(rule_info.get("unknown_term_flag"))):
            try:
                profiles = []
                if memory_store and hasattr(memory_store, "list_user_profiles_for_guild"):
                    profiles = await memory_store.list_user_profiles_for_guild(guild_id=int(gid), limit=1000)
                from harness.agent.prompt_parser.prompt_manager.user_profile_logic import (
                    find_matching_guild_member,
                    merge_guild_and_db_profiles,
                    resolve_mentioned_users,
                )
                profiles = merge_guild_and_db_profiles(profiles, getattr(message, "guild", None))
                if profiles:
                    if subject:
                        matched_member = find_matching_guild_member(subject, profiles)
                    if not matched_member:
                        m_users = resolve_mentioned_users(user_text_for_search, profiles, allow_self=True)
                        if m_users:
                            matched_member = m_users[0]
            except Exception as e:
                log.warning("user-first guild member lookup failed: %r", e)

        if matched_member:
            # サーバーメンバーが存在する場合：Web検索・外部調査を完全にキャンセル
            rule_info["needs_research"] = False
            rule_info["should_consult_model"] = False
            rule_info["topic_opinion_flag"] = False
            rule_info["unknown_term_flag"] = False
            disp_m = str(matched_member.get("current_display_name") or matched_member.get("current_name") or subject)
            runtime.matched_subject_member = matched_member
            member_note = f"ユーザー（{disp_m}）はこのDiscordサーバーの参加者・仲間である。"
            if member_note not in runtime.memory_context_lines:
                runtime.memory_context_lines.append(member_note)
            log.info(
                "User-first priority: '%s' resolved to Discord guild member '%s' (uid=%s). Prioritizing user over web search.",
                subject or user_text_for_search,
                disp_m,
                matched_member.get("user_id"),
            )
        elif bool(rule_info.get("topic_opinion_flag")):
            # サーバーメンバーに該当しない場合のみ、記憶DB（ステップ1）やWeb調査（ステップ2）へ進む
            persona = cfg_persona_namespace(custom_cfg=cfg)
            aspect = str(rule_info.get("target_aspect") or "")
            found_memories = await check_memory_for_topic(
                memory_store,
                persona=persona,
                target_subject=subject,
                target_aspect=aspect,
                guild_id=gid,
            )
            if found_memories:
                rule_info["needs_research"] = False
                rule_info["should_consult_model"] = False
                from ...common.bot_identity import resolve_bot_identity
                from harness.agent.tool_registry.web_research.topic_investigation import is_person_like_memory
                bot_ident = resolve_bot_identity(bot=self.bot, message=message)
                runtime.context_lines.append(f"（{bot_ident.primary_name}の記憶・実感: {' / '.join(found_memories)}）")
                runtime.recalled_topic_memories.extend(found_memories)
                for fm in found_memories:
                    if fm not in runtime.memory_context_lines:
                        runtime.memory_context_lines.append(fm)

                # 対象が記憶の中で人間・サーバーメンバーとして行動している場合、メンバーとしても解決
                if not matched_member and subject and is_person_like_memory(subject, found_memories):
                    matched_member = {
                        "user_id": 0,
                        "current_name": subject,
                        "current_display_name": subject,
                        "aliases": [],
                        "roles": [],
                        "from_memory": True,
                    }
                    runtime.matched_subject_member = matched_member
                    member_note = f"ユーザー（{subject}）はこのDiscordサーバーの参加者・仲間である。"
                    if member_note not in runtime.memory_context_lines:
                        runtime.memory_context_lines.append(member_note)
                    log.info("Topic memory entity resolution: '%s' resolved as guild member/friend from memories.", subject)

                log.info("Topic opinion inquiry: recalled memory for '%s': %r", subject, found_memories)
            else:
                log.info("Topic opinion inquiry: no memory for '%s', proceeding to web research", subject)

        base_decision = {
            "needs_research": bool(rule_info.get("needs_research")),
            "mode": self._normalize_research_mode(rule_info.get("mode"), fallback="simple_search"),
            "query": str(rule_info.get("query") or "").strip(),
            "reason": " / ".join(list(rule_info.get("rule_reasons") or [])[:2]),
            "provisional_reply": "" if bool(rule_info.get("topic_opinion_flag")) else self._default_research_provisional_text(rule_info),
            "recency_flag": bool(rule_info.get("recency_flag")),
            "unknown_term_flag": bool(rule_info.get("unknown_term_flag")),
            "source_request": bool(rule_info.get("source_request")),
            "url_present": bool(rule_info.get("url_present")),
            "x_timeline_flag": bool(rule_info.get("x_timeline_flag")),
            "x_search_flag": bool(rule_info.get("x_search_flag")),
            "conversational_followup": bool(rule_info.get("conversational_followup")),
            "rule_reasons": list(rule_info.get("rule_reasons") or []),
            "topic_opinion_flag": bool(rule_info.get("topic_opinion_flag")),
            "target_subject": str(rule_info.get("target_subject") or ""),
            "target_aspect": str(rule_info.get("target_aspect") or ""),
            "original_user_text": runtime.original_user_text,
        }

        model_result: dict[str, Any] = {}
        if bool(rule_info.get("should_consult_model")):
            model_fallback = cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))
            decision_model = str(cfg("REPLY_RESEARCH_DECISION_MODEL", model_fallback) or "")
            try:
                if decision_model:
                    runtime.utility_models_used.add(decision_model)
                model_result = await call_ollama_json(
                    build_reply_research_decider_prompt(
                        original_user_text=runtime.original_user_text,
                        effective_user_text=runtime.effective_user_text,
                        context_lines=runtime.context_lines,
                        rule_info=rule_info,
                    ),
                    system_prompt=build_reply_research_decider_system_prompt(),
                    schema=REPLY_RESEARCH_DECISION_SCHEMA,
                    think=False,
                    model=decision_model,
                    timeout_sec=self._research_decision_timeout_sec(),
                    retries=0,
                    temperature=0.1,
                    num_predict=max(cfg_int("REPLY_RESEARCH_DECISION_NUM_PREDICT", 192), 96),
                )
            except Exception as e:
                log.warning("reply research decision failed; using rules only: %r", e)

        user_text = str(runtime.original_user_text or "").strip()
        howto_keywords = (
            "方法", "やり方", "やりかた", "仕方", "しかた", "手順",
            "使い方", "つかいかた", "ダウンロード", "download", "保存",
            "ログイン", "サインイン", "login", "signin",
            "料金", "仕様", "仕組み", "しくみ",
        )
        is_howto_intent = any(kw in user_text.lower() for kw in howto_keywords)

        model_mode = self._normalize_research_mode(
            model_result.get("mode"),
            fallback="",
        )
        model_selected_web = model_mode in {"browser_search", "simple_search"}

        final_mode = self._normalize_research_mode(
            model_result.get("mode"),
            fallback=str(base_decision.get("mode") or "auto"),
        )
        if str(base_decision.get("mode") or "") == "img_thread":
            # img_thread はURL一般処理より優先する。汎用ブラウザ読みだと本文が崩れやすい。
            final_mode = "img_thread"
        elif str(base_decision.get("mode") or "") == "img_top5":
            # img_top5 はルール側で確定したモード。LLMの上書きを禁止する
            final_mode = "img_top5"
        elif base_decision["url_present"]:
            final_mode = "browser_read_url"
        elif model_selected_web:
            # モデルがWeb検索（browser_search等）を選択した場合は、ルールのx_search_flagによる上書きを防ぎモデルの推論を最優先
            final_mode = model_mode
        elif base_decision.get("topic_opinion_flag"):
            # 作品・トピックの評価・比較調査は上位記事の本文を読解して世間の評判を把握する
            final_mode = "browser_search"
        elif is_howto_intent:
            # How-to（やり方・操作手順・保存等）の質問は、SNSポスト検索ではなくWeb解説検索を選択
            final_mode = model_mode if model_mode and model_mode != "x_search" else "browser_search"
        elif base_decision.get("x_search_flag") or str(base_decision.get("mode") or "") == "x_search":
            final_mode = "x_search"
        elif base_decision["x_timeline_flag"]:
            final_mode = "x_timeline"

        final_query = str(model_result.get("search_query") or base_decision["query"] or "").strip()
        if base_decision["url_present"] or base_decision["x_timeline_flag"] or final_mode in {"img_top5", "img_thread"}:
            # URL/タイムライン/IMG は query を固定URLにする
            final_query = str(base_decision["query"] or final_query or "").strip()

        final_decision = {
            **base_decision,
            "needs_research": bool(base_decision["needs_research"] or model_result.get("needs_research")),
            "mode": final_mode,
            "query": final_query,
            "reason": truncate_text(str(model_result.get("reason") or base_decision["reason"] or "").strip(), 40),
            "provisional_reply": self._choose_research_provisional_text(
                model_result=model_result,
                base_decision=base_decision,
                user_text=runtime.original_user_text,
            ),
            "recency_flag": bool(base_decision["recency_flag"] or model_result.get("recency_flag")),
            "unknown_term_flag": bool(base_decision["unknown_term_flag"] or model_result.get("unknown_term_flag")),
            "source_request": bool(base_decision["source_request"] or model_result.get("source_request")),
            "x_timeline_flag": bool(base_decision["x_timeline_flag"]),
            "x_search_flag": bool(base_decision.get("x_search_flag")),
        }
        if final_decision["provisional_reply"] == "SKIP":
            final_decision["provisional_reply"] = ""
        elif not final_decision["provisional_reply"] and final_decision["needs_research"]:
            final_decision["provisional_reply"] = self._default_research_provisional_text(final_decision)

        runtime.reply_research_decision = final_decision
        return final_decision

    async def _generate_research_backed_reply(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        base_prompt: str,
        last_assistant_text: str,
        intent_info: dict[str, Any],
        research_decision: dict[str, Any],
        research_result: dict[str, Any],
    ) -> str:
        if self._is_research_result_insufficient(research_result):
            return self._build_research_failure_reply(research_decision, research_result)

        research_mode = self._normalize_research_mode(
            research_result.get("mode") or research_decision.get("mode"),
            fallback="simple_search",
        )

        research_base_prompt = self._build_research_reply_base_prompt(
            runtime,
            message=message,
            last_assistant_text=last_assistant_text,
            intent_info=intent_info,
            topic_break="直前の流れとは別話題" in str(base_prompt or ""),
        )
        prompt = build_research_grounded_reply_prompt(
            base_prompt=research_base_prompt or base_prompt,
            research_summary=str(research_result.get("summary") or ""),
            research_query=str(research_result.get("query") or research_decision.get("query") or ""),
            research_mode=research_mode,
            rule_reason=str(research_decision.get("reason") or ""),
            recency_flag=bool(research_decision.get("recency_flag")),
            source_request=bool(research_decision.get("source_request")),
            unknown_term_flag=bool(research_decision.get("unknown_term_flag")),
            used_browser=bool(research_result.get("used_browser")),
            confidence=float(research_result.get("confidence") or 0.0),
            sources=list(research_result.get("sources") or []),
        )
        try:
            call_fn = getattr(self, "_call_reply_model_or_fallback", None)
            if callable(call_fn):
                reply = await call_fn(
                    message,
                    prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
            else:
                reply = await self._call_with_typing(
                    message,
                    prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
        except Exception:
            if research_mode == "img_thread":
                return self._build_img_thread_direct_reply(research_result)
            raise
        reply = await self._apply_retry_guards(
            message,
            prompt=prompt,
            reply=reply,
            last_assistant_text=last_assistant_text,
            intent_info=intent_info,
            runtime=runtime,
        )
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        replacement_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
        ).strip()
        raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
        other_names_set = {raw_author_name} if raw_author_name and raw_author_name != replacement_name else None
        user_text_val = str(runtime.original_user_text or "").strip()
        cleaned = sanitize_generated_reply(
            reply,
            bot_user_id=bot_user_id,
            replacement_name=replacement_name,
            target_name=replacement_name,
            other_names=other_names_set,
            user_text=user_text_val,
        )

        is_topic_opinion = bool(research_decision.get("topic_opinion_flag")) or "topic_opinion_inquiry" in str(
            research_decision.get("reason") or ""
        )
        if is_topic_opinion and _looks_like_topic_opinion_evasion_reply(cleaned):
            log.warning("topic opinion inquiry evasion reply detected: %r; retrying with explicit guidance", cleaned[:60])
            retry_prompt = _build_topic_opinion_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
                if retried:
                    retried_cleaned = sanitize_generated_reply(
                        retried,
                        bot_user_id=bot_user_id,
                        replacement_name=replacement_name,
                        user_text=user_text_val,
                    )
                    if retried_cleaned and not _looks_like_topic_opinion_evasion_reply(retried_cleaned):
                        cleaned = retried_cleaned
                    elif retried_cleaned:
                        cleaned = retried_cleaned
            except Exception as e:
                log.warning("topic opinion evasion retry failed: %r", e)

        if (
            research_mode == "img_thread"
            and (not cleaned or self._is_configured_failure_reply(cleaned) or looks_like_abnormal_assistant_reply(cleaned))
        ):
            return self._build_img_thread_direct_reply(research_result)
        if cleaned:
            return self._append_research_citations(cleaned, research_result)
        return self._build_research_failure_reply(research_decision, research_result)

    def _store_channel_research_cache(self, cid: int | None, result: dict[str, Any]) -> None:
        if cid is None or not result:
            return
        mode = str(result.get("mode") or "").strip()
        if mode not in {"img_top5", "x_timeline"}:
            return

        if not isinstance(getattr(self, "_channel_research_cache", None), dict):
            self._channel_research_cache = {}
            
        import time as _time
        self._channel_research_cache[cid] = {
            "mode": mode,
            "summary": str(result.get("summary") or ""),
            "sources": list(result.get("sources") or []),
            "threads": list(result.get("threads") or []),
            "query": str(result.get("query") or ""),
            "timestamp": _time.monotonic(),
            "channel_id": cid,
        }

    async def _resolve_research_with_deepdive(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        research_decision: dict[str, Any],
        research_context: str,
    ) -> tuple[dict[str, Any], bool]:
        cid = channel_id(message)
        cache = self._channel_research_cache.get(cid) if cid is not None else None
        
        deepdive_info = detect_deepdive_followup(runtime.original_user_text, cache)
        
        if deepdive_info.get("is_deepdive"):
            deepdive_mode = deepdive_info.get("deepdive_mode")
            if deepdive_mode == "img_thread":
                target_url = deepdive_info.get("target_url")
                if not target_url:
                    target_url = "https://img.2chan.net/b/futaba.php?mode=cat&sort=6"
                try:
                    result = await research_dispatch(target_url, context=research_context, mode="img_thread")
                    return result, True
                except Exception as e:
                    return {"error": str(e)}, True
            elif deepdive_mode == "x_deepdive" and cache:
                fake_result = {
                    "mode": "x_timeline",
                    "summary": cache.get("summary", ""),
                    "sources": cache.get("sources", []),
                    "used_web": True,
                    "used_browser": False,
                }
                return fake_result, True
        
        # 通常の調査フロー
        research_input = (
            runtime.original_user_text
            if self._normalize_research_mode(research_decision.get("mode")) in {"browser_read_url", "x_timeline", "img_thread"}
            else str(research_decision.get("query") or runtime.effective_user_text or runtime.original_user_text)
            if self._normalize_research_mode(research_decision.get("mode")) != "img_top5"
            else str(research_decision.get("query") or "https://img.2chan.net/b/futaba.php?mode=cat&sort=6")
        )
        try:
            result = await research_dispatch(
                research_input,
                context=research_context,
                mode=self._normalize_research_mode(research_decision.get("mode"), fallback="simple_search"),  # type: ignore[arg-type]
                max_results=4,
            )
            return result, False
        except Exception as e:
            log.warning("reply research dispatch failed: %r", e)
            return {
                "used_web": False,
                "used_browser": False,
                "summary": "",
                "sources": [],
                "confidence": 0.0,
                "mode": self._normalize_research_mode(research_decision.get("mode"), fallback="simple_search"),
                "query": str(research_decision.get("query") or research_input or "").strip(),
                "error": f"調査中にエラーが発生した: {e}",
            }, False

    async def _send_timeout_fallback_reply(
        self,
        message: discord.Message,
        resolved_runtime: MessageRuntime,
        *,
        bot_user_id: int | None = None,
        provisional_message: discord.Message | None = None,
    ) -> discord.Message | None:
        fallback_text = _reply_model_timeout_fallback()
        _eff_target = (
            resolved_runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or resolved_runtime.author_name
            or "ユーザー"
        ).strip()
        _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
        _eff_other_names = {
            line.split(":", 1)[0].strip()
            for line in (resolved_runtime.context_lines or [])
            if ":" in line and line.split(":", 1)[0].strip() != _eff_target
        }
        if _raw_author_name and _raw_author_name != _eff_target:
            _eff_other_names.add(_raw_author_name)
        reply = sanitize_generated_reply(
            fallback_text,
            bot_user_id=bot_user_id,
            replacement_name=_eff_target,
            target_name=_eff_target,
            other_names=_eff_other_names,
        )
        sent: discord.Message | None = None
        if provisional_message is not None:
            try:
                sent = await self._send_followup_reply(provisional_message, reply)
            except Exception as e:
                log.warning("followup timeout fallback reply failed; falling back to single final reply: %r", e)
                with contextlib.suppress(Exception):
                    await provisional_message.delete()
                sent = await self._send_reply(message, reply)
        else:
            sent = await self._send_reply(message, reply)
        if sent is not None:
            self._append_sent_message(sent)
        return sent

    async def _send_contextual_reaction(
        self,
        message: discord.Message,
        runtime: MessageRuntime | None = None,
        incoming_emotion_scores: dict[str, float] | None = None,
        *,
        force_response: bool = False,
    ) -> None:
        resolved_runtime = runtime or await self._get_or_build_runtime(message)
        if resolved_runtime is None:
            return

        cid = channel_id(message)
        primary_cid = cfg_primary_channel_id(0)
        is_primary = primary_cid > 0 and (cid == primary_cid or _get_channel_kind(message) == "primary")

        bot_user_id = resolved_runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        is_direct = bool(force_response)
        if not is_direct and bot_user_id is not None:
            with contextlib.suppress(Exception):
                is_direct = await _is_force_response_trigger(message, bot_user_id)

        umigame_running = cid is not None and cid in self.umigame_states

        if getattr(resolved_runtime, "is_twenty_doors_reply", False):
            handled = await self._handle_active_20doors_message(message, resolved_runtime)
            if handled:
                return

        if umigame_running:
            if not getattr(resolved_runtime, "is_umigame_reply", False):
                return
            handled = await self._handle_active_umigame_message(message, resolved_runtime)
            if handled:
                return
        else:
            if await self._handle_time(message, resolved_runtime):
                return
            if await self._handle_weather(message, resolved_runtime):
                return

        pre_last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=resolved_runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        research_decision = dict(getattr(resolved_runtime, "reply_research_decision", {}) or {})
        needs_research = self._should_use_research_reply(research_decision)
        flow = await _check_context_flow(
            message,
            resolved_runtime.context_lines,
            latest_user_text=resolved_runtime.effective_user_text,
            last_assistant_text=pre_last_assistant_text,
        )

        # このターンで実際に触った utility tier だけを解放し、main tier のVRAMを空ける。
        _models_to_release = {str(m).strip() for m in getattr(resolved_runtime, "utility_models_used", set()) if str(m).strip()}
        if cfg_bool("OLLAMA_RELEASE_CONFIGURED_UTILITY_MODELS_BEFORE_REPLY", False):
            _utility_model = str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
            _classifier_model = str(cfg("OLLAMA_CLASSIFIER_MODEL", "") or "").strip()
            _models_to_release.update(m for m in (_utility_model, _classifier_model) if m)
        for _m in _models_to_release:
            await release_ollama_model(
                _m,
                timeout_sec=max(cfg_float("OLLAMA_UTILITY_RELEASE_TIMEOUT_SEC", 2.0), 0.2),
            )
        del _models_to_release

        try:
            if flow == "BREAK":
                log.info("Topic break detected: sending off-topic/unclear message to LLM.")
                if needs_research:
                    prompt, last_assistant_text, intent_info = await self._build_research_break_base_prompt(
                        message,
                        resolved_runtime,
                        incoming_emotion_scores=incoming_emotion_scores,
                    )
                    last_assistant_text = pre_last_assistant_text or last_assistant_text
                    reply = ""
                else:
                    prompt, reply, last_assistant_text, intent_info = await self._generate_break_reply(
                        message,
                        resolved_runtime,
                        incoming_emotion_scores=incoming_emotion_scores,
                        skip_model=False,
                        allow_unclear_intent_fallback=True,
                    )
                if pre_last_assistant_text and not last_assistant_text:
                    last_assistant_text = pre_last_assistant_text
                handled_message_ids: list[int] = []
                context_lines = resolved_runtime.context_lines
                spontaneous_source = str(intent_info.get("spontaneous_source") or "").strip()
            else:
                prompt, reply, last_assistant_text, intent_info, handled_message_ids, context_lines = await self._generate_normal_reply(
                    message,
                    resolved_runtime,
                    incoming_emotion_scores=incoming_emotion_scores,
                    skip_model=needs_research,
                    allow_unclear_intent_fallback=not needs_research,
                )
                spontaneous_source = ""
        except ReplyModelTimeoutError as e:
            if not should_send_timeout_fallback(
                is_primary_channel=is_primary,
                is_direct=is_direct,
            ):
                log.warning(
                    "Reply skipped because model call timed out after retries channel=%s message=%s: %r",
                    cid,
                    getattr(message, "id", None),
                    e,
                )
                return
            log.warning(
                "Model call timed out after retries; sending timeout fallback reply channel=%s message=%s is_primary=%s is_direct=%s: %r",
                cid,
                getattr(message, "id", None),
                is_primary,
                is_direct,
                e,
            )
            self._append_current_user_message(resolved_runtime, message)
            await self._send_timeout_fallback_reply(
                message,
                resolved_runtime,
                bot_user_id=bot_user_id,
            )
            return

        self._append_current_user_message(resolved_runtime, message)

        if needs_research:
            provisional_message: discord.Message | None = None
            provisional_text = (
                str(research_decision.get("provisional_reply", "")).strip()
                if cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False)
                else ""
            )
            if provisional_text:
                try:
                    provisional_message = await self._send_provisional_reply(message, provisional_text)
                except Exception as e:
                    log.warning("provisional reply failed; falling back to single final reply: %r", e)

            research_context = self._build_research_context_text(resolved_runtime)
            research_result, is_deepdive = await self._resolve_research_with_deepdive(
                message, resolved_runtime, research_decision, research_context
            )
            if cid is not None:
                self._store_channel_research_cache(cid, research_result)

            try:
                reply = await self._generate_research_backed_reply(
                    message,
                    resolved_runtime,
                    base_prompt=prompt,
                    last_assistant_text=last_assistant_text,
                    intent_info=intent_info,
                    research_decision=research_decision,
                    research_result=research_result,
                )
            except ReplyModelTimeoutError as e:
                if not should_send_timeout_fallback(
                    is_primary_channel=is_primary,
                    is_direct=is_direct,
                ):
                    log.warning(
                        "Research reply skipped because model call timed out after retries channel=%s message=%s: %r",
                        cid,
                        getattr(message, "id", None),
                        e,
                    )
                    if provisional_message is not None:
                        with contextlib.suppress(Exception):
                            await provisional_message.delete()
                    return
                log.warning(
                    "Research model call timed out after retries; sending timeout fallback reply channel=%s message=%s is_primary=%s is_direct=%s: %r",
                    cid,
                    getattr(message, "id", None),
                    is_primary,
                    is_direct,
                    e,
                )
                await self._send_timeout_fallback_reply(
                    message,
                    resolved_runtime,
                    bot_user_id=bot_user_id,
                    provisional_message=provisional_message,
                )
                return
            _eff_target = (
                resolved_runtime.author_display_name
                or (display_name(message) if message is not None else "")
                or resolved_runtime.author_name
                or "ユーザー"
            ).strip()
            _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
            _eff_other_names = {
                line.split(":", 1)[0].strip()
                for line in (resolved_runtime.context_lines or [])
                if ":" in line and line.split(":", 1)[0].strip() != _eff_target
            }
            if _raw_author_name and _raw_author_name != _eff_target:
                _eff_other_names.add(_raw_author_name)
            reply = sanitize_generated_reply(
                reply,
                bot_user_id=bot_user_id,
                replacement_name=_eff_target,
                target_name=_eff_target,
                other_names=_eff_other_names,
            )

            if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                puzzle = self.umigame_states.pop(cid, None)
                if puzzle:
                    reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

            if should_skip_unknown_reply(
                reply,
                user_text=resolved_runtime.original_user_text,
                is_primary_channel=is_primary,
                is_direct=is_direct,
            ):
                log.info(
                    "Research reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                    reply,
                    getattr(message, "id", None),
                )
                if provisional_message is not None:
                    with contextlib.suppress(Exception):
                        await provisional_message.delete()
                return
            elif looks_like_unknown_or_incomprehension_reply(reply, user_text=resolved_runtime.original_user_text):
                if resolved_runtime.matched_subject_member or resolved_runtime.recalled_topic_memories:
                    cleaned_r = sanitize_unknown_denial_for_known_subject(reply)
                    if cleaned_r != reply:
                        reply = cleaned_r
                        log.info("Sanitized unknown denial in research reply for known topic/member: %r", reply)
                log.info(
                    "Research reply retained despite unknown expression due to primary/direct exception (is_primary=%s, is_direct=%s): %r (message_id=%s)",
                    is_primary,
                    is_direct,
                    reply,
                    getattr(message, "id", None),
                )

            if provisional_message is not None:
                try:
                    sent = await self._send_followup_reply(provisional_message, reply)
                except Exception as e:
                    log.warning("followup reply failed; falling back to single final reply: %r", e)
                    sent = await self._send_reply(message, reply)
            else:
                sent = await self._send_reply(message, reply)
            self._append_sent_message(sent)
            _bg_task = asyncio.create_task(self._store_and_update_summary(
                message,
                sent,
                runtime=resolved_runtime,
                context_lines=context_lines,
                assistant_text=reply,
                incoming_emotion_scores=incoming_emotion_scores,
                spontaneous_source=spontaneous_source,
                research_result=research_result,
                episode_context=intent_info,
            ))
            _bg_task.add_done_callback(
                lambda t: log.warning("_store_and_update_summary failed in background: %r", t.exception()) if t.exception() else None
            )
        else:
            reply = await self._apply_retry_guards(
                message,
                prompt=prompt,
                reply=reply,
                last_assistant_text=last_assistant_text,
                intent_info=intent_info,
                runtime=resolved_runtime,
            )
            _eff_target = (
                resolved_runtime.author_display_name
                or (display_name(message) if message is not None else "")
                or resolved_runtime.author_name
                or "ユーザー"
            ).strip()
            _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
            _eff_other_names = {
                line.split(":", 1)[0].strip()
                for line in (resolved_runtime.context_lines or [])
                if ":" in line and line.split(":", 1)[0].strip() != _eff_target
            }
            if _raw_author_name and _raw_author_name != _eff_target:
                _eff_other_names.add(_raw_author_name)
            reply = sanitize_generated_reply(
                reply,
                bot_user_id=bot_user_id,
                replacement_name=_eff_target,
                target_name=_eff_target,
                other_names=_eff_other_names,
            )

            if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                puzzle = self.umigame_states.pop(cid, None)
                if puzzle:
                    reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

            if should_skip_unknown_reply(
                reply,
                user_text=resolved_runtime.original_user_text,
                is_primary_channel=is_primary,
                is_direct=is_direct,
            ):
                log.info(
                    "Reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                    reply,
                    getattr(message, "id", None),
                )
                return
            elif looks_like_unknown_or_incomprehension_reply(reply, user_text=resolved_runtime.original_user_text):
                if resolved_runtime.matched_subject_member or resolved_runtime.recalled_topic_memories:
                    cleaned_r = sanitize_unknown_denial_for_known_subject(reply)
                    if cleaned_r != reply:
                        reply = cleaned_r
                        log.info("Sanitized unknown denial in reply for known topic/member: %r", reply)
                log.info(
                    "Reply retained despite unknown expression due to primary/direct exception (is_primary=%s, is_direct=%s): %r (message_id=%s)",
                    is_primary,
                    is_direct,
                    reply,
                    getattr(message, "id", None),
                )

            sent = await self._send_reply(message, reply)
            self._append_sent_message(sent)
            salvaged_res: dict[str, Any] | None = getattr(resolved_runtime, "salvaged_research_result", None)
            if not salvaged_res and isinstance(intent_info, dict):
                salvaged_res = intent_info.get("salvaged_research_result")

            _bg_task = asyncio.create_task(self._store_and_update_summary(
                message,
                sent,
                runtime=resolved_runtime,
                context_lines=context_lines,
                assistant_text=reply,
                incoming_emotion_scores=incoming_emotion_scores,
                spontaneous_source=spontaneous_source,
                research_result=salvaged_res,
                episode_context=intent_info,
            ))
            _bg_task.add_done_callback(
                lambda t: log.warning("_store_and_update_summary failed in background: %r", t.exception()) if t.exception() else None
            )




