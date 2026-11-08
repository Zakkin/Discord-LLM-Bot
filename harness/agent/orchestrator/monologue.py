"""AIエージェント・ハーネス オーケストレーター 内部モノローグ・欲求管理モジュール (orchestrator/monologue.py)

エージェントの内部欲求（退屈・孤独・好奇心）の更新、好奇心駆動の題材選定、独白生成、Web学習を担当するMixin。
"""
from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import logging
import random
import re
import time
from typing import Any, TYPE_CHECKING

import discord

from lib.config_utils import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
    cfg_primary_channel_id,
)
from lib.discord_utils import channel_id
from ollama_bot.common.emotion_helpers import clamp_score
from harness.agent.tool_registry.mcp.fact_check import search_web
from ollama_bot.common.ollama_helpers import (
    call_ollama_json,
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
    truncate_text,
)

log = logging.getLogger("harness.agent.orchestrator.monologue")

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


_COMMON_TOPIC_MODIFIERS = {
    "アニメ", "漫画", "マンガ", "映画", "劇場版", "動画", "ゲーム", "作品",
    "話題", "最新", "人気", "好き", "推し", "おすすめ", "オススメ", "関連",
    "情報", "感想", "展開", "登場", "配信", "放送", "公式", "ニュース",
    "記事", "考察", "内容", "様子", "こと",
}


def _extract_topic_keywords(text: str) -> list[str]:
    """トピックまたは記憶から主要名詞・固有名詞・実質語キーワードを抽出する。"""
    raw_str = str(text or "")
    # 原文から記号を自然な境界としてトークン抽出（カッコや空白が単語分離を保つ）
    raw_tokens = re.findall(
        r"[a-zA-Z0-9]{2,}"
        r"|[ァ-ヶー]+[\u4e00-\u9fff]+"
        r"|[\u4e00-\u9fff]+[ァ-ヶー]+"
        r"|[\u4e00-\u9fff]{2,}"
        r"|[ァ-ヶー]{2,}"
        r"|[ぁ-ん]{3,}",
        raw_str,
    )

    stop_words = {
        "について", "に関する", "に関して", "という",
        "さんの", "くんの", "ちゃんの", "たちの",
        "している", "してます", "だけど", "けれど",
        "だから", "でした", "ました", "ですね",
        "ですよ", "などの", "とかの", "からの",
        "あとで", "考える", "調べる",
    }

    keywords: list[str] = []
    seen: set[str] = set()

    def _add_kw(tok: str) -> None:
        t = tok.strip().lower()
        if not t or t in stop_words or t in seen:
            return
        seen.add(t)
        keywords.append(t)

    for token in raw_tokens:
        tok = token.strip().lower()
        if not tok or tok in stop_words:
            continue
        # 補助語（アニメ、映画、ゲーム等）が密着している場合の分離
        separated = False
        for mod in _COMMON_TOPIC_MODIFIERS:
            if len(mod) >= 2 and len(tok) > len(mod):
                if tok.startswith(mod):
                    _add_kw(mod)
                    _add_kw(tok[len(mod):])
                    separated = True
                    break
                elif tok.endswith(mod):
                    _add_kw(tok[:-len(mod)])
                    _add_kw(mod)
                    separated = True
                    break
        if not separated:
            _add_kw(tok)

    return keywords


def _keyword_in_text(keyword: str, text: str) -> bool:
    """キーワードまたはその主要構成要素がテキストに含まれているか判定する。"""
    if keyword in text:
        return True
    # 複合語（例: ワス先輩）が分解されてテキストに含まれているかチェック
    sub_parts = re.findall(r"[\u4e00-\u9fff]{2,}|[ァ-ヶー]{2,}|[a-zA-Z0-9]{2,}", keyword)
    if len(sub_parts) >= 2:
        return all(part in text for part in sub_parts)
    return False


