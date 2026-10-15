"""OllamaChatReplyMixinのLLM出力整形、最終返答抽出、再試行ガードを担当する補助Mixin。

このファイルは生成結果からユーザーに見せる本文だけを取り出し、プロンプト漏れ、
思考漏れ、オウム返し、前回返答との過度な類似、空応答を検出して再生成または
フォールバックへ落とす。プロンプト構築と送信処理は別のreply系Mixinに分離している。
"""
import logging
import re
import sys
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr


import discord
from ollama_bot.ollama_chat_ import ollama_chat_helpers as _helpers
from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime
from ollama_bot.ollama_chat_.ollama_chat_texts import (
    FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT,
    build_abnormal_rejection_retry_prompt,
    build_character_deviation_retry_prompt,
    build_final_reply_extractor_prompt,
    build_output_only_retry_prompt,
    build_parrot_retry_prompt,
    build_self_contradiction_retry_prompt,
    build_similar_retry_prompt,
    build_time_contradiction_retry_prompt,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    FINAL_REPLY_EXTRACTION_SCHEMA,
    _looks_like_role_flip_reply,
)
from harness.agent.prompt_parser.parser import (
    extract_first_user_facing_reply,
    extract_unknown_incomprehension_word,
    looks_like_abnormal_rejection,
    looks_like_character_deviation,
    looks_like_mimicked_kaomoji_or_hanging_tail,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_parrot_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_self_contradicting_reply,
    looks_like_similar_to_previous_reply as _looks_too_similar_to_previous_reply,
    looks_like_time_of_day_contradiction,
    looks_like_truncated_reply,
    looks_like_unknown_word_incomprehension,
    looks_like_unusable_assistant_reply,
    sanitize_abnormal_rejection,
    sanitize_generated_reply,
    strip_unprompted_nerd_emoji,
)
from .reply_helpers import (
    _build_role_flip_fallback_reply,
    _strip_user_echo_prefix,
)

log = logging.getLogger("ollama_bot.ollama_chat_.reply_guards")

_PUBLIC_REPLY_MODULE = f"{__package__}.ollama_chat_reply"
_default_cfg = _helpers.cfg
_default_call_ollama_json = _helpers.call_ollama_json


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)



def cfg(name: str, default: Any = None) -> Any:
    return _public_attr("cfg", _default_cfg)(name, default)


