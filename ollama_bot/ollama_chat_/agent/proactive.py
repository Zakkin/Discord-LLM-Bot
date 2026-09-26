"""
[AI Agent Summary]
自発的な話題提供（Xタイムライン、ふたばIMG掲示板、ユーザー関心事、記憶）、プロンプト構築、送信処理を担当するMixin。
This module handles spontaneous proactive posting using various sources (X timeline, Futaba IMG catalog, user interests, memories), prompt construction, and message delivery.
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
from typing import Any

import discord

from ...common.config_helpers import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_primary_channel_id,
    model_supports_thinking,
)
from ...common.discord_helpers import append_context_message
from ...common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
    truncate_text,
)
from ...common.reply_helpers import _format_reply
from ..ollama_chat_helpers import (
    _build_topic_prompt,
    append_chat_reply_output_suffix,
    build_output_only_retry_prompt,
    build_topic_system_prompt,
    choose_spontaneous_source,
    fetch_recent_x_timeline_items,
    format_user_interests_for_prompt,
    format_x_timeline_items_for_prompt,
    log,
    rotate_x_timeline_items_for_spontaneous,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _AgentProactiveBase = OllamaChatProtocol
else:
    _AgentProactiveBase = object


class _AgentProactiveMixin(_AgentProactiveBase):
    async def _build_proactive_memory_prompt(self) -> str:
        action_type = random.choices(["chat", "topic"], weights=[0.7, 0.3])[0]
        if action_type == "topic":
            return _build_topic_prompt()

        topic_hint = ""
        async with self._emotion_lock:
            topic_hint = str(self.emotion_state.recent_topic_hint or "").strip()
        topic_hint = re.sub(r"\s+", " ", topic_hint).strip(" -・\t")
        prompt = "Discordの雑談チャンネルに、会話のきっかけになる自然で短い話題を1つ投げて。"
        prompt += (
            "\n\n※最重要ルール:\n"
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを"
            "『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。\n"
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。"
        )
        if topic_hint:
            prompt += (
                "\n\n[内部素材]"
                f"\n最近気になっている題材（このラベルと原文は出力禁止）: {topic_hint}"
                "\nこの題材は必要なら自然に言い換えて、セリフ本文だけを出してください。"
            )
        return prompt

    def _build_proactive_x_timeline_prompt(self, timeline_block: str) -> str:
        return "\n".join([
            "Xのタイムラインで見かけた投稿をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "投稿本文の丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[Xタイムライン直近投稿]",
            timeline_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

    def _build_proactive_img_prompt(self, catalog_block: str) -> str:
        return "\n".join([
            "ふたば☆ちゃんねるのIMG掲示板で見かけた面白いスレッドを材料にして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "スレッドの丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[IMG 勢い上位スレッド]",
            catalog_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

    def _build_proactive_user_interest_prompt(self, interest_block: str) -> str:
        return "\n".join([
            "サーバーのメンバーが以前興味を持っていた話題をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "分析結果の読み上げや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[サーバーメンバーの関心事]",
            interest_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

    def _proactive_num_predict(self) -> int | None:
        configured = cfg_int("AGENT_PROACTIVE_NUM_PREDICT", 0)
        if configured > 0:
            return configured

        if model_supports_thinking():
            return 2048

        base = cfg_int("OLLAMA_REPLY_NUM_PREDICT", 128)
        return max(base, 2048) if base > 0 else 2048

    def _proactive_retry_num_predict(self) -> int | None:
        configured = cfg_int("AGENT_PROACTIVE_RETRY_NUM_PREDICT", 0)
        if configured > 0:
            return configured

        if model_supports_thinking():
            return 3072

        base = self._proactive_num_predict() or 0
        return max(base * 2, 3072) if base > 0 else 3072

    def _proactive_timeout_sec(self) -> float:
        return max(cfg_float("AGENT_PROACTIVE_TIMEOUT_SEC", 120.0), 10.0)

    async def _resolve_proactive_channel(
        self,
        channel_id_value: int,
    ) -> discord.TextChannel | discord.Thread | None:
        channel = self.bot.get_channel(channel_id_value)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel

        fetch_channel = getattr(self.bot, "fetch_channel", None)
        if callable(fetch_channel):
            try:
                fetched = await fetch_channel(channel_id_value)
            except Exception as e:
                log.warning("proactive channel fetch failed channel_id=%s err=%r", channel_id_value, e)
            else:
                if isinstance(fetched, (discord.TextChannel, discord.Thread)):
                    return fetched
                log.warning(
                    "proactive channel fetch returned unsupported type channel_id=%s type=%s",
                    channel_id_value,
                    type(fetched).__name__,
                )
                return None

        if channel is None:
            log.warning("proactive channel is not cached channel_id=%s", channel_id_value)
        else:
            log.warning(
                "proactive channel has unsupported cached type channel_id=%s type=%s",
                channel_id_value,
                type(channel).__name__,
            )
        return None

    def _sanitize_proactive_reply(self, reply: str) -> str:
        cleaned = extract_first_user_facing_reply(str(reply or ""))
        if cleaned and not looks_like_abnormal_assistant_reply(cleaned):
            return cleaned
        cleaned = sanitize_generated_reply(str(reply or "")).strip()
        if cleaned and not looks_like_abnormal_assistant_reply(cleaned):
            return cleaned
        return ""

    async def _generate_proactive_reply(
        self,
        prompt: str,
        *,
        system_prompt: str,
        source: str,
    ) -> str:
        prompt_with_suffix = append_chat_reply_output_suffix(prompt)
        raw_reply = await self._call_model(
            prompt_with_suffix,
            system_prompt=system_prompt,
            num_predict=self._proactive_num_predict(),
            timeout_sec=self._proactive_timeout_sec(),
        )
        cleaned = self._sanitize_proactive_reply(raw_reply)
        if cleaned:
            return cleaned

        log.warning(
            "proactive reply unusable source=%s stage=initial raw=%r",
            source,
            truncate_text(str(raw_reply or ""), 240),
        )

        retry_prompt = build_output_only_retry_prompt(prompt)
        retried = await self._call_model(
            retry_prompt,
            system_prompt=system_prompt,
            num_predict=self._proactive_retry_num_predict(),
            timeout_sec=self._proactive_timeout_sec(),
        )
        cleaned = self._sanitize_proactive_reply(retried)
        if cleaned:
            return cleaned

        log.warning(
            "proactive reply unusable source=%s stage=retry raw=%r",
            source,
            truncate_text(str(retried or ""), 240),
        )
        return ""

    async def _store_proactive_bot_utterance(self, channel: discord.abc.Messageable, raw_reply: str) -> None:
        try:
            cleaned_reply = extract_first_user_facing_reply(str(raw_reply or ""))
            if not cleaned_reply or looks_like_abnormal_assistant_reply(cleaned_reply):
                return
            store_method = getattr(self.memory_store, "add_bot_utterance", None)
            if not callable(store_method):
                return
            guild_obj = getattr(channel, "guild", None)
            await store_method(
                persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
                guild_id=getattr(guild_obj, "id", None),
                channel_id=getattr(channel, "id", None),
                content=cleaned_reply,
            )
        except Exception as e:
            log.exception("proactive bot utterance save failed: %s", e)

    async def _proactive_action(self) -> None:
        if not getattr(self, "is_active", True):
            return
        if not cfg_bool("AGENT_PROACTIVE_POST_ENABLED", True):
            return
        channel_id_value = cfg_primary_channel_id(0)
        if channel_id_value <= 0:
            return

        async with self._emotion_lock:
            last_human_ts = float(self.emotion_state.last_human_message_ts or 0.0)
            last_post_ts = float(self.emotion_state.last_proactive_post_ts or 0.0)

        if last_human_ts > 0:
            idle_sec = max(time.time() - float(self._last_managed_human_message_ts or 0.0), 0.0)
            if idle_sec < cfg_float("AGENT_PROACTIVE_MIN_IDLE_SEC", 45 * 60.0):
                return

        if (time.time() - last_post_ts) < cfg_float("AGENT_PROACTIVE_COOLDOWN_SEC", 3 * 3600.0):
            return

        channel = await self._resolve_proactive_channel(channel_id_value)
        if channel is None:
            return

        proactive_system_prompt = build_topic_system_prompt()

        try:
            async with channel.typing():
                source = choose_spontaneous_source()
                log.info("proactive action starting: chosen source=%s", source)
                prompt = ""

                if source == "x":
                    try:
                        log.info("proactive action: attempting to fetch X timeline")
                        timeline_limit = max(cfg_int("SPONTANEOUS_X_TIMELINE_POSTS", 10), 1)
                        timeline_items = await fetch_recent_x_timeline_items(limit=timeline_limit)
                        timeline_items = rotate_x_timeline_items_for_spontaneous(self, timeline_items, limit=timeline_limit)
                        timeline_block = format_x_timeline_items_for_prompt(timeline_items, limit=timeline_limit)
                        if not timeline_block:
                            raise RuntimeError("empty x timeline")
                        prompt = self._build_proactive_x_timeline_prompt(timeline_block)
                        log.info("proactive action: successfully built X timeline prompt")
                    except Exception as e:
                        log.warning("x proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "img":
                    try:
                        log.info("proactive action: attempting to fetch IMG catalog")
                        from ...common.web_research import _img_helper
                        fetch_func = getattr(_img_helper, "fetch_img_top5_threads", None)
                        if callable(fetch_func):
                            timeline_items = await fetch_func(limit=5)
                            if not timeline_items:
                                raise RuntimeError("empty img catalog")

                            format_func = getattr(_img_helper, "format_img_top5_summary", None)
                            if callable(format_func):
                                timeline_block = format_func(timeline_items)
                            else:
                                timeline_block = "\n".join([f"- {t.get('text', '')}" for t in timeline_items])

                            prompt = self._build_proactive_img_prompt(timeline_block)
                            log.info("proactive action: successfully built IMG prompt")
                        else:
                            raise RuntimeError("img helper fetch_img_top5_threads not found")
                    except Exception as e:
                        log.warning("img proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "user_interest":
                    try:
                        log.info("proactive action: attempting to fetch user interests")
                        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip()
                        guild_obj = getattr(channel, "guild", None)
                        guild_id_val = getattr(guild_obj, "id", None)
                        interests = await self.memory_store.list_user_interests(
                            persona=persona,
                            guild_id=guild_id_val,
                            channel_id=channel.id,
                            limit=5,
                            min_interest_level=cfg_float("USER_INTERESTS_MIN_SCORE", 0.3),
                        )
                        if not interests:
                            raise RuntimeError("no user interests found")

                        selected_interests = random.sample(interests, min(len(interests), random.randint(1, 2)))
                        interest_block = format_user_interests_for_prompt(selected_interests)
                        if not interest_block:
                            raise RuntimeError("empty user interests block")
                        prompt = self._build_proactive_user_interest_prompt(interest_block)
                        log.info("proactive action: successfully built user interest prompt")
                        for it in selected_interests:
                            if it.get("id"):
                                with contextlib.suppress(Exception):
                                    await self.memory_store.touch_user_interest(int(it["id"]))
                    except Exception as e:
                        log.warning("user interest proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "memory":
                    log.info("proactive action: using memory source")
                    prompt = await self._build_proactive_memory_prompt()

                reply = await self._generate_proactive_reply(
                    prompt,
                    system_prompt=proactive_system_prompt,
                    source=source,
                )
                if not reply and source != "memory":
                    log.warning("proactive reply source=%s was unusable; retrying with memory prompt", source)
                    source = "memory"
                    prompt = await self._build_proactive_memory_prompt()
                    reply = await self._generate_proactive_reply(
                        prompt,
                        system_prompt=proactive_system_prompt,
                        source=source,
                    )
                if not reply:
                    log.warning("proactive action skipped: no usable reply source=%s", source)
                    return
            sent = await channel.send(_format_reply(reply))
            await self._store_proactive_bot_utterance(channel, reply)
            append_context_message(self.channel_context_cache, sent, bot_user_id=self.bot.user.id if self.bot.user else None)

            try:
                if hasattr(self, "_store_channel_research_cache"):
                    if source == "img" and "timeline_items" in locals() and timeline_items:
                        self._store_channel_research_cache(channel.id, {
                            "mode": "img_top5",
                            "summary": str(timeline_block or ""),
                            "threads": list(timeline_items),
                            "sources": [str(t.get("url") or "") for t in timeline_items if t.get("url")],
                            "query": "https://img.2chan.net/b/futaba.php?mode=cat&sort=6",
                        })
                    elif source == "x" and "timeline_items" in locals() and timeline_items:
                        self._store_channel_research_cache(channel.id, {
                            "mode": "x_timeline",
                            "summary": str(timeline_block or ""),
                            "sources": [str(p.get("url") or "") for p in timeline_items if p.get("url")],
                            "query": "https://x.com/home",
                        })

                pseudo_hint = ""
                if source == "x" and "timeline_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はXタイムラインから拾った: {timeline_block[:800]})"
                elif source == "img" and "timeline_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はふたばの勢い上位スレッドから拾った: {timeline_block[:800]})"
                elif source == "user_interest" and "interest_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はユーザーの関心事から拾った: {interest_block[:800]})"
                elif source == "memory":
                    async with self._emotion_lock:
                        topic_val = str(self.emotion_state.recent_topic_hint or "").strip()
                    if topic_val:
                        pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景は最近気になっていた題材から拾った: {topic_val})"

                if pseudo_hint and channel.id in self.channel_context_cache:
                    turn: dict[str, Any] = {
                        "id": -random.randint(1000000, 9999999),
                        "author_id": None,
                        "role": "system",
                        "name": "System",
                        "content": pseudo_hint,
                        "line": f"System: {pseudo_hint}",
                        "created_at_ts": time.time(),
                    }
                    self.channel_context_cache[channel.id].append(turn)
            except Exception as e:
                log.warning("failed to inject proactive pseudo hint or research cache: %r", e)
            async with self._emotion_lock:
                self.emotion_state.boredom = max(0.0, float(self.emotion_state.boredom or 0.0) - 0.70)
                self.emotion_state.loneliness = max(0.0, float(self.emotion_state.loneliness or 0.0) - 0.55)
                self.emotion_state.last_proactive_post_ts = time.time()
                self.emotion_state.mood, self.emotion_state.mood_reason = self._derive_mood_from_state()
            self._save_emotion_state()
        except Exception as e:
            log.exception("agent proactive action failed: %s", e)