def _topic_matches_candidate(
    topic_key: str,
    topic_keywords: list[str],
    candidate_key: str,
) -> bool:
    """単一の候補記憶キーに対してトピックが整合しているか判定する。"""
    # 1. 完全部分一致（全体がそのまま含まれる）
    if topic_key in candidate_key:
        return True
    if len(candidate_key) <= 80 and candidate_key in topic_key:
        return True

    if not topic_keywords:
        return False

    # 2. 各キーワードの一致チェック
    matched: list[str] = []
    unmatched: list[str] = []
    for kw in topic_keywords:
        if _keyword_in_text(kw, candidate_key):
            matched.append(kw)
        else:
            unmatched.append(kw)

    total_kws = len(topic_keywords)
    match_count = len(matched)

    # キーワードが1個の場合（例: "ワルプルギス"）
    if total_kws == 1:
        if match_count == 1:
            kw = matched[0]
            # トピック全体の主要部分がそのキーワードで占められているか
            if len(kw) >= 2 and (len(kw) / max(len(topic_key), 1)) >= 0.40:
                return True
        return False

    # キーワードが2個の場合（例: "ワス", "先輩"）
    if total_kws == 2:
        if match_count == 2:
            return True
        if match_count == 1 and unmatched[0] in _COMMON_TOPIC_MODIFIERS:
            return True
        return False

    # キーワードが3個以上の場合
    match_ratio = match_count / total_kws
    # 主要キーワードの多く（60%以上、かつ最低2個以上の一致）
    if match_ratio >= 0.60 and match_count >= 2:
        # 未一致のキーワードに未知の固有表現（ハルシネーション）がないか検査
        has_suspicious_hallucination = False
        for ukw in unmatched:
            if ukw in _COMMON_TOPIC_MODIFIERS:
                continue
            # カタカナ3文字以上（例: マイクラ）、英数字3文字以上、漢字3文字以上は新語・ハルシネーションと判定
            if re.search(r"[ァ-ヶー]{3,}|[a-zA-Z0-9]{3,}|[\u4e00-\u9fff]{3,}", ukw):
                has_suspicious_hallucination = True
                break
        if not has_suspicious_hallucination:
            return True

    return False


def _topic_matches_memory(topic: str, candidates: list[str]) -> bool:
    topic_key = _topic_compare_key(topic)
    if len(topic_key) < 3:
        return False

    topic_keywords = _extract_topic_keywords(topic)

    for candidate in candidates:
        candidate_key = _topic_compare_key(candidate)
        if not candidate_key:
            continue
        if _topic_matches_candidate(topic_key, topic_keywords, candidate_key):
            return True
    return False


def _clean_agent_topic_with_reason(raw_topic: str, candidates: list[str]) -> tuple[str, str]:
    """生成された題材テキストを検証・整形し、整形後テキストと棄却理由（成功時は空文字）のタプルを返す。"""
    raw_str = str(raw_topic or "").strip()
    if not raw_str:
        return "", "empty"

    cleaned = sanitize_generated_reply(raw_str)
    cleaned = _CHAT_SPECIAL_TOKEN_RE.split(cleaned, maxsplit=1)[0].strip()
    if not cleaned:
        return "", "empty"

    last_reason = "empty"
    lines = cleaned.splitlines() or [cleaned]
    for raw_line in lines:
        topic = _normalize_agent_topic_text(raw_line)
        if not topic:
            last_reason = "empty"
            continue
        if len(topic) > 180:
            last_reason = "length_exceeded"
            continue
        if re.search(r"(以下は|この中から|出力は|選んでください|最も|あとで軽く考え)", topic) or _CHAT_SPECIAL_TOKEN_RE.search(topic):
            last_reason = "meta_phrase_detected"
            continue
        if _topic_matches_memory(topic, candidates):
            return topic[:120].rstrip(" -・\t"), ""
        else:
            last_reason = "memory_mismatch"

    return "", last_reason


def _clean_agent_topic(raw_topic: str, candidates: list[str]) -> str:
    topic, reason = _clean_agent_topic_with_reason(raw_topic, candidates)
    if not topic:
        log.info("agent curiosity topic rejected in clean: reason=%s raw=%r", reason, raw_topic)
    return topic


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


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _AgentMonologueBase = OllamaChatProtocol
else:
    _AgentMonologueBase = object


