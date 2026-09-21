from __future__ import annotations

import re
from typing import Any

from .json_compat import loads as json_loads
from .memory_store import MemoryStore


_CONTINUATION_MARKERS = (
    "続き",
    "つづき",
    "さっき",
    "例の",
    "前の",
    "前回",
    "この前",
    "あの件",
    "その件",
    "前話してた",
    "前に言ってた",
)


def _normalize_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.translate(str.maketrans("", "", "、。,.!?！？:：;；「」『』()（）[]【】"))
    return normalized


def _extract_keywords(text: str) -> set[str]:
    raw = str(text or "").strip()
    if not raw:
        return set()
    tokens = re.findall(r"[A-Za-z0-9_+-]{2,}|[ァ-ヶヴー]{2,}|[\u3041-\u309f]{2,}|[\u4e00-\u9fff]{1,}", raw)
    return {token.lower() for token in tokens if len(token) >= 2}


def _load_keywords(raw: Any) -> list[str]:
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


def _matches_record(record: dict[str, Any], text: str) -> bool:
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return False

    cue_keywords = _load_keywords(record.get("cue_keywords_json"))
    for keyword in cue_keywords:
        normalized_keyword = _normalize_text(keyword)
        if normalized_keyword and normalized_keyword in normalized_text:
            return True

    topic_tag = str(record.get("cue_topic_tag", "") or "").strip()
    if topic_tag:
        normalized_tag = _normalize_text(topic_tag)
        if normalized_tag and normalized_tag in normalized_text:
            return True
        text_keywords = _extract_keywords(text)
        tag_keywords = _extract_keywords(topic_tag)
        if text_keywords and tag_keywords and text_keywords.intersection(tag_keywords):
            return True

    if any(marker in text for marker in _CONTINUATION_MARKERS):
        return bool(cue_keywords or topic_tag)

    return False


async def match_cues(
    *,
    store: MemoryStore,
    persona: str,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    text: str,
) -> list[dict[str, Any]]:
    await expire_old(store, persona=persona)
    records = await store.list_pending_prospective_memories(
        persona=persona,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user_id=user_id,
        limit=20,
    )
    matched = [record for record in records if _matches_record(record, text)]
    matched.sort(
        key=lambda record: (
            float(record.get("priority", 0.0) or 0.0),
            str(record.get("created_at", "") or ""),
        ),
        reverse=True,
    )
    return matched


async def mark_triggered(store: MemoryStore, record_id: int) -> None:
    await store.mark_prospective_triggered(int(record_id))


async def add_prospective(
    store: MemoryStore,
    *,
    persona: str,
    guild_id: int | None,
    channel_id: int | None,
    target_user_id: int | None,
    kind: str = "event_based",
    cue_keywords: list[str] | None = None,
    cue_topic_tag: str | None = None,
    intent: str,
    priority: float = 0.5,
    expires_at: str | None = None,
) -> int:
    return await store.add_prospective_memory(
        persona=persona,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user_id=target_user_id,
        kind=kind,
        cue_keywords=cue_keywords,
        cue_topic_tag=cue_topic_tag,
        intent=intent,
        priority=priority,
        expires_at=expires_at,
    )


async def expire_old(store: MemoryStore, persona: str) -> None:
    await store.expire_prospective_memories(persona=persona)


__all__ = [
    "match_cues",
    "mark_triggered",
    "add_prospective",
    "expire_old",
]
