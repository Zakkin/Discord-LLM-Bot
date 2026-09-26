# ollama_bot/common/memory/logic.py
from __future__ import annotations

import re
import logging
from datetime import datetime, timezone
from typing import Any

from ..emotion_helpers import EMOTION_KEYS, EMOTION_LABEL_TO_JA, build_memory_emotion_tags, format_emotion_tags_ja
from ..json_compat import loads as json_loads
from .store import MemoryStore
from .. import ollama_helpers
from ..bot_identity import _extract_character_names_from_config


log = logging.getLogger("ollama_bot.common.memory.logic")

_ALLOWED_ASSOCIATED_EMOTIONS = {*(str(key) for key in EMOTION_KEYS), "neutral", "none"}
_ASSOCIATED_EMOTION_ALIASES = {
    **{str(label): key for key, label in EMOTION_LABEL_TO_JA.items()},
    "なし": "none",
    "無し": "none",
    "無": "none",
    "普通": "neutral",
    "平常": "neutral",
}

def _cosine_similarity(vec1: list[float], vec2: list[float] | None) -> float:
    if not vec1 or not vec2:
        return 0.0
    if len(vec1) != len(vec2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot_product / (norm1 * norm2)

def _embed_model_from_cfg(cfg: Any) -> str:
    return str(cfg("OLLAMA_EMBED_MODEL", ""))


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = cfg(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _same_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except Exception:
        return False


MEMORY_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_store": {"type": "boolean"},
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string"},
                    "content": {"type": "string"},
                    "associated_emotion": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["memory_type", "content", "associated_emotion", "score"],
                "additionalProperties": False,
            },
        },
        "training_candidate": {
            "type": "object",
            "properties": {
                "should_keep": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["should_keep", "reason"],
            "additionalProperties": False,
        },
    },
    "required": ["should_store", "memories", "training_candidate"],
    "additionalProperties": False,
}


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
    },
    "required": ["summary"],
    "additionalProperties": False,
}


HABIT_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_update": {"type": "boolean"},
        "tone_casual": {"type": "number"},
        "response_detail": {"type": "number"},
        "search_preference": {"type": "number"},
        "joke_receptivity": {"type": "number"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": [
        "should_update",
        "tone_casual",
        "response_detail",
        "search_preference",
        "joke_receptivity",
        "confidence",
        "reason",
    ],
    "additionalProperties": False,
}


ALLOWED_MEMORY_TYPES = {
    "user_preference",
    "preference",
    "fact",
    "constraint",
    "setting",
    "theme",
    "conversation_theme",
    "communication_style",
    "behavioral_principle",
    "location_preference",
    "recipe",
    "food",
}


BLOCKED_MEMORY_TYPES = {
    "user",
    "ai",
    "ai_response",
    "user_request",
    "persona",
    "personality",
    "guild_id",
    "channel_id",
    "user_id",
    "character",
    "character_name",
    "dialogue",
    "conversation",
    "context",
}


def _normalize_score(value: Any) -> float:
    try:
        score = float(value)
    except Exception:
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _profile_score(value: Any, *, default: float = 0.5) -> float:
    try:
        score = float(value)
    except Exception:
        return default
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _is_self_referential_memory(content: str) -> bool:
    """AI自身（設定されたBot呼称）のキャラクター設定・プロフィール・状態をユーザー記憶として保存・注入しないための判定。"""
    text = str(content or "").strip()
    if not text:
        return False

    # 1. 設定から動的にキャラクター名・呼称を取得して判定
    try:
        from ..bot_identity import _extract_character_names_from_config
        char_names = _extract_character_names_from_config()
        if char_names:
            escaped_names = "|".join(re.escape(n) for n in char_names if len(n) >= 2)
            if escaped_names:
                dynamic_pattern = rf"(?:^|[「『（(\s])(?:{escaped_names})(?:は|が|の|って|という|について|も)"
                if re.search(dynamic_pattern, text, flags=re.IGNORECASE):
                    return True
    except Exception:
        pass

    # 2. 汎用的なAI呼称判定（フォールバック）
    generic_subject_pattern = r"(?:^|[「『（(\s])(?:AI|ai|Bot|bot|ボット|アシスタント)(?:は|が|の|って|という|について|も)"
    if re.search(generic_subject_pattern, text, flags=re.IGNORECASE):
        return True

    return False