class AgentMonologueMixin(_AgentMonologueBase):
    """エージェント内部欲求・モノローグMixin"""

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
        persona = cfg_persona_namespace(custom_cfg=cfg)
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
            # BAN-114: 自身の独白・思考・知識メモリを除外（自己増殖ループ防止）
            if item.get("memory_type") in ("agent_thought", "agent_knowledge"):
                continue

            item_text = str(item.get("content") or "").strip()
            if not item_text:
                continue

            # 過去の独白・題材・検索結果を含むテキストを除外
            if item_text.startswith("題材: ") or ("独白:" in item_text):
                continue
            if item_text.startswith("検索題材: ") or ("検索結果:" in item_text):
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
            raw_topic = str((result or {}).get("topic") or "")
            topic, reject_reason = _clean_agent_topic_with_reason(raw_topic, candidates)
            if not topic:
                log.warning("agent curiosity topic rejected: reason=%s raw=%r", reject_reason, raw_topic)
                return ""

            # BAN-114: 過去に独白済みのトピックとの重複を排除
            recent_topics: list[str] = []
            async with self._emotion_lock:
                hint = str(self.emotion_state.recent_topic_hint or "").strip()
                if hint:
                    recent_topics.append(hint)
            hist = getattr(self, "_recent_monologue_topics", None)
            if isinstance(hist, (list, deque)):
                recent_topics.extend(list(hist))

            if any(_topic_compare_key(topic) == _topic_compare_key(rt) for rt in recent_topics):
                log.warning("agent curiosity topic rejected as duplicate of recent monologue topics: %r", topic)
                return ""

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

    def is_inner_monologue_in_cooldown(self) -> bool:
        """Inner Monologue が現在クールダウン中かどうか判定する"""
        now = time.time()
        last_ts = float(getattr(self.emotion_state, "last_thought_ts", 0.0) or 0.0)
        cooldown_sec = cfg_float("AGENT_INNER_MONOLOGUE_COOLDOWN_SEC", 2 * 3600.0)
        return (now - last_ts) < cooldown_sec

    async def _execute_inner_monologue_and_learn(self) -> None:
        if not cfg_bool("AGENT_INNER_MONOLOGUE_ENABLED", True):
            log.info("[monologue] Inner monologue skipped: reason=disabled")
            return

        now = time.time()
        async with self._emotion_lock:
            last_ts = float(self.emotion_state.last_thought_ts or 0.0)
        cooldown_sec = cfg_float("AGENT_INNER_MONOLOGUE_COOLDOWN_SEC", 2 * 3600.0)
        remaining = cooldown_sec - (now - last_ts)
        if remaining > 0:
            log.info("[monologue] Inner monologue skipped: reason=cooldown (remaining=%.0fs)", remaining)
            async with self._emotion_lock:
                self.emotion_state.curiosity = max(0.0, float(self.emotion_state.curiosity or 0.0) - 0.05)
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
                    persona=cfg_persona_namespace(custom_cfg=cfg),
                    guild_id=self.bot.guilds[0].id if self.bot.guilds else None,
                    channel_id=cfg_primary_channel_id(0),
                    user_id=None,
                    memory_type="agent_thought",
                    content=f"題材: {topic}\n独白: {monologue[:400]}",
                    score=0.25,
                    emotion_tags=[],
                )
                recent_monologues = getattr(self, "_recent_monologue_topics", None)
                if not isinstance(recent_monologues, deque):
                    recent_monologues = deque(maxlen=max(cfg_int("RECENT_MONOLOGUE_TOPICS_CACHE_SIZE", 10), 5))
                    with contextlib.suppress(Exception):
                        setattr(self, "_recent_monologue_topics", recent_monologues)
                recent_monologues.append(topic)

            if cfg_bool("AGENT_WEB_LEARNING_ENABLED", False):
                web_result = await search_web(topic, max_results=int(cfg("AGENT_WEB_LEARNING_MAX_RESULTS", 3) or 3))
                web_result = str(web_result or "").strip()
                if web_result and "エラー" not in web_result and "見つかりません" not in web_result:
                    await self.memory_store.add_memory(
                        persona=cfg_persona_namespace(custom_cfg=cfg),
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


_AgentMonologueMixin = AgentMonologueMixin
