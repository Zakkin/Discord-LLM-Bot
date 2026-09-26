"""OllamaChatReplyMixinのLLM出力整形、最終返答抽出、再試行ガードを担当する補助Mixin。

このファイルは生成結果からユーザーに見せる本文だけを取り出し、プロンプト漏れ、
思考漏れ、オウム返し、前回返答との過度な類似、空応答を検出して再生成または
フォールバックへ落とす。プロンプト構築と送信処理は別のreply系Mixinに分離している。
"""
import logging
import sys
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr


import discord
from . import ollama_chat_helpers as _helpers
from .ollama_chat_types import MessageRuntime
from .ollama_chat_texts import (
    FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT,
    build_character_deviation_retry_prompt,
    build_final_reply_extractor_prompt,
    build_output_only_retry_prompt,
    build_parrot_retry_prompt,
    build_similar_retry_prompt,
    build_time_contradiction_retry_prompt,
)
from .ollama_chat_helpers import (
    FINAL_REPLY_EXTRACTION_SCHEMA,
    _looks_like_role_flip_reply,
    _looks_too_similar_to_previous_reply,
)
from ..common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_character_deviation,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_parrot_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_time_of_day_contradiction,
    looks_like_truncated_reply,
    looks_like_unusable_assistant_reply,
    sanitize_generated_reply,
    strip_unprompted_nerd_emoji,
)
from ..common.reply_helpers import (
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
    from .ollama_chat_types import OllamaChatProtocol
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
        reply = sanitize_generated_reply(reply)
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
            reply = sanitize_generated_reply(reply)
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
            retry_prompt = build_similar_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._reply_retry_num_predict(),
                )
                retried_clean = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
                if _looks_too_similar_to_previous_reply(last_assistant_text, retried_clean):
                    log.warning("retried reply is still too similar to previous reply; falling back to safe reply")
                    safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "さっきと同じようなことを言いそうだったな…") or "").strip()
                    reply = (
                        backup_reply
                        if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply))
                        else (safe_fallback or "さっきと同じようなことを言いそうだったな…")
                    )
                else:
                    reply = retried_clean
            except Exception as e:
                log.warning("similar retry failed: %r", e)
                safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "さっきと同じようなことを言いそうだったな…") or "").strip()
                reply = backup_reply if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply)) else (safe_fallback or "さっきと同じようなことを言いそうだったな…")

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
                reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(retried))
            except Exception as e:
                log.warning("output-only retry failed: %r", e)
                reply = backup_reply or cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。")

        reply = sanitize_generated_reply(reply)
        reply = _strip_user_echo_prefix(runtime.original_user_text, reply)
        retried_backup = self._select_retry_backup_reply(reply)
        if retried_backup and not (
            looks_like_prompt_leak(retried_backup)
            or looks_like_reasoning_leak(retried_backup)
            or looks_like_non_japanese_reply(retried_backup)
            or looks_like_truncated_reply(retried_backup)
            or looks_like_unusable_assistant_reply(retried_backup)
            or (last_assistant_text and _looks_too_similar_to_previous_reply(last_assistant_text, retried_backup))
        ):
            backup_reply = retried_backup

        if self._needs_final_reply_extraction(reply):
            reply = await self._extract_final_user_reply(reply)
            reply = _strip_user_echo_prefix(runtime.original_user_text, sanitize_generated_reply(reply))
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

        final_reply = extract_first_user_facing_reply(reply)
        final_reply = strip_unprompted_nerd_emoji(runtime.original_user_text, final_reply)
        if last_assistant_text and _looks_too_similar_to_previous_reply(last_assistant_text, final_reply):
            log.warning("final reply is still too similar to previous reply; falling back to safe reply")
            safe_fallback = str(cfg("OLLAMA_SIMILAR_REPLY_FALLBACK", "さっきと同じようなことを言いそうだったな…") or "").strip()
            final_reply = (
                backup_reply
                if (backup_reply and not _looks_too_similar_to_previous_reply(last_assistant_text, backup_reply))
                else (safe_fallback or "さっきと同じようなことを言いそうだったな…")
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