def call_ollama_json(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("call_ollama_json", _default_call_ollama_json)(*args, **kwargs)


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _OllamaChatReplyGuardBase = OllamaChatProtocol
else:
    _OllamaChatReplyGuardBase = object


class OllamaChatReplyGuardMixin(_OllamaChatReplyGuardBase):

    async def _extract_final_user_reply(self, raw_reply: str) -> str:
        cleaned = sanitize_generated_reply(raw_reply)
        first_pass = extract_first_user_facing_reply(cleaned)
        if first_pass and not self._needs_final_reply_extraction(first_pass):
            return first_pass
        try:
            result = await call_ollama_json(
                build_final_reply_extractor_prompt(cleaned),
                system_prompt=FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT,
                think=False,
                model=str(cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
                schema=FINAL_REPLY_EXTRACTION_SCHEMA,
                timeout_sec=float(cfg("FINAL_REPLY_EXTRACT_TIMEOUT_SEC", 20.0)),
                retries=0,
            )
            extracted = sanitize_generated_reply(str(result.get("reply") or "").strip())
            if extracted:
                return extract_first_user_facing_reply(extracted)
        except Exception as e:
            log.warning("final reply extractor failed: %r", e)
        return first_pass or extract_first_user_facing_reply(raw_reply)

    def _needs_final_reply_extraction(self, reply: str) -> bool:
        cleaned = sanitize_generated_reply(reply)
        return (
            looks_like_prompt_leak(cleaned)
            or looks_like_reasoning_leak(cleaned)
            or looks_like_multi_turn_output(cleaned)
        )

    async def _apply_retry_guards(
        self,
        message: discord.Message,
        *,
        prompt: str,
        reply: str,
        last_assistant_text: str,
        intent_info: dict[str, Any],
        runtime: MessageRuntime,
    ) -> str:
        reply = sanitize_generated_reply(reply, user_text=runtime.original_user_text)
        reply = _strip_user_echo_prefix(runtime.original_user_text, reply)
        backup_reply = self._select_retry_backup_reply(reply)

        leak_output_problem = (
            looks_like_prompt_leak(reply)
            or looks_like_reasoning_leak(reply)
            or looks_like_multi_turn_output(reply)
            or looks_like_non_japanese_reply(reply)
        )
        severe_output_problem = (
            leak_output_problem 
            or looks_like_unusable_assistant_reply(reply) 
            or looks_like_truncated_reply(reply)
        )
        needs_extract = leak_output_problem

        if needs_extract:
            reply = await self._extract_final_user_reply(reply)
            reply = sanitize_generated_reply(reply, user_text=runtime.original_user_text)
            reply = _strip_user_echo_prefix(runtime.original_user_text, reply)
            extracted_backup = self._select_retry_backup_reply(reply)
            if extracted_backup:
                backup_reply = extracted_backup

        if looks_like_unusable_assistant_reply(reply) or looks_like_truncated_reply(reply):
            retry_prompt = build_output_only_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
            except Exception as e:
                log.warning("empty/garbage retry failed: %r", e)
                reply = backup_reply or cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "今のはうまく返せなかった。もう一回言ってくれ。")
        elif _looks_like_role_flip_reply(intent_info, reply):
            reply = _build_role_flip_fallback_reply()
        elif looks_like_abnormal_rejection(reply):
            retry_prompt = build_abnormal_rejection_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
                if looks_like_abnormal_rejection(retried_clean):
                    log.warning("retried reply still has abnormal rejection; sanitizing to safe tsukkomi or falling back")
                    sanitized = sanitize_abnormal_rejection(retried_clean, user_text=runtime.original_user_text)
                    if sanitized and not looks_like_abnormal_rejection(sanitized):
                        reply = sanitized
                    else:
                        fallback = str(cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", "うまく返せなかったな…もう一回言ってくれ。") or "").strip()
                        reply = fallback or "うまく返せなかったな…もう一回言ってくれ。"
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("abnormal rejection retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", "うまく返せなかったな…もう一回言ってくれ。") or "").strip()
                reply = backup_reply if (backup_reply and not looks_like_abnormal_rejection(backup_reply)) else (safe_fallback or "うまく返せなかったな…もう一回言ってくれ。")
        elif looks_like_character_deviation(reply, user_text=runtime.original_user_text):
            retry_prompt = build_character_deviation_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
                if looks_like_character_deviation(retried_clean, user_text=runtime.original_user_text):
                    log.warning("retried reply still has character deviation; falling back to safe character reply")
                    fallback = str(cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", "うまく返せなかったな…もう一回言ってくれ。") or "").strip()
                    reply = fallback or "うまく返せなかったな…もう一回言ってくれ。"
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("character deviation retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", "うまく返せなかったな…もう一回言ってくれ。") or "").strip()
                reply = backup_reply if (backup_reply and not looks_like_character_deviation(backup_reply, user_text=runtime.original_user_text)) else (safe_fallback or "うまく返せなかったな…もう一回言ってくれ。")
        elif looks_like_time_of_day_contradiction(reply, user_text=runtime.original_user_text):
            retry_prompt = build_time_contradiction_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
            except Exception as e:
                log.warning("time contradiction retry failed: %r", e)
        elif looks_like_parrot_reply(runtime.original_user_text, reply):
            retry_prompt = build_parrot_retry_prompt(prompt, runtime.original_user_text)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
                if looks_like_parrot_reply(runtime.original_user_text, retried_clean):
                    log.warning("retried reply is still parrot-like; falling back to safe reply")
                    safe_fallback = str(cfg("OLLAMA_PARROT_FALLBACK", "うまく言葉が出てこなかったな…もう一回言ってくれ。") or "").strip()
                    reply = (
                        backup_reply
                        if (backup_reply and not looks_like_parrot_reply(runtime.original_user_text, backup_reply))
                        else (safe_fallback or "うまく言葉が出てこなかったな…もう一回言ってくれ。")
                    )
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("parrot retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_PARROT_FALLBACK", "うまく言葉が出てこなかったな…もう一回言ってくれ。") or "").strip()
                reply = backup_reply if (backup_reply and not looks_like_parrot_reply(runtime.original_user_text, backup_reply)) else (safe_fallback or "うまく言葉が出てこなかったな…もう一回言ってくれ。")
        elif last_assistant_text and _looks_too_similar_to_previous_reply(last_assistant_text, reply):
            retry_prompt = build_similar_retry_prompt(
                prompt,
                rejected_reply=reply,
                last_assistant_text=last_assistant_text,
            )
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried, user_text=runtime.original_user_text))
                if _looks_too_similar_to_previous_reply(last_assistant_text, retried_clean):
                    log.warning("retried reply is still too similar to previous reply; falling back to safe reply")
                    safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "おいおい、からかわないでくれよ…！") or "").strip()
                    reply = (
                        backup_reply
                        if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply))
                        else (safe_fallback or "おいおい、からかわないでくれよ…！")
                    )
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("similar retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "おいおい、からかわないでくれよ…！") or "").strip()
                reply = backup_reply if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply)) else (safe_fallback or "おいおい、からかわないでくれよ…！")
        elif last_assistant_text and looks_like_self_contradicting_reply(
            reply, last_assistant_text=last_assistant_text, user_text=runtime.original_user_text
        ):
            log.warning("self-contradicting reply detected: reply=%r last=%r", reply[:50], last_assistant_text[:50])
            retry_prompt = build_self_contradiction_retry_prompt(prompt, last_assistant_text=last_assistant_text)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(
                    runtime.original_user_text,
                    sanitize_generated_reply(retried, user_text=runtime.original_user_text),
                )
                if looks_like_self_contradicting_reply(
                    retried_clean,
                    last_assistant_text=last_assistant_text,
                    user_text=runtime.original_user_text,
                ):
                    log.warning("retried reply is still self-contradicting; falling back to safe reply")
                    safe_fallback = str(cfg("OLLAMA_SELF_CONTRADICTION_FALLBACK", "そうですよね…！やっぱり良いですよね…！") or "").strip()
                    reply = (
                        backup_reply
                        if (
                            backup_reply
                            and not looks_like_self_contradicting_reply(
                                backup_reply,
                                last_assistant_text=last_assistant_text,
                                user_text=runtime.original_user_text,
                            )
                        )
                        else (safe_fallback or "そうですよね…！やっぱり良いですよね…！")
                    )
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("self contradiction retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_SELF_CONTRADICTION_FALLBACK", "そうですよね…！やっぱり良いですよね…！") or "").strip()
                reply = (
                    backup_reply
                    if (
                        backup_reply
                        and not looks_like_self_contradicting_reply(
                            backup_reply,
                            last_assistant_text=last_assistant_text,
                            user_text=runtime.original_user_text,
                        )
                    )
                    else (safe_fallback or "そうですよね…！やっぱり良いですよね…！")
                )
        elif looks_like_mimicked_kaomoji_or_hanging_tail(reply, user_text=runtime.original_user_text):
            log.warning("hanging tail or mimicked kaomoji detected in reply: %r", reply[:50])
            retry_prompt = build_output_only_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(
                    runtime.original_user_text,
                    sanitize_generated_reply(retried, user_text=runtime.original_user_text),
                )
                if looks_like_mimicked_kaomoji_or_hanging_tail(retried_clean, user_text=runtime.original_user_text):
                    log.warning("retried reply still has hanging tail or kaomoji echo; using sanitized fallback")
                    safe_fallback = str(cfg("OLLAMA_HANGING_TAIL_FALLBACK", "そうなんですね…！") or "").strip()
                    reply = backup_reply or (safe_fallback or "そうなんですね…！")
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("kaomoji/hanging tail retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_HANGING_TAIL_FALLBACK", "そうなんですね…！") or "").strip()
                reply = backup_reply or (safe_fallback or "そうなんですね…！")

        elif looks_like_unknown_word_incomprehension(reply):
            word = extract_unknown_incomprehension_word(reply)
            log.info("unknown word incomprehension detected in reply: word=%r reply=%r", word, reply[:60])
            meaning = ""
            search_query = f"{word} 意味" if word else ""
            if word:
                try:
                    from harness.agent.tool_registry.adapters.web_search import WebSearchToolAdapter
                    search_res = await WebSearchToolAdapter.execute_search(query=search_query)
                    if search_res:
                        m_summary = re.search(r"概要[:：]\s*(.+)", search_res)
                        if m_summary and m_summary.group(1).strip() and "見つかりませんでした" not in m_summary.group(1):
                            meaning = m_summary.group(1).strip()
                        elif "見つかりませんでした" not in search_res and "エラーが発生" not in search_res:
                            meaning = search_res.replace("【Web検索結果】", "").strip()[:200]
                except Exception as e:
                    log.warning("on-demand WebSearchToolAdapter failed for %r: %r", word, e)

            if word and meaning:
                retry_instruction = (
                    f"\n\n[回答の再生成指示（単語の意味判明に伴う自己修正）]\n"
                    f"相手の使った言葉『{word}』の意味は【{meaning}】です。\n"
                    f"意味が判明したため、『何ですか』と聞き返したり困惑したりせず、この意味を踏まえてあなたのキャラクター設定（一人称、口調、感情）のまま等身大にリアクションしてください。"
                )
                retry_prompt = f"{prompt}{retry_instruction}"
                try:
                    retried = await self._call_with_typing(
                        message,
                        retry_prompt,
                        system_prompt=self._reply_system_prompt(),
                        images=runtime.media.images,
                        num_predict=self._reply_retry_num_predict(),
                    )
                    retried_clean = _strip_user_echo_prefix(
                        runtime.original_user_text,
                        sanitize_generated_reply(retried, user_text=runtime.original_user_text),
                    )
                    if looks_like_unknown_word_incomprehension(retried_clean):
                        log.warning("retried reply still has unknown word incomprehension: %r", retried_clean[:60])
                        reply = retried_clean
                    else:
                        reply = retried_clean
                        salvaged_data = {
                            "query": search_query,
                            "summary": meaning,
                            "mode": "browser_search",
                            "word": word,
                            "meaning": meaning,
                        }
                        setattr(runtime, "salvaged_research_result", salvaged_data)
                        if isinstance(intent_info, dict):
                            intent_info["salvaged_research_result"] = salvaged_data
                except Exception as e:
                    log.warning("unknown word retry failed: %r", e)

        elif (
            looks_like_prompt_leak(reply)
            or looks_like_reasoning_leak(reply)
            or looks_like_multi_turn_output(reply)
            or looks_like_non_japanese_reply(reply)
        ):
            retry_prompt = build_output_only_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried, user_text=runtime.original_user_text))
            except Exception as e:
                log.warning("output-only retry failed: %r", e)
                reply = backup_reply or cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。")

        reply = sanitize_generated_reply(reply, user_text=runtime.original_user_text)
        reply = _strip_user_echo_prefix(runtime.original_user_text, reply)
        retried_backup = self._select_retry_backup_reply(reply)
        if retried_backup and not (
            looks_like_prompt_leak(retried_backup)
            or looks_like_reasoning_leak(retried_backup)
            or looks_like_non_japanese_reply(retried_backup)
            or looks_like_truncated_reply(retried_backup)
            or looks_like_unusable_assistant_reply(retried_backup)
            or (last_assistant_text and _looks_too_similar_to_previous_reply(last_assistant_text, retried_backup))
            or (last_assistant_text and looks_like_self_contradicting_reply(retried_backup, last_assistant_text=last_assistant_text, user_text=runtime.original_user_text))
        ):
            backup_reply = retried_backup

        if self._needs_final_reply_extraction(reply):
            reply = await self._extract_final_user_reply(reply)
            reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(reply, user_text=runtime.original_user_text))
            extracted_backup = self._select_retry_backup_reply(reply)
            if extracted_backup and not (
                looks_like_prompt_leak(extracted_backup)
                or looks_like_reasoning_leak(extracted_backup)
                or looks_like_non_japanese_reply(extracted_backup)
                or looks_like_truncated_reply(extracted_backup)
                or looks_like_unusable_assistant_reply(extracted_backup)
            ):
                backup_reply = extracted_backup

        final_leak_problem = (
            looks_like_prompt_leak(reply)
            or looks_like_reasoning_leak(reply)
            or looks_like_multi_turn_output(reply)
            or looks_like_non_japanese_reply(reply)
            or looks_like_truncated_reply(reply)
        )
        final_unusable_problem = looks_like_unusable_assistant_reply(reply)
        if final_leak_problem or final_unusable_problem:
            fallback_default = (
                "今のはうまく言葉になっていません。"
                if final_leak_problem
                else "今のはうまく返せなかった。もう一回言ってくれ。"
            )
            fallback_config = (
                "OLLAMA_REASONING_LEAK_FALLBACK"
                if final_leak_problem
                else "OLLAMA_EMPTY_REPLY_FALLBACK"
            )
            reply = str(
                cfg(fallback_config, fallback_default)
                or fallback_default
            ).strip()
            if backup_reply and not (
                looks_like_prompt_leak(backup_reply)
                or looks_like_reasoning_leak(backup_reply)
                or looks_like_non_japanese_reply(backup_reply)
                or looks_like_truncated_reply(backup_reply)
                or looks_like_unusable_assistant_reply(backup_reply)
            ):
                reply = backup_reply

        final_reply = extract_first_user_facing_reply(reply, user_text=runtime.original_user_text)
        final_reply = strip_unprompted_nerd_emoji(runtime.original_user_text, final_reply)
        if last_assistant_text and _looks_too_similar_to_previous_reply(last_assistant_text, final_reply):
            log.warning("final reply is still too similar to previous reply; falling back to safe reply")
            safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "おいおい、からかわないでくれよ…！") or "").strip()
            final_reply = (
                backup_reply
                if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply))
                else (safe_fallback or "おいおい、からかわないでくれよ…！")
            )
        if last_assistant_text and looks_like_self_contradicting_reply(
            final_reply, last_assistant_text=last_assistant_text, user_text=runtime.original_user_text
        ):
            log.warning("final reply is self-contradicting; falling back to safe reply")
            safe_fallback = str(cfg("OLLAMA_SELF_CONTRADICTION_FALLBACK", "そうですよね…！やっぱり良いですよね…！") or "").strip()
            final_reply = (
                backup_reply
                if (
                    backup_reply
                    and not looks_like_self_contradicting_reply(
                        backup_reply,
                        last_assistant_text=last_assistant_text,
                        user_text=runtime.original_user_text,
                    )
                )
                else (safe_fallback or "そうですよね…！やっぱり良いですよね…！")
            )
        if looks_like_mimicked_kaomoji_or_hanging_tail(final_reply, user_text=runtime.original_user_text):
            log.warning("final reply has mimicked kaomoji or hanging tail; falling back to safe reply")
            safe_fallback = str(cfg("OLLAMA_HANGING_TAIL_FALLBACK", "そうなんですね…！") or "").strip()
            final_reply = (
                backup_reply
                if (
                    backup_reply
                    and not looks_like_mimicked_kaomoji_or_hanging_tail(
                        backup_reply,
                        user_text=runtime.original_user_text,
                    )
                )
                else (safe_fallback or "そうなんですね…！")
            )
        if (
            looks_like_unusable_assistant_reply(final_reply)
            or looks_like_truncated_reply(final_reply)
            or looks_like_non_japanese_reply(final_reply)
            or looks_like_reasoning_leak(final_reply)
        ):
            if backup_reply and not (
                looks_like_prompt_leak(backup_reply)
                or looks_like_reasoning_leak(backup_reply)
                or looks_like_non_japanese_reply(backup_reply)
                or looks_like_truncated_reply(backup_reply)
                or looks_like_unusable_assistant_reply(backup_reply)
            ):
                return strip_unprompted_nerd_emoji(runtime.original_user_text, backup_reply)
            fallback = str(
                cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "今のはうまく返せなかった。もう一回言ってくれ。")
                or "今のはうまく返せなかった。もう一回言ってくれ。"
            ).strip()
            if looks_like_unusable_assistant_reply(fallback):
                fallback = "今のはうまく返せなかった。もう一回言ってくれ。"
            return fallback
        if looks_like_character_deviation(final_reply, user_text=runtime.original_user_text):
            if backup_reply and not looks_like_character_deviation(backup_reply, user_text=runtime.original_user_text):
                return strip_unprompted_nerd_emoji(runtime.original_user_text, backup_reply)
            fallback_char = str(
                cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", "うまく返せなかったな…もう一回言ってくれ。")
                or "うまく返せなかったな…もう一回言ってくれ。"
            ).strip()
            return fallback_char
        return final_reply
