"""x.com 自発発話・定期巡回向けタイムライン取得・ローテーション・整形ヘルパー。"""
from __future__ import annotations

import contextlib
from collections import deque
from typing import Any

from harness.agent.tool_registry.web_research.dispatch import research_dispatch
from lib.config_utils import cfg_int
from lib.text_utils import normalize_compare_text as _normalize_compare_text
from ollama_bot.common.ollama_helpers import truncate_text


def _get_research_dispatch() -> Any:
    try:
        import ollama_bot.ollama_chat_.ollama_chat_helpers as helpers
        if hasattr(helpers, "research_dispatch"):
            return getattr(helpers, "research_dispatch")
    except Exception:
        pass
    return research_dispatch


async def fetch_recent_x_timeline_items(
    limit: int = 10,
    *,
    custom_dispatch: Any = None,
) -> list[dict[str, str]]:
    resolved_limit = max(int(limit or 10), 1)
    dispatch_fn = custom_dispatch or _get_research_dispatch()
    result = await dispatch_fn(
        f"今のXのタイムラインを{resolved_limit}件見て",
        mode="x_timeline",
        max_results=resolved_limit,
    )
    error = str(result.get("error") or "").strip()
    if error:
        raise RuntimeError(error)

    items: list[dict[str, str]] = []
    for source in list(result.get("sources") or []):
        if not isinstance(source, dict):
            continue
        text = str(source.get("snippet") or source.get("content_excerpt") or source.get("content_text") or "").strip()
        if not text:
            continue
        items.append({
            "author": str(source.get("author") or "").strip(),
            "handle": str(source.get("handle") or "").strip(),
            "text": text,
            "url": str(source.get("url") or "").strip(),
        })
        if len(items) >= resolved_limit:
            break
    if not items:
        raise RuntimeError(str(result.get("summary") or "Xタイムラインの投稿を抽出できませんでした。").strip())
    return items


def format_x_timeline_items_for_prompt(items: list[dict[str, str]], *, limit: int = 10) -> str:
    lines: list[str] = []
    seen_texts: set[str] = set()
    for item in list(items or [])[:max(int(limit or 10), 1)]:
        text = truncate_text(str(item.get("text") or "").strip().replace("\n", " "), 280)
        if not text or text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        lines.append(f"{len(lines) + 1}. 本文: {text}")
    return "\n".join(lines)


def rotate_x_timeline_items_for_spontaneous(
    owner: Any,
    items: list[dict[str, str]],
    *,
    limit: int = 10,
) -> list[dict[str, str]]:
    normalized_items = [dict(item) for item in list(items or []) if str(item.get("text") or "").strip()]
    max_items = max(int(limit or 10), 1)
    if not normalized_items:
        return []

    cache_size = max(cfg_int("SPONTANEOUS_X_RECENT_TEXT_CACHE_SIZE", 60), max_items)
    recent = getattr(owner, "_recent_spontaneous_x_texts", None)
    if not isinstance(recent, deque) or recent.maxlen != cache_size:
        recent = deque(list(recent or [])[-cache_size:], maxlen=cache_size)
        with contextlib.suppress(Exception):
            setattr(owner, "_recent_spontaneous_x_texts", recent)

    def _item_key(item: dict[str, str]) -> str:
        return _normalize_compare_text(str(item.get("text") or ""))

    recent_keys = {str(key) for key in recent if str(key)}
    unseen = [item for item in normalized_items if _item_key(item) and _item_key(item) not in recent_keys]

    if unseen:
        ordered = unseen + [item for item in normalized_items if item not in unseen]
    else:
        cursor = int(getattr(owner, "_spontaneous_x_timeline_cursor", 0) or 0)
        offset = cursor % len(normalized_items)
        ordered = normalized_items[offset:] + normalized_items[:offset]

    selected = ordered[:max_items]
    for item in selected:
        key = _item_key(item)
        if key:
            recent.append(key)
    with contextlib.suppress(Exception):
        setattr(owner, "_spontaneous_x_timeline_cursor", int(getattr(owner, "_spontaneous_x_timeline_cursor", 0) or 0) + 1)
    return selected


__all__ = [
    "fetch_recent_x_timeline_items",
    "format_x_timeline_items_for_prompt",
    "rotate_x_timeline_items_for_spontaneous",
]
