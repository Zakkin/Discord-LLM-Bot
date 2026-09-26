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
    looks_like_feedback_text,
)
from ...common.context_helpers import (
    _check_context_flow,
    _find_last_assistant_message_text,
)
from ...common.discord_helpers import author_id, channel_id, content
from ...common.history_helpers import safe_message_content, find_recent_bot_and_user_pair
from ...common.ollama_helpers import extract_first_user_facing_reply, sanitize_generated_reply, truncate_text, looks_like_abnormal_assistant_reply, looks_like_parrot_reply, _normalize_compare_text, call_ollama_json, release_ollama_model, should_skip_unknown_reply
from ...common.umigame_helpers import (
    _build_umigame_clear_append_message,
    _looks_like_umigame_clear,
)
from ...common.web_research import (
    build_img_thread_direct_reply,
    build_unknown_topic_fallback_reply,
    check_memory_for_topic,
    clean_img_thread_fragment,
    detect_deepdive_followup,
    detect_reply_research_rules,
    research_dispatch,
)
from ..ollama_chat_texts import build_reply_research_decider_prompt, build_reply_research_decider_system_prompt, build_research_grounded_reply_prompt
from ...common.config_helpers import cfg, cfg_bool, cfg_int, cfg_float
from ..ollama_chat_types import MessageRuntime

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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _ResearchEventBase = OllamaChatProtocol
else:
    _ResearchEventBase = object


