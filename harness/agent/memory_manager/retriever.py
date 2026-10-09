"""
[AI Agent Summary]
記憶の想起（RAG）、埋め込みベクトル検索、FTS5キーワード検索、ハイブリッド検索、スコアリング、フォーマットを担当するモジュール。
This module handles memory retrieval (RAG), vector search, FTS5 keyword search, hybrid search, scoring, and prompt formatting.
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect
import logging
import re
from typing import Any

from .compat import get_ollama_helpers, json_loads
from .store import MemoryStore
from .extractor import (
    _is_self_referential_memory,
    _looks_like_noise_memory,
    _persona_from_cfg,
)
from ollama_bot.common.emotion_helpers import format_emotion_tags_ja
from ollama_bot.common import ollama_helpers

log = logging.getLogger("harness.agent.memory_manager.retriever")


async def _safe_await(val: Any, default: Any = None) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val if val is not None else default


def _cosine_similarity(vec1: list[float], vec2: list[float] | None) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1, norm2 = sum(a * a for a in vec1) ** 0.5, sum(b * b for b in vec2) ** 0.5
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0


def _embed_model_from_cfg(cfg: Any) -> str:
    return str(cfg("OLLAMA_EMBED_MODEL", ""))


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = cfg(name, default)
    return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)


def _same_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except Exception:
        return False


def _parse_emotion_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    try:
        parsed = json_loads(str(raw).strip())
        return [str(v).strip() for v in parsed if str(v).strip()] if isinstance(parsed, list) else []
    except Exception:
        return []


def _relative_time_label(iso_time_str: str) -> str:
    text = str(iso_time_str or "").strip()
    if not text:
        return "過去"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        sec = max((datetime.now(timezone.utc) - dt).total_seconds(), 0.0)
        for limit, label in ((3600, "ついさっき"), (86400, "数時間前"), (86400 * 7, "数日前"), (86400 * 30, "少し前"), (86400 * 180, "数ヶ月前")):
            if sec < limit:
                return label
        return "だいぶ前"
    except Exception:
        return "過去"


def _format_memory_line(item: dict[str, Any]) -> str:
    content = str(item.get("content", "")).strip()
    if not content:
        return ""
    time_text = _relative_time_label(str(item.get("updated_at") or item.get("created_at") or ""))
    emotion_text = format_emotion_tags_ja(_parse_emotion_tags(item.get("emotion_tags")))

    prefix_parts: list[str] = []
    if time_text:
        prefix_parts.append(time_text)
    if emotion_text:
        prefix_parts.append(f"当時の気分: {emotion_text}")
    if item.get("_emotion_hook_match"):
        prefix_parts.append("今の気分に近い記憶")
    if item.get("_semantic_match"):
        prefix_parts.append("関連する記憶")
    elif item.get("_keyword_match"):
        prefix_parts.append("関連する記憶")
    return f"[{' / '.join(prefix_parts)}] {content}" if prefix_parts else content


def _build_fts_query(text: str) -> str | None:
    clean = re.sub(r'<@!?\d+>', '', str(text or "")).strip()
    clean = re.sub(r'https?://\S+', '', clean).strip()
    clean = re.sub(r'[\r\n\t]+', ' ', clean).strip()
    if not clean:
        return None
    tokens = re.findall(r'[A-Za-z0-9_]{2,}|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]{2,}', clean)
    if not tokens:
        sub = re.sub(r'["\'\*\(\)\:\^\?\!！？、。]', ' ', clean).strip()
        if len(sub) >= 2:
            return f'"{sub[:30]}"'
        return None

    unique_tokens: list[str] = []
    for t in tokens:
        if t not in unique_tokens:
            unique_tokens.append(t)
        if len(unique_tokens) >= 3:
            break

    if not unique_tokens:
        return None
    return ' OR '.join(f'"{t}"' for t in unique_tokens)


async def get_relevant_memories(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    preferred_emotion_tags: list[str] | None = None,
    user_text: str | None = None,
    include_channel_memories: bool | None = None,
    include_channel_summary: bool | None = None,
) -> tuple[list[str], str | None]:
    persona = _persona_from_cfg(cfg)
    top_k = int(cfg("MEMORY_TOP_K", 5))
    min_score = float(cfg("MEMORY_MIN_SCORE", 0.55))
    preferred = [str(tag).strip() for tag in (preferred_emotion_tags or []) if str(tag).strip()]
    candidate_multiplier = max(1, int(cfg("MEMORY_EMOTION_SEARCH_CANDIDATE_MULTIPLIER", 3)))
    candidate_limit = top_k if not preferred else max(top_k, top_k * candidate_multiplier)
    if include_channel_memories is None:
        include_channel_memories = _cfg_bool(cfg, "MEMORY_INCLUDE_CHANNEL_MEMORIES_IN_REPLY", False)
    if include_channel_summary is None:
        include_channel_summary = _cfg_bool(cfg, "MEMORY_INCLUDE_CHANNEL_SUMMARY_IN_REPLY", False)
    include_global_semantic = _cfg_bool(cfg, "MEMORY_INCLUDE_GLOBAL_SEMANTIC_MEMORIES", True)
    use_hybrid = _cfg_bool(cfg, "MEMORY_USE_HYBRID_SEARCH", True)
    dense_weight = float(cfg("MEMORY_HYBRID_DENSE_WEIGHT", 1.0))
    sparse_weight = float(cfg("MEMORY_HYBRID_SPARSE_WEIGHT", 1.0))
    rrf_k = int(cfg("MEMORY_HYBRID_RRF_K", 60))

    user_memories_raw = await _safe_await(
        store.get_recent_user_memories(
            persona=persona,
            guild_id=guild_id,
            user_id=user_id,
            limit=candidate_limit,
            min_score=min_score,
        ),
        [],
    )
    user_memories = list(user_memories_raw or [])
    channel_memories: list[dict[str, Any]] = []
    if include_channel_memories:
        channel_memories_raw = await _safe_await(
            store.get_recent_channel_memories(
                persona=persona,
                guild_id=guild_id,
                channel_id=channel_id,
                limit=candidate_limit,
                min_score=min_score,
            ),
            [],
        )
        channel_memories = list(channel_memories_raw or [])

    semantic_limit = int(cfg("MEMORY_SEMANTIC_LIMIT", 5))
    semantic_threshold = float(cfg("MEMORY_SEMANTIC_THRESHOLD", 0.45))
    embed_model = _embed_model_from_cfg(cfg)
    semantic_memories: list[dict[str, Any]] = []
    helpers = get_ollama_helpers()
    if embed_model and user_text and user_text.strip():
        user_text_clean = re.sub(r'<@!?\d+>', '', user_text).strip()
        if user_text_clean:
            query_embedding = await helpers.call_ollama_embeddings(user_text_clean, model=embed_model)
            keyword_query = _build_fts_query(user_text_clean) if use_hybrid else None

            if query_embedding:
                used_search = False
                if use_hybrid and hasattr(store, "search_memories_hybrid") and getattr(store, "is_vec_enabled", False):
                    try:
                        hybrid_results = await store.search_memories_hybrid(
                            query_embedding,
                            keyword_query=keyword_query,
                            persona=persona,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=user_id,
                            include_global=include_global_semantic,
                            limit=semantic_limit,
                            threshold=semantic_threshold,
                            dense_weight=dense_weight,
                            sparse_weight=sparse_weight,
                            rrf_k=rrf_k,
                        )
                        semantic_memories = list(hybrid_results)
                        used_search = True
                    except Exception:
                        log.debug("search_memories_hybrid failed, falling back", exc_info=True)
                        used_search = False

                if not used_search and hasattr(store, "search_memories_by_vector") and getattr(store, "is_vec_enabled", False):
                    try:
                        native_results = await store.search_memories_by_vector(
                            query_embedding,
                            persona=persona,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=user_id,
                            include_global=include_global_semantic,
                            limit=semantic_limit,
                            threshold=semantic_threshold,
                        )
                        semantic_memories = list(native_results)
                        used_search = True
                    except Exception:
                        log.debug("search_memories_by_vector failed, falling back to python calculation", exc_info=True)
                        used_search = False

                if not used_search:
                    try:
                        all_embedded = await store.get_memories_with_embeddings(
                            persona=persona,
                            guild_id=guild_id,
                            channel_id=channel_id,
                            user_id=user_id,
                            include_global=include_global_semantic,
                        )

                        for row in all_embedded:
                            row_user_id = row.get("user_id")
                            row_mem_type = str(row.get("memory_type", "") or "")
                            is_current_user_memory = (
                                user_id is not None
                                and row_user_id is not None
                                and _same_id(row_user_id, user_id)
                            )
                            is_global_memory = row_user_id is None
                            is_shared_knowledge = (
                                include_global_semantic
                                and row_mem_type in ('fact', 'setting', 'theme', 'conversation_theme')
                            )
                            if not is_current_user_memory and not (include_global_semantic and is_global_memory) and not is_shared_knowledge:
                                continue
                            try:
                                vec = json_loads(row["embedding_json"])
                                sim = _cosine_similarity(query_embedding, vec)
                                row["_semantic_score"] = sim
                            except Exception:
                                row["_semantic_score"] = 0.0

                        all_embedded.sort(key=lambda x: float(x.get("_semantic_score", 0.0)), reverse=True)
                        for sm in all_embedded[:semantic_limit]:
                            if sm["_semantic_score"] > semantic_threshold:
                                sm["_semantic_match"] = True
                                semantic_memories.append(sm)
                    except Exception:
                        log.exception("get_relevant_memories: semantic search fallback failed")

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    preferred_rank = {tag: len(preferred) - idx for idx, tag in enumerate(preferred)}

    for item in user_memories + channel_memories + semantic_memories:
        content = str(item.get("content", "")).strip()
        if not content or content in seen:
            continue
        if _is_self_referential_memory(content) or _looks_like_noise_memory(content):
            continue
        seen.add(content)
        emotion_tags = _parse_emotion_tags(item.get("emotion_tags"))
        emotion_matches = [tag for tag in preferred if tag in emotion_tags]
        enriched = dict(item)
        if emotion_matches:
            enriched["_emotion_hook_match"] = emotion_matches
            enriched["_emotion_hook_score"] = float(sum(preferred_rank.get(tag, 0) for tag in emotion_matches))
        else:
            enriched["_emotion_hook_score"] = 0.0

        merged.append(enriched)

    merged.sort(
        key=lambda item: (
            float(item.get("_semantic_score", 0.0) or 0.0),
            float(item.get("_emotion_hook_score", 0.0) or 0.0),
            float(item.get("score", 0.0) or 0.0),
            str(item.get("updated_at", "") or ""),
        ),
        reverse=True,
    )
    merged = merged[:top_k]

    for item in merged:
        mid = item.get("id")
        if mid is not None:
            try:
                await _safe_await(store.touch_memory(int(mid)))
            except Exception:
                log.exception("touch_memory failed: id=%r", mid)

    summary = None
    if include_channel_summary and channel_id is not None:
        try:
            summary_row_raw = await _safe_await(
                store.get_channel_summary(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=int(channel_id),
                ),
                None,
            )
            summary_row: dict[str, Any] | None = summary_row_raw if isinstance(summary_row_raw, dict) else None
            if summary_row:
                summary = str(summary_row.get("summary", "")).strip() or None
        except Exception:
            log.exception("get_channel_summary failed")

    memory_lines = [_format_memory_line(item) for item in merged]
    memory_lines = [line for line in memory_lines if line]
    return memory_lines, summary
