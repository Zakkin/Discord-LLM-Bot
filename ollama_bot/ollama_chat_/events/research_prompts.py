"""返信前リサーチ結果を取り込むためのプロンプト構築および引用整形処理を担当するMixin。"""
from __future__ import annotations

import contextlib
import re
from typing import Any, TYPE_CHECKING

import discord

from lib.config_utils import cfg_int
from lib.discord_utils import author_id, display_name, strip_discord_mentions
from ...common.bot_identity import resolve_bot_identity
from ...common.ollama_helpers import truncate_text
from ..ollama_chat_types import MessageRuntime

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _ResearchPromptsBase = OllamaChatProtocol
else:
    _ResearchPromptsBase = object


from harness.agent.prompt_parser.prompt_manager.research_prompts import (
    build_research_context_text,
    build_research_reply_base_prompt,
)


class _ResearchPromptsMixin(_ResearchPromptsBase):
    """リサーチ返答のプロンプト構築および引用整形メソッドを提供するMixin。"""

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

        prompt = "\n".join([
            "以下のメッセージは、直前の流れとは別話題です。",
            f"対象メッセージ: {user_text_for_prompt}",
            "",
            "【現在の対話相手】",
            f"サーバーニックネーム（表示名）: 『{target_name}』さん",
            "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
            f"※相手を呼ぶ・お礼を言う場合は、アカウントID/ユーザー名ではなく必ず現在の対話相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。あなた自身の名前（{primary_name}等）や自分へのメンション（<@{bot_user_id}>等）で相手を呼ぶことは絶対に禁止です。",
            f"※ユーザーが発言内であなた（{primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
            "",
            "会話の続きに無理やり合わせなくてよいです。",
            "単独で意味が通る質問・依頼なら、その内容に直接答えてください。",
            "直前のAI発言、最近の会話要約、長期記憶を引きずらず、このメッセージ自体に答えてください。",
            "※ただし、対象メッセージや調査結果に以前の会話や記憶にある用語・キャラクターが含まれる場合は、知っている話題として自然に関連付けて言及してください。",
            "このあと外部調査結果が追加される場合は、その調査結果と対象メッセージを最優先してください。",
        ])

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
