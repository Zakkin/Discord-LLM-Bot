"""
[AI Agent Summary]
このファイルは、感情スコアに基づいてメモリシステムから関連する記憶（コンテキスト）を抽出する処理を担当する Mixin です。
This mixin handles the extraction of relevant memories (context) from the memory system based on emotion scores.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import discord

from ...common import memory_logic
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