class _ResearchEventMixin(_ResearchEventBase):
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
        if normalized in {"simple_search", "browser_search", "browser_read_url", "x_timeline", "img_top5", "img_thread"}:
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
            persona = str(cfg("MEMORY_PERSONA_NAMESPACE", ""))
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

    def _build_research_context_text(self, runtime: MessageRuntime) -> str:
        parts: list[str] = []
        if runtime.context_lines:
            parts.append("\n".join(runtime.context_lines[-4:]))
        if runtime.effective_user_text and runtime.effective_user_text != runtime.original_user_text:
            parts.append(runtime.effective_user_text)
        return "\n".join(part for part in parts if part).strip()

    def _build_research_reply_base_prompt(
        self,
        runtime: MessageRuntime,
        *,
        last_assistant_text: str = "",
        intent_info: dict[str, Any] | None = None,
        topic_break: bool = False,
    ) -> str:
        payload = dict(intent_info or {})
        user_text_for_prompt = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()

        lead = "以下のメッセージは、直前の流れとは別話題です。" if topic_break else "以下のメッセージに、外部調査結果を踏まえて直接答えてください。"
        ref_match = re.match(r"^（([^（）\n]{1,120})への返信）[\s\n]*", user_text_for_prompt)
        parts = [lead]
        if ref_match:
            ref_info = ref_match.group(1).strip()
            clean_user_text = user_text_for_prompt[ref_match.end():].strip() or user_text_for_prompt
            parts.append(f"【返信元の相手発言】: {ref_info}")
            parts.append(f"対象メッセージ: {clean_user_text}")
            parts.append("※返信元の相手発言の話題に引きずられず、対象メッセージ（最新のユーザー発言）に直接答えてください。")
        else:
            parts.append(f"対象メッセージ: {user_text_for_prompt}")
        parts.extend([
            "",
            *(["会話の続きに無理やり合わせなくてよいです。"] if topic_break else []),
            "今回の返答は、対象メッセージと外部調査結果を最優先してください。",
            "会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題へ戻らないでください。",
            "対象メッセージや外部調査結果に出てこない固有名詞・人物・作品名・ミーム・例えを勝手に持ち込まないでください。",
            "外部調査結果に含まれない断定は禁止です。",
        ])

        if last_assistant_text and any(ch in str(last_assistant_text or "") for ch in ("?", "？")):
            parts += [
                "",
                "直前のAI発言が質問だった場合だけ、その質問への返答として自然につながるように答えてください。",
                "ただし話題の中心は必ず対象メッセージと外部調査結果に置いてください。",
            ]

        if bool(payload.get("target_is_ai")) and str(payload.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}:
            parts += [
                "",
                "最新のユーザー発言はAIへの依頼または要求である可能性があります。",
                "依頼された内容を、AI自身が引き受けるか断るかの形で自然に返答してください。",
            ]
        if bool(payload.get("should_clarify")):
            parts += [
                "",
                "意味が曖昧なら、決めつけずに短く確認してください。",
            ]
        if runtime.media.prompt_parts:
            parts += list(runtime.media.prompt_parts)

        return "\n".join(parts).strip()

    async def _build_research_break_base_prompt(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:

        user_text_for_prompt = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()

        prompt = "\n".join([
            "以下のメッセージは、直前の流れとは別話題です。",
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "会話の続きに無理やり合わせなくてよいです。",
            "単独で意味が通る質問・依頼なら、その内容に直接答えてください。",
            "直前のAI発言、最近の会話要約、長期記憶を引きずらず、このメッセージ自体に答えてください。",
            "※ただし、対象メッセージや調査結果に以前の会話や記憶にある用語・キャラクターが含まれる場合は、知っている話題として自然に関連付けて言及してください。",
            "このあと外部調査結果が追加される場合は、その調査結果と対象メッセージを最優先してください。",
        ])

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
        if not isinstance(research_result, dict):
            return []

        limit = max(min(cfg_int("REPLY_RESEARCH_MAX_CITATIONS", 3), 4), 1)
        seen_urls: set[str] = set()
        citations: list[dict[str, Any]] = []
        for source in list(research_result.get("sources") or []):
            if not isinstance(source, dict):
                continue
            if str(source.get("source_type") or "").strip() != "browser":
                continue
            if not bool(source.get("used_in_answer")):
                continue
            url = str(source.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            citations.append(source)
            if len(citations) >= limit:
                break
        return citations

    def _build_research_citation_suffix(self, research_result: dict[str, Any] | None) -> str:
        # Web/MCP research sources are used only to ground the answer.
        # We intentionally do not render source lists in Discord replies.
        return ""

    def _append_research_citations(self, reply: str, research_result: dict[str, Any] | None) -> str:
        base_reply = str(reply or "").strip()
        if not base_reply:
            return base_reply

        suffix = self._build_research_citation_suffix(research_result)
        if not suffix:
            return base_reply

        max_chars = 1900
        joined = f"{base_reply}\n\n{suffix}"
        if len(joined) <= max_chars:
            return joined

        reserved = len(suffix) + 2
        trimmed = truncate_text(base_reply, max(max_chars - reserved, 80))
        return f"{trimmed}\n\n{suffix}"

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

        # ステップ1: トピック評価・感想・疑問の問い合わせであれば、まず記憶DBから思い出しを試みる
        if bool(rule_info.get("topic_opinion_flag")):
            memory_store = getattr(self, "memory_store", None)
            persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "koito"))
            subject = str(rule_info.get("target_subject") or "")
            aspect = str(rule_info.get("target_aspect") or "")
            gid = getattr(getattr(message, "guild", None), "id", None)
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
                bot_ident = resolve_bot_identity(bot=self.bot, message=message)
                runtime.context_lines.append(f"（{bot_ident.primary_name}の記憶・実感: {' / '.join(found_memories)}）")
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
        cleaned = sanitize_generated_reply(reply)
        if (
            research_mode == "img_thread"
            and (not cleaned or self._is_configured_failure_reply(cleaned) or looks_like_abnormal_assistant_reply(cleaned))
        ):
            return self._build_img_thread_direct_reply(research_result)
        if cleaned:
            return self._append_research_citations(cleaned, research_result)
        return self._build_research_failure_reply(research_decision, research_result)

    def _emotion_prereply_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PREREPLY_TIMEOUT_SEC", 2.0), 0.5)

    def _emotion_presence_sync_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PRESENCE_SYNC_TIMEOUT_SEC", 15.0), 5.0)

    def _emotion_update_timeout_sec(self, *, needs_scoring: bool) -> float:
        configured = cfg_float("EMOTION_UPDATE_TIMEOUT_SEC", 0.0)
        if configured > 0.0:
            return configured
        reason_timeout = max(cfg_float("EMOTION_REASON_TIMEOUT_SEC", 20.0), 5.0)
        if needs_scoring:
            score_timeout = max(
                cfg_float("EMOTION_SCORE_TIMEOUT_SEC", cfg_float("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0)),
                10.0,
            )
            return score_timeout + reason_timeout + 5.0
        return reason_timeout + 5.0


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

    async def _send_contextual_reaction(
        self,
        message: discord.Message,
        runtime: MessageRuntime | None = None,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> None:
        resolved_runtime = runtime or await self._get_or_build_runtime(message)
        if resolved_runtime is None:
            return

        cid = channel_id(message)
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

            reply = await self._generate_research_backed_reply(
                message,
                resolved_runtime,
                base_prompt=prompt,
                last_assistant_text=last_assistant_text,
                intent_info=intent_info,
                research_decision=research_decision,
                research_result=research_result,
            )
            reply = sanitize_generated_reply(reply)

            if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                puzzle = self.umigame_states.pop(cid, None)
                if puzzle:
                    reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

            if should_skip_unknown_reply(reply, user_text=resolved_runtime.original_user_text):
                log.info(
                    "Research reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                    reply,
                    getattr(message, "id", None),
                )
                if provisional_message is not None:
                    with contextlib.suppress(Exception):
                        await provisional_message.delete()
                return

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
            reply = sanitize_generated_reply(reply)

            if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                puzzle = self.umigame_states.pop(cid, None)
                if puzzle:
                    reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

            if should_skip_unknown_reply(reply, user_text=resolved_runtime.original_user_text):
                log.info(
                    "Reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                    reply,
                    getattr(message, "id", None),
                )
                return

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
                episode_context=intent_info,
            ))
            _bg_task.add_done_callback(
                lambda t: log.warning("_store_and_update_summary failed in background: %r", t.exception()) if t.exception() else None
            )



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

    async def _maybe_capture_feedback(self, message: discord.Message, *, bot_user_id: Optional[int]) -> None:
        if bot_user_id is None or getattr(message.author, "bot", False):
            return
        feedback_text = content(message)
        if not looks_like_feedback_text(feedback_text):
            return

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default"))
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
            task = asyncio.create_task(
                self._run_emotion_update_for_message(
                    message,
                    runtime=runtime,
                    incoming_scores=incoming_scores,
                    incoming_appraisal=incoming_appraisal,
                    incoming_reaction=incoming_reaction,
                )
            )
            log.info("TRACE emotion: task scheduled channel=%s author=%s", channel_id(message), author_id(message))

            def _done_callback(t: asyncio.Task) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = t.exception()
                    if exc is not None:
                        log.exception("TRACE emotion: task failed channel=%s author=%s err=%r", channel_id(message), author_id(message), exc)

            task.add_done_callback(_done_callback)
        except Exception as e:
            log.exception("TRACE emotion: task schedule failed channel=%s author=%s err=%r", channel_id(message), author_id(message), e)
