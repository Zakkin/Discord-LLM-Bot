"""
[AI Agent Summary]
このファイルは、感情スコアに基づいてメモリシステムから関連する記憶（コンテキスト）を抽出する処理を担当する Mixin です。
This mixin handles the extraction of relevant memories (context) from the memory system based on emotion scores.
"""
from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

import discord

from harness.agent.memory_manager import logic as memory_logic
from ...common.config_helpers import cfg, cfg_bool
from ...common.emotion_helpers import (
    build_memory_emotion_tags,
    clamp_score,
    merge_emotion_scores,
)
from ...common.ollama_helpers import truncate_lines
from ..ollama_chat_types import MessageRuntime

log = logging.getLogger("ollama_bot.ollama_chat")


if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionMemoryBase = OllamaChatProtocol
else:
    _EmotionMemoryBase = object

from .heuristics import _infer_emotion_scores_without_llm


class OllamaChatEmotionMemoryMixin(_EmotionMemoryBase):
    async def _get_emotion_memory_lines(
        self,
        runtime: MessageRuntime,
        *,
        current_scores: dict[str, float] | None = None,
    ) -> list[str]:
        if not cfg_bool("EMOTION_MEMORY_CONTEXT_ENABLED", True):
            return []
        if not cfg("MEMORY_ENABLED", True):
            return []
        store = getattr(self, "memory_store", None)
        if store is None:
            return []

        target_text = (runtime.effective_user_text or runtime.original_user_text or "").strip()
        # 引用プレフィックス（（あなたの「...」への返信））を除去して純粋なユーザー発言で検索
        target_text = re.sub(r"^（あなたの[「『].*?[」』]への返信）\s*", "", target_text).strip() or target_text
        heuristic_scores = _infer_emotion_scores_without_llm(target_text)
        preferred_tags = build_memory_emotion_tags(
            merge_emotion_scores(current_scores, heuristic_scores),
            max_tags=max(int(cfg("EMOTION_MEMORY_MAX_TAGS", 2) or 2), 1),
            min_score=clamp_score(float(cfg("EMOTION_MEMORY_MIN_TAG_SCORE", 0.24) or 0.24)),
        )
        if not preferred_tags:
            return []

        try:
            memory_lines, _ = await memory_logic.get_relevant_memories(
                cfg=cfg,
                store=store,
                guild_id=getattr(runtime, "guild_id", None),
                channel_id=getattr(runtime, "cid", None),
                user_id=getattr(runtime, "user_id", None),
                preferred_emotion_tags=preferred_tags,
                user_text=target_text,
            )
        except Exception as e:
            log.warning("emotion memory lookup failed: %r", e)
            return []

        if not memory_lines:
            return []

        # fact_check / research タグや調査系ノイズ記憶（Gemini API、ファクトチェック等）を除外
        def _is_emotion_noise_memory(line: str) -> bool:
            lowered = line.lower()
            noise_keywords = (
                "fact_check",
                "factcheck",
                "research",
                "gemini api",
                "以前ファクトチェックした話題:",
                "ファクトチェック",
                "web検索",
                "調査結果",
                "検索結果",
                "mcp",
            )
            return any(kw in lowered for kw in noise_keywords)

        memory_lines = [line for line in memory_lines if not _is_emotion_noise_memory(line)]
        if not memory_lines:
            return []

        return truncate_lines(
            memory_lines,
            max_lines=max(int(cfg("EMOTION_MEMORY_CONTEXT_LIMIT", 2) or 2), 1),
            max_chars_per_line=max(int(cfg("EMOTION_MEMORY_MAX_CHARS_PER_LINE", 180) or 180), 80),
            max_total_chars=max(int(cfg("EMOTION_MEMORY_MAX_TOTAL_CHARS", 360) or 360), 120),
        )

    async def _get_preferred_memory_emotion_tags(self, extra_scores: dict[str, float] | None = None) -> list[str]:
        scores, _, _ = await self._get_emotion_snapshot()
        merged_scores = merge_emotion_scores(scores, extra_scores)
        return build_memory_emotion_tags(merged_scores, max_tags=2, min_score=0.34)