def _looks_like_noise_memory(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return True
    if len(text) < 8:
        return True
    if text.isdigit():
        return True
    if re.fullmatch(r"<@!?\d+>", text):
        return True
    if re.fullmatch(r"\d{15,25}", text):
        return True
    if _is_self_referential_memory(text):
        return True

    lowered = text.lower()
    blocked_fragments = [
        "guild_id",
        "channel_id",
        "user_id",
        "persona",
        "ai_response",
        "bot返答",
        "ユーザー発言",
        "高id:",
    ]
    if any(frag in lowered for frag in blocked_fragments):
        return True
    return False


def _is_assistant_quote_like(content: str, assistant_text: str, user_text: str = "") -> bool:
    c = str(content or "").strip()
    a = str(assistant_text or "").strip()
    if not c or not a:
        return False
    normalized_content = re.sub(r"\s+", " ", c)
    normalized_assistant = re.sub(r"\s+", " ", a)
    normalized_user = re.sub(r"\s+", " ", str(user_text or "").strip())
    if normalized_content == normalized_assistant:
        return True

    # ユーザー発言側にも含まれる言葉・知識であれば、アシスタントの共感・受容フレーズと被っていても破棄しない
    if normalized_user:
        if normalized_content in normalized_user:
            return False
        user_tokens = set(re.findall(r'[A-Za-z0-9_]{2,}|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]{2,}', normalized_user))
        content_tokens = set(re.findall(r'[A-Za-z0-9_]{2,}|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]{2,}', normalized_content))
        if user_tokens and (user_tokens & content_tokens):
            return False

    if len(normalized_content) < 24 and normalized_content in normalized_assistant:
        return True
    return bool(
        normalized_assistant in normalized_content
        and len(normalized_content) <= len(normalized_assistant) + 12
    )


def _is_single_turn_unsafe_or_low_value(content: str) -> bool:
    text = str(content or "").strip()
    return any(token in text for token in ("死んだ息子", "子作り", "子供できちゃった", "どっちが子供産む"))


def _is_fallback_reply(cfg: Any, text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    fallbacks = {
        str(cfg("OLLAMA_MODEL_TIMEOUT_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_REASONING_LEAK_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_PARROT_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_WEATHER_PLACE_REQUIRED_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_UNKNOWN_FACT_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_UNCLEAR_INTENT_FALLBACK", "") or "").strip(),
        str(cfg("OLLAMA_ROLE_FLIP_FALLBACK", "") or "").strip(),
    }
    return t in fallbacks and bool(t)


def _normalize_associated_emotion(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _ALLOWED_ASSOCIATED_EMOTIONS:
        return lowered
    return str(_ASSOCIATED_EMOTION_ALIASES.get(text, "") or "").strip().lower()


def _merge_memory_emotion_tags(turn_tags: list[str], associated_emotion: str) -> list[str]:
    merged: list[str] = []
    normalized_associated = _normalize_associated_emotion(associated_emotion)
    if normalized_associated and normalized_associated not in {"neutral", "none"}:
        merged.append(normalized_associated)
    for tag in turn_tags:
        normalized_tag = _normalize_associated_emotion(tag) or str(tag).strip().lower()
        if not normalized_tag or normalized_tag in {"neutral", "none"}:
            continue
        if normalized_tag not in merged:
            merged.append(normalized_tag)
    return merged


def _join_context_lines(
    context_lines: list[str],
    limit: int = 12,
    *,
    max_chars_per_line: int = 200,
    max_total_chars: int = 1200,
) -> str:
    if not context_lines:
        return ""
    trimmed = ollama_helpers.truncate_lines(
        context_lines,
        max_lines=limit,
        max_chars_per_line=max_chars_per_line,
        max_total_chars=max_total_chars,
    )
    return "\n".join(trimmed)


def _persona_from_cfg(cfg: Any) -> str:
    return str(cfg("MEMORY_PERSONA_NAMESPACE", "default"))


def _parse_emotion_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json_loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except Exception:
        pass
    return []


def _relative_time_label(iso_time_str: str) -> str:
    text = str(iso_time_str or "").strip()
    if not text:
        return "過去"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = now - dt
        seconds = max(diff.total_seconds(), 0.0)

        if seconds < 3600:
            return "ついさっき"
        if seconds < 86400:
            return "数時間前"
        if seconds < 86400 * 7:
            return "数日前"
        if seconds < 86400 * 30:
            return "少し前"
        if seconds < 86400 * 180:
            return "数ヶ月前"
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
    cfg,
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

    user_memories = await store.get_recent_user_memories(
        persona=persona,
        guild_id=guild_id,
        user_id=user_id,
        limit=candidate_limit,
        min_score=min_score,
    )
    channel_memories: list[dict[str, Any]] = []
    if include_channel_memories:
        channel_memories = await store.get_recent_channel_memories(
            persona=persona,
            guild_id=guild_id,
            channel_id=channel_id,
            limit=candidate_limit,
            min_score=min_score,
        )
    
    semantic_limit = int(cfg("MEMORY_SEMANTIC_LIMIT", 5))
    semantic_threshold = float(cfg("MEMORY_SEMANTIC_THRESHOLD", 0.45))
    embed_model = _embed_model_from_cfg(cfg)
    semantic_memories = []
    if embed_model and user_text and user_text.strip():
        user_text_clean = re.sub(r'<@!?\d+>', '', user_text).strip()
        if user_text_clean:
            query_embedding = await ollama_helpers.call_ollama_embeddings(user_text_clean, model=embed_model)
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
                await store.touch_memory(int(mid))
            except Exception:
                log.exception("touch_memory failed: id=%r", mid)

    summary = None
    if include_channel_summary and channel_id is not None:
        try:
            summary_row: dict[str, Any] | None = await store.get_channel_summary(
                persona=persona,
                guild_id=guild_id,
                channel_id=int(channel_id),
            )
            if summary_row:
                summary = str(summary_row.get("summary", "")).strip() or None
        except Exception:
            log.exception("get_channel_summary failed")

    memory_lines = [_format_memory_line(item) for item in merged]
    memory_lines = [line for line in memory_lines if line]
    return memory_lines, summary


def _habit_profile_model_name(cfg: Any) -> str:
    return str(
        cfg(
            "HABIT_PROFILE_MODEL",
            cfg(
                "OLLAMA_MIDDLE_MODEL",
                cfg("OLLAMA_CLASSIFIER_MODEL", cfg("MEMORY_EXTRACT_MODEL", cfg("OLLAMA_MODEL", ""))),
            ),
        )
        or ""
    )


async def update_user_habit_profile(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    candidate_reason: str = "auto-saved",
) -> dict[str, Any] | None:
    if not cfg("HABIT_PROFILE_ENABLED", True):
        return None

    if user_id is None:
        return None

    user_text = str(user_text or "").strip()
    assistant_text = str(assistant_text or "").strip()
    if not user_text or not assistant_text:
        return None

    if ollama_helpers.looks_like_abnormal_assistant_reply(assistant_text) or _is_fallback_reply(cfg, assistant_text):
        return None

    persona = _persona_from_cfg(cfg)
    model = _habit_profile_model_name(cfg)
    timeout_sec = float(cfg("HABIT_PROFILE_TIMEOUT_SEC", cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0)) or 60.0)
    retries = int(cfg("HABIT_PROFILE_RETRIES", 1) or 1)
    temperature = float(cfg("HABIT_PROFILE_TEMPERATURE", 0.1) or 0.1)
    min_confidence = _profile_score(cfg("HABIT_PROFILE_MIN_CONFIDENCE", 0.35), default=0.35)
    min_delta = max(0.0, min(0.49, float(cfg("HABIT_PROFILE_SIGNAL_MIN_DELTA", 0.08) or 0.08)))
    base_alpha = max(0.0, min(1.0, float(cfg("HABIT_PROFILE_EMA_ALPHA", 0.12) or 0.12)))

    context_text = _join_context_lines(
        context_lines,
        limit=int(cfg("HABIT_PROFILE_CONTEXT_LINES", 16) or 16),
        max_chars_per_line=int(cfg("HABIT_PROFILE_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("HABIT_PROFILE_CONTEXT_MAX_TOTAL_CHARS", 3200) or 3200),
    )
    system_prompt = (
        "あなたはDiscord会話から、相手ごとの会話習慣プロファイルを数値化する補助AIです。"
        "出力はJSONのみです。"
        "各スコアは0.0から1.0で、0.5は今回の会話からは判断しない中立値です。"
        "tone_casual は砕けた口調を好むほど高く、丁寧で落ち着いた口調を好むほど低くします。"
        "response_detail は説明を詳しくしてほしいほど高く、短く結論だけを好むほど低くします。"
        "search_preference は不明点や新しい話題で外部確認を望むほど高く、雑談テンポ優先ほど低くします。"
        "joke_receptivity は軽い冗談やツッコミを受け入れやすいほど高く、真面目な返答を好むほど低くします。"
        "明示的な依頼・反応・継続的な文脈から読める時だけ0.5から動かし、単なるbot側の口調をユーザーの好みと誤認しないでください。"
    )
    prompt = (
        f"[候補理由]\n{candidate_reason or 'auto-saved'}\n\n"
        f"[直近文脈]\n{context_text or '(なし)'}\n\n"
        f"[ユーザー発言]\n{ollama_helpers.truncate_text(user_text, 600)}\n\n"
        f"[AI返答]\n{ollama_helpers.truncate_text(assistant_text, 900)}\n\n"
        "この1ターンから、今後の返答に戻せる会話習慣があるか判定してください。\n"
        "判断できない項目は必ず0.5にしてください。"
    )

    try:
        result = await ollama_helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=HABIT_PROFILE_SCHEMA,
            model=model,
            temperature=temperature,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        err_name = type(e).__name__
        if (
            "Timeout" in err_name
            or "Client" in err_name
            or "ServerDisconnected" in err_name
            or "OSError" in err_name
            or "OllamaJSON" in err_name
            or "JSONDecode" in err_name
        ):
            log.warning("update_user_habit_profile: call_ollama_json failed: %r", e)
        else:
            log.exception("update_user_habit_profile: call_ollama_json failed")
        return None

    if not isinstance(result, dict) or not bool(result.get("should_update")):
        return None

    confidence = _profile_score(result.get("confidence", 0.0), default=0.0)
    if confidence < min_confidence:
        return None

    signals = {
        "tone_casual": _profile_score(result.get("tone_casual"), default=0.5),
        "response_detail": _profile_score(result.get("response_detail"), default=0.5),
        "search_preference": _profile_score(result.get("search_preference"), default=0.5),
        "joke_receptivity": _profile_score(result.get("joke_receptivity"), default=0.5),
    }
    updates: dict[str, float | None] = {
        key: value if abs(value - 0.5) >= min_delta else None
        for key, value in signals.items()
    }
    if not any(value is not None for value in updates.values()):
        return None

    method = getattr(store, "upsert_user_habit_profile", None)
    if not callable(method):
        log.debug("update_user_habit_profile: store has no upsert_user_habit_profile")
        return None

    try:
        return await method(
            persona=persona,
            guild_id=guild_id,
            user_id=user_id,
            tone_casual=updates["tone_casual"],
            response_detail=updates["response_detail"],
            search_preference=updates["search_preference"],
            joke_receptivity=updates["joke_receptivity"],
            alpha=base_alpha * max(confidence, 0.25),
            source_summary=str(result.get("reason") or "").strip(),
            confidence=confidence,
        )
    except Exception:
        log.exception(
            "update_user_habit_profile: profile upsert failed persona=%r guild=%r channel=%r user=%r",
            persona,
            guild_id,
            channel_id,
            user_id,
        )
        return None


async def get_user_habit_profile(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    user_id: int | None,
) -> dict[str, Any] | None:
    if not cfg("HABIT_PROFILE_ENABLED", True) or user_id is None:
        return None
    method = getattr(store, "get_user_habit_profile", None)
    if not callable(method):
        return None
    try:
        return await method(
            persona=_persona_from_cfg(cfg),
            guild_id=guild_id,
            user_id=user_id,
        )
    except Exception:
        log.exception("get_user_habit_profile failed user_id=%r", user_id)
        return None


def format_user_habit_profile_lines(
    profile: dict[str, Any] | None,
    *,
    min_evidence: int = 1,
    min_delta: float = 0.10,
) -> list[str]:
    if not profile:
        return []

    try:
        evidence_count = int(profile.get("evidence_count", 0) or 0)
    except Exception:
        evidence_count = 0
    if evidence_count < max(int(min_evidence), 1):
        return []

    threshold = max(0.0, min(0.49, float(min_delta)))
    tone = _profile_score(profile.get("tone_casual"), default=0.5)
    detail = _profile_score(profile.get("response_detail"), default=0.5)
    search = _profile_score(profile.get("search_preference"), default=0.5)
    joke = _profile_score(profile.get("joke_receptivity"), default=0.5)

    lines: list[str] = []
    if tone >= 0.5 + threshold:
        lines.append("口調: 砕けた自然な口調を受け入れやすい。")
    elif tone <= 0.5 - threshold:
        lines.append("口調: 落ち着いた丁寧寄りの口調が合いやすい。")

    if detail >= 0.5 + threshold:
        lines.append("説明量: 結論だけでなく理由や手順も少し足すとよい。")
    elif detail <= 0.5 - threshold:
        lines.append("説明量: 短めに、結論や反応を先に置く方が合いやすい。")

    if search >= 0.5 + threshold:
        lines.append("検索: 不明点や新しい話題では外部確認を好む傾向がある。")
    elif search <= 0.5 - threshold:
        lines.append("検索: 雑談では検索よりテンポを優先する方が合いやすい。")

    if joke >= 0.5 + threshold:
        lines.append("冗談: 軽い冗談やツッコミを返してもよい。")
    elif joke <= 0.5 - threshold:
        lines.append("冗談: からかいより素直で穏やかな返答を優先する。")

    return lines[:4]


async def get_user_habit_profile_lines(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    user_id: int | None,
) -> list[str]:
    profile = await get_user_habit_profile(
        cfg=cfg,
        store=store,
        guild_id=guild_id,
        user_id=user_id,
    )
    return format_user_habit_profile_lines(
        profile,
        min_evidence=int(cfg("HABIT_PROFILE_PROMPT_MIN_EVIDENCE", 1) or 1),
        min_delta=float(cfg("HABIT_PROFILE_PROMPT_MIN_DELTA", 0.10) or 0.10),
    )


async def extract_and_store_memory(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    emotion_scores: dict[str, float] | None = None,
    emotion_reason: str | None = None,
) -> dict[str, Any] | None:
    if not cfg("MEMORY_ENABLED", True):
        return None

    if ollama_helpers.looks_like_abnormal_assistant_reply(assistant_text) or _is_fallback_reply(cfg, assistant_text):
        return None

    persona = _persona_from_cfg(cfg)
    model = str(cfg("MEMORY_EXTRACT_MODEL", cfg("OLLAMA_MODEL", "")))
    timeout_sec = float(cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0))
    retries = int(cfg("MEMORY_EXTRACT_RETRIES", 2))
    temperature = float(cfg("MEMORY_EXTRACT_TEMPERATURE", 0.1))

    context_text = _join_context_lines(
        context_lines,
        limit=int(cfg("MEMORY_EXTRACT_CONTEXT_LINES", cfg("MEMORY_LONG_CONTEXT_LINES", 24)) or 24),
        max_chars_per_line=int(cfg("MEMORY_EXTRACT_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("MEMORY_EXTRACT_CONTEXT_MAX_TOTAL_CHARS", cfg("MEMORY_LONG_CONTEXT_MAX_TOTAL_CHARS", 5000)) or 5000),
    )
    emotion_tags = build_memory_emotion_tags(emotion_scores, max_tags=2, min_score=0.34)
    emotion_text = format_emotion_tags_ja(emotion_tags) or "(なし)"

    char_names = _extract_character_names_from_config()
    primary_char = char_names[0] if char_names else str(cfg("BOT_FIRST_PERSON", "") or "Bot")

    system_prompt = (
        "あなたは会話から長期記憶候補を抽出する補助AIです。"
        "保存対象は、ユーザーの好みや明示された制約だけでなく、今日話した日常的な話題、気になったこと、盛り上がった何気ない会話の内容も含みます。"
        "『今後また話題に出せそうなこと』であれば積極的に抽出してください。"
        "AI返答に外部調査やURL読解で得た事実・結論・注意点が含まれる場合は、今後も役立つ要点として保存候補にしてください。"
        "ただし保存するのは返答文の丸写しではなく、事実や前提を短く要約した内容だけです。"
        "【主客・文脈判定の重要ルール】"
        "1. クイズ、当てっこ、連想ゲーム、仮定の話、単なる推測や一問一答の回答を、ユーザー自身の恒久的な好みや属性（food, user_preference等）として誤認して保存してはいけません。"
        "（例: AIの「私の好きなスイーツを当ててみて」に対してユーザーが「チョコミント」と答えた場合、それはクイズの回答であってユーザーの好物ではないため誤認保存しない）"
        f"2. AI自身（{primary_char}、アシスタント、Bot）のキャラクター設定・プロフィール・発言（例: 『{primary_char}は〜である』『AIは〜』等）を、ユーザーの記憶として抽出・保存してはいけません。"
        "3. ユーザーが教えてくれた知識、作品、登場人物・キャラクター、用語、ネットミーム、設定（例: 『ワス先輩』『〇〇という技・効能』等）は、AI自身のプロフィールではないため、有益な知識・事実（fact または setting）として積極的に保存してください。"
        "4. 【behavioral_principle の厳禁事項】behavioral_principleはユーザーが持つ行動パターンや要望の傾向（例: 『ユーザーは詳しい説明より手短な回答を好む』）に限定されます。"
        "『ユーザーの発言が短い場合は焦りや戸惑いを示す反応を返す』『発言が〇〇の場合AIは〜する』など、AIの返答ポリシー・行動ルール・対応方針をbehavioral_principleとして保存してはなりません。"
        "behavioral_principleはあくまで『このユーザーと対話する際に意識すべきユーザー側の傾向・要望』であり、AIの内部処理や返し方のルールではありません。"
        "保存してはいけないもの: 数字ID、チャンネル情報、人格名、ロール名、意味のない相槌単体、罵倒、下ネタ、botの口調そのもの、AI自身の属性、AIの返答スタイル・返答ポリシー・行動ルール。"
        "memory_type は許可された種類のみを使ってください。"
        "memories の各要素には associated_emotion を必ず入れてください。"
        "値は joy, anticipation, anger, disgust, sadness, surprise, fear, neutral, none のいずれかです。"
        "会話全体ではなく、その記憶単体にもっとも結びつく感情を選び、特にないなら none にしてください。"
        "score は 0.0 から 1.0 の範囲にしてください。些細な日常会話でも 0.5 以上のスコアをつけてかまいません。"
        "出力は必ずJSONのみで返してください。"
    )

    prompt = (
        f"[長期記憶として残してよい種類]\n"
        f"{', '.join(sorted(ALLOWED_MEMORY_TYPES))}\n\n"
        f"[保存してはいけない種類]\n"
        f"{', '.join(sorted(BLOCKED_MEMORY_TYPES))}\n\n"
        f"[その時の感情]\n{emotion_text}\n\n"
        f"[感情の理由]\n{ollama_helpers.truncate_text(emotion_reason or '', 120) or '(なし)'}\n\n"
        f"[associated_emotion のルール]\n"
        f"- 各 memory ごとに、その記憶が結びつく感情を 1 つだけ入れる\n"
        f"- 値は joy / anticipation / anger / disgust / sadness / surprise / fear / neutral / none のどれか\n"
        f"- 迷ったら、その記憶を思い出した時に一番再燃しやすい感情を選ぶ\n\n"
        f"[直近文脈]\n{context_text or '(なし)'}\n\n"
        f"[ユーザー発言]\n{ollama_helpers.truncate_text(user_text, 500)}\n\n"
        f"[AI返答]\n{ollama_helpers.truncate_text(assistant_text, 900)}\n\n"
        f"[AI返答から保存してよい内容]\n"
        f"- 外部調査・URL読解・Xタイムライン確認で得た事実、結論、注意点\n"
        f"- ユーザーが後で参照しそうな説明済みの話題や共通前提\n"
        f"- 返答の言い回しやキャラクター口調そのものは保存しない\n\n"
        f"[主客判定の注意]\n"
        f"- クイズ・当てっこの回答や単なる推測を、ユーザー自身の嗜好・好物と誤認して保存しないでください。\n"
        f"- AI自身（{primary_char}、Bot）の設定・プロフィール・年齢・職業をユーザーの記憶として保存しないでください。\n"
        f"- ユーザーが教えてくれた知識、登場人物、用語、設定（『ワス先輩』『〇〇の効果』等）はAI自身の属性ではないため、fact や setting として積極的に保存してください。\n"
    )

    try:
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            log.info(
                "extract_and_store_memory: model=%s timeout_sec=%s retries=%s prompt=%r",
                model,
                timeout_sec,
                retries,
                ollama_helpers.truncate_text(prompt, int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
            )
        result = await ollama_helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=MEMORY_EXTRACTION_SCHEMA,
            model=model,
            temperature=temperature,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        err_name = type(e).__name__
        is_ignorable_err = (
            "Timeout" in err_name
            or "Client" in err_name
            or "ServerDisconnected" in err_name
            or "OSError" in err_name
            or "OllamaJSON" in err_name
            or "JSONDecode" in err_name
        )
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            if is_ignorable_err:
                log.warning(
                    "extract_and_store_memory: call_ollama_json failed model=%s err=%r prompt=%r",
                    model,
                    e,
                    ollama_helpers.truncate_text(prompt, int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
                )
            else:
                log.exception(
                    "extract_and_store_memory: call_ollama_json failed model=%s prompt=%r",
                    model,
                    ollama_helpers.truncate_text(prompt, int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
                )
        else:
            if is_ignorable_err:
                log.warning("extract_and_store_memory: call_ollama_json failed: %r", e)
            else:
                log.exception("extract_and_store_memory: call_ollama_json failed")
        return None

    if not isinstance(result, dict):
        return None

    if bool(result.get("should_store", False)):
        for item in result.get("memories", []) or []:
            try:
                memory_type = str(item.get("memory_type", "")).strip()
                content = str(item.get("content", "")).strip()
                associated_emotion = _normalize_associated_emotion(item.get("associated_emotion"))
                score = _normalize_score(item.get("score", 0.0))
                if not content:
                    continue
                if not memory_type:
                    continue
                if memory_type in BLOCKED_MEMORY_TYPES:
                    continue
                if memory_type not in ALLOWED_MEMORY_TYPES:
                    continue
                if _looks_like_noise_memory(content):
                    continue
                if _is_assistant_quote_like(content, assistant_text, user_text=user_text):
                    continue
                if _is_single_turn_unsafe_or_low_value(content):
                    continue
                
                embed_model = _embed_model_from_cfg(cfg)
                mem_embedding = None
                if embed_model:
                    mem_embedding = await ollama_helpers.call_ollama_embeddings(content, model=embed_model)

                await store.add_memory(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    memory_type=memory_type,
                    content=content,
                    score=score,
                    emotion_tags=_merge_memory_emotion_tags(emotion_tags, associated_emotion),
                    embedding=mem_embedding,
                )
            except Exception:
                log.exception("extract_and_store_memory: add_memory failed item=%r", item)

    return result


async def maybe_store_training_candidate(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    looks_like_reasoning_leak,
    looks_like_parrot_reply,
    is_weather_reply: bool = False,
) -> bool:
    if not cfg("TRAINING_CANDIDATE_ENABLED", True):
        return False

    if not user_text or not assistant_text:
        return False

    if len(assistant_text.strip()) < 6:
        return False

    if is_weather_reply:
        return False

    if ollama_helpers.looks_like_abnormal_assistant_reply(assistant_text) or _is_fallback_reply(cfg, assistant_text):
        return False

    try:
        if looks_like_reasoning_leak(assistant_text):
            return False
    except Exception:
        log.exception("maybe_store_training_candidate: reasoning leak check failed")

    try:
        if looks_like_parrot_reply(user_text, assistant_text):
            return False
    except Exception:
        log.exception("maybe_store_training_candidate: parrot check failed")

    persona = _persona_from_cfg(cfg)
    context_text = _join_context_lines(context_lines, limit=12)

    await store.add_training_candidate(
        persona=persona,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        user_text=user_text.strip(),
        assistant_text=assistant_text.strip(),
        context_text=context_text,
        reason="auto-saved",
    )
    return True


async def maybe_update_channel_summary(
    *,
    cfg,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    context_lines: list[str],
) -> bool:
    if not cfg("MEMORY_ENABLED", True):
        return False

    trigger = int(cfg("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20))
    if channel_id is None:
        return False
    if len(context_lines) < trigger:
        return False

    persona = _persona_from_cfg(cfg)
    model = str(cfg("MEMORY_EXTRACT_MODEL", cfg("OLLAMA_MODEL", "")))
    max_lines = int(cfg("MEMORY_SUMMARY_MAX_LINES", 5))
    timeout_sec = float(cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0))
    retries = int(cfg("MEMORY_EXTRACT_RETRIES", 2))
    temperature = float(cfg("MEMORY_EXTRACT_TEMPERATURE", 0.1))

    system_prompt = (
        "あなたはDiscord会話の内部要約を作る補助AIです。"
        "継続中の話題、重要前提、参加者の意図だけを短く残してください。"
        "雑談のノイズはできるだけ省いてください。"
        "出力はJSONのみで返してください。"
    )

    cleaned_lines = [
        line for line in context_lines
        if not (
            ollama_helpers.looks_like_abnormal_assistant_reply(line.split(": ", 1)[1] if ": " in line else line)
            or _is_fallback_reply(cfg, line.split(": ", 1)[1] if ": " in line else line)
        )
    ]
    summary_context_limit = max(
        int(cfg("MEMORY_SUMMARY_CONTEXT_LINES", cfg("MEMORY_LONG_CONTEXT_LINES", max(trigger, 20))) or max(trigger, 20)),
        trigger,
    )
    lines = ollama_helpers.truncate_lines(
        cleaned_lines[-summary_context_limit:],
        max_lines=summary_context_limit,
        max_chars_per_line=int(cfg("MEMORY_SUMMARY_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("MEMORY_SUMMARY_CONTEXT_MAX_TOTAL_CHARS", cfg("MEMORY_LONG_CONTEXT_MAX_TOTAL_CHARS", 5000)) or 5000),
    )
    if not lines:
        return False
    prompt = f"次の会話を内部メモとして最大{max_lines}行程度で要約してください\n\n[会話]\n" + "\n".join(lines)

    try:
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            log.info(
                "maybe_update_channel_summary: model=%s timeout_sec=%s retries=%s context=%r",
                model,
                timeout_sec,
                retries,
                ollama_helpers.truncate_text("\n".join(lines), int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
            )
        result = await ollama_helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=SUMMARY_SCHEMA,
            model=model,
            num_predict=int(cfg("MEMORY_SUMMARY_NUM_PREDICT", 512) or 512),
            temperature=temperature,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        err_name = type(e).__name__
        if (
            "Timeout" in err_name
            or "Client" in err_name
            or "ServerDisconnected" in err_name
            or "OSError" in err_name
            or "OllamaJSON" in err_name
            or "JSONDecode" in err_name
        ):
            log.warning("maybe_update_channel_summary: call_ollama_json failed: %r", e)
        else:
            log.exception("maybe_update_channel_summary: call_ollama_json failed")
        return False

    summary = str((result or {}).get("summary", "")).strip()
    if not summary:
        return False

    await store.set_channel_summary(
        persona=persona,
        guild_id=guild_id,
        channel_id=int(channel_id),
        summary=summary[:1000],
        source_count=len(lines),
    )
    return True
