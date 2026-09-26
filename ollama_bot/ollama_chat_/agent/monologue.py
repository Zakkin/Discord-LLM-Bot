"""
[AI Agent Summary]
エージェントの内部欲求（退屈・孤独・好奇心）の更新、好奇心駆動の題材選定、独白生成、Web学習を担当するMixin。
This module handles internal needs updates (boredom, loneliness, curiosity), curiosity-driven topic extraction from memories, monologue generation, and web learning.
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
)
from ...common.discord_helpers import channel_id
from ...common.emotion_helpers import clamp_score
from ...common.fact_check import search_web
from ...common.ollama_helpers import (
    call_ollama_json,
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
    truncate_text,
)
from ..ollama_chat_helpers import log

_AGENT_TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
    },
    "required": ["topic"],
}

_AGENT_MONOLOGUE_SCHEMA = {
    "type": "object",
    "properties": {
        "monologue": {"type": "string"},
    },
    "required": ["monologue"],
}

_CHAT_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>]+?\|>", flags=re.IGNORECASE)


def _normalize_agent_topic_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\s*(?:[-*+・]|[0-9０-９]+[.)．、])\s*", "", value)
    value = re.sub(r"^\s*(?:題材|topic)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    value = value.strip(" \t\r\n'\"`*_#「」『』（）()[]【】")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -・\t")


def _topic_compare_key(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[`*_#\"'「」『』（）()\[\]【】、。,.，．:：;；!?！？・/／\\|-]", "", value)
    return value.lower()


def _topic_matches_memory(topic: str, candidates: list[str]) -> bool:
    topic_key = _topic_compare_key(topic)
    if len(topic_key) < 3:
        return False

    for candidate in candidates:
        candidate_key = _topic_compare_key(candidate)
        if not candidate_key:
            continue
        if topic_key in candidate_key:
            return True
        if len(candidate_key) <= 80 and candidate_key in topic_key:
            return True
    return False


def _clean_agent_topic(raw_topic: str, candidates: list[str]) -> str:
    cleaned = sanitize_generated_reply(str(raw_topic or ""))
    cleaned = _CHAT_SPECIAL_TOKEN_RE.split(cleaned, maxsplit=1)[0]

    for raw_line in cleaned.splitlines() or [cleaned]:
        topic = _normalize_agent_topic_text(raw_line)
        if not topic:
            continue
        if len(topic) > 180:
            continue
        if re.search(r"(以下は|この中から|出力は|選んでください|最も|あとで軽く考え)", topic):
            continue
        if _CHAT_SPECIAL_TOKEN_RE.search(topic):
            continue
        if _topic_matches_memory(topic, candidates):
            return topic[:120].rstrip(" -・\t")
    return ""


def _clean_agent_monologue(raw_monologue: str) -> str:
    raw = str(raw_monologue or "").strip()
    if "<think" in raw.lower() or "thinking process" in raw.lower():
        return ""
    cleaned = extract_first_user_facing_reply(raw)
    cleaned = _CHAT_SPECIAL_TOKEN_RE.split(cleaned, maxsplit=1)[0].strip()
    if not cleaned:
        return ""
    if len(cleaned) > 400:
        cleaned = truncate_text(cleaned, 400)
    if looks_like_abnormal_assistant_reply(cleaned):
        return ""
    return cleaned


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _AgentMonologueBase = OllamaChatProtocol
else:
    _AgentMonologueBase = object


class _AgentMonologueMixin(_AgentMonologueBase):
    def _mark_human_activity(self, message: discord.Message) -> None:
        if getattr(message.author, "bot", False):
            return
        cid = channel_id(message)
        if cid not in self._managed_channel_ids():
            return
        now = time.time()
        self._last_managed_human_message_ts = now
        self.emotion_state.last_human_message_ts = now

    async def _update_agent_needs(self, *, elapsed_sec: float) -> None:
        elapsed_sec = max(float(elapsed_sec), 0.0)
        idle_sec = max(time.time() - float(self._last_managed_human_message_ts or 0.0), 0.0)
        async with self._emotion_lock:
            state = self.emotion_state

            boredom_gain = min(elapsed_sec / max(cfg_float("AGENT_BOREDOM_FULL_SEC", 4 * 3600.0), 600.0), 0.35)
            loneliness_gain = min(elapsed_sec / max(cfg_float("AGENT_LONELINESS_FULL_SEC", 6 * 3600.0), 900.0), 0.25)

            if float(state.last_human_message_ts or 0.0) > 0:
                if idle_sec < cfg_float("AGENT_ACTIVE_RESET_IDLE_SEC", 15 * 60.0):
                    boredom_gain *= 0.20
                    loneliness_gain *= 0.10

            state.boredom = clamp_score(float(state.boredom or 0.0) + boredom_gain)
            state.loneliness = clamp_score(float(state.loneliness or 0.0) + loneliness_gain)
            state.curiosity = clamp_score(float(state.curiosity or 0.0) + min(elapsed_sec / max(cfg_float("AGENT_CURIOSITY_FULL_SEC", 8 * 3600.0), 1800.0), 0.18))

            mood, mood_reason = self._derive_mood_from_state()
            state.mood = mood
            state.mood_reason = mood_reason
            state.last_agent_tick_ts = time.time()
        self._save_emotion_state()

    async def _choose_curiosity_topic_from_memories(self) -> str:
        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default"))
        primary_channel_id = cfg_primary_channel_id(0)
        guild_id = None
        if self.bot.guilds:
            guild_id = self.bot.guilds[0].id

        items = await self.memory_store.get_recent_channel_memories(
            persona=persona,
            guild_id=guild_id,
            channel_id=primary_channel_id,
            limit=10,
            min_score=0.0,
        )
        if not items:
            return ""

        candidates: list[str] = []
        for item in items:
            item_text = str(item.get("content") or "").strip()
            if not item_text:
                continue
            if len(item_text) > 160:
                item_text = item_text[:160]
            candidates.append(item_text)
        if not candidates:
            return ""

        prompt = "\n".join([
            "以下は最近の記憶の断片です。",
            "この中から『あとで軽く考えたり調べたりすると面白そうな題材』を1つだけ短く選んでください。",
            "必ず記憶断片に含まれる語句だけを使い、新しい事実を追加しないでください。",
            'JSONだけで {"topic":"短い題材"} の形で返してください。',
            "",
            *[f"- {c}" for c in candidates[:8]],
        ])
        try:
            result = await call_ollama_json(
                prompt,
                system_prompt="あなたは題材の抽出器です。説明文や前置きは不要です。JSONだけを返してください。",
                schema=_AGENT_TOPIC_SCHEMA,
                think=False,
                model=cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", ""))),
                temperature=0.0,
                retries=0,
                num_predict=max(cfg_int("AGENT_TOPIC_NUM_PREDICT", 64), 32),
            )
            topic = _clean_agent_topic(str((result or {}).get("topic") or ""), candidates)
            if not topic:
                log.warning("agent curiosity topic rejected: raw=%r", (result or {}).get("topic"))
            return topic
        except Exception as e:
            log.warning("agent curiosity topic selection failed: %r", e)
            return ""

    async def _cool_down_failed_inner_monologue(self) -> None:
        async with self._emotion_lock:
            self.emotion_state.curiosity = max(0.0, float(self.emotion_state.curiosity or 0.0) - 0.20)
            self.emotion_state.last_thought_ts = time.time()
            self.emotion_state.mood, self.emotion_state.mood_reason = self._derive_mood_from_state()
        self._save_emotion_state()

    async def _execute_inner_monologue_and_learn(self) -> None:
        if not cfg_bool("AGENT_INNER_MONOLOGUE_ENABLED", True):
            return

        now = time.time()
        async with self._emotion_lock:
            last_ts = float(self.emotion_state.last_thought_ts or 0.0)
        if (now - last_ts) < cfg_float("AGENT_INNER_MONOLOGUE_COOLDOWN_SEC", 2 * 3600.0):
            return

        topic = await self._choose_curiosity_topic_from_memories()
        if not topic:
            await self._cool_down_failed_inner_monologue()
            return

        monologue_prompt = "\n".join([
            f"題材: {topic}",
            "この題材について、Discordの会話の流れを壊さない範囲で1人で短く考えてください。",
            "出力は2〜4文程度の簡潔な独白にしてください。",
        ])
        try:
            try:
                result = await call_ollama_json(
                    monologue_prompt,
                    system_prompt="あなたは短い独白を作る補助モデルです。JSONだけを返してください。",
                    schema=_AGENT_MONOLOGUE_SCHEMA,
                    think=False,
                    model=cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", ""))),
                    temperature=0.3,
                    retries=0,
                    num_predict=max(cfg_int("AGENT_INNER_MONOLOGUE_NUM_PREDICT", 160), 64),
                )
                monologue = _clean_agent_monologue(str((result or {}).get("monologue") or ""))
            except Exception as e:
                log.warning("agent inner monologue generation failed: %r", e)
                monologue = ""
            if not monologue:
                log.warning("agent inner monologue rejected: topic=%r", topic)
            if monologue:
                await self.memory_store.add_memory(
                    persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default")),
                    guild_id=self.bot.guilds[0].id if self.bot.guilds else None,
                    channel_id=cfg_primary_channel_id(0),
                    user_id=None,
                    memory_type="agent_thought",
                    content=f"題材: {topic}\n独白: {monologue[:400]}",
                    score=0.25,
                    emotion_tags=[],
                )

            if cfg_bool("AGENT_WEB_LEARNING_ENABLED", False):
                web_result = await search_web(topic, max_results=int(cfg("AGENT_WEB_LEARNING_MAX_RESULTS", 3) or 3))
                web_result = str(web_result or "").strip()
                if web_result and "エラー" not in web_result and "見つかりません" not in web_result:
                    await self.memory_store.add_memory(
                        persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default")),
                        guild_id=self.bot.guilds[0].id if self.bot.guilds else None,
                        channel_id=cfg_primary_channel_id(0),
                        user_id=None,
                        memory_type="agent_knowledge",
                        content=f"検索題材: {topic}\n検索結果:\n{web_result[:1200]}",
                        score=0.30,
                        emotion_tags=[],
                    )

            async with self._emotion_lock:
                self.emotion_state.curiosity = max(0.0, float(self.emotion_state.curiosity or 0.0) - 0.55)
                self.emotion_state.last_thought_ts = time.time()
                self.emotion_state.recent_topic_hint = topic[:120]
                self.emotion_state.mood, self.emotion_state.mood_reason = self._derive_mood_from_state()
            self._save_emotion_state()
        except Exception as e:
            log.exception("agent inner monologue failed: %s", e)
