from __future__ import annotations

from collections import deque
from typing import Any, Optional

import discord

from .discord_helpers import (
    ContextTurn,
    author_id,
    channel_id,
    content,
    context_item_created_at_ts,
    message_created_at_ts,
    to_context_turn,
)


def _turn_from_cached_item(item: dict[str, Any]) -> ContextTurn | None:
    line = str(item.get("line") or "").strip()
    if not line:
        return None
    turn: ContextTurn = {
        "id": int(item.get("id") or 0),
        "author_id": item.get("author_id"),
        "role": str(item.get("role") or "user"),
        "name": str(item.get("name") or "unknown"),
        "content": str(item.get("content") or ""),
        "line": line,
    }
    created_at_ts = context_item_created_at_ts(item)
    if created_at_ts is not None:
        turn["created_at_ts"] = created_at_ts
    return turn


def _is_recent_enough(
    *,
    item_ts: float | None,
    now_ts: float | None,
    max_age_sec: float | None,
) -> bool:
    if max_age_sec is None or max_age_sec <= 0 or now_ts is None or item_ts is None:
        return True
    return (now_ts - item_ts) <= max_age_sec


def _recent_turns_from_cache(
    message: discord.Message,
    *,
    limit: int,
    channel_cache: dict[int, deque[dict[str, Any]]] | None = None,
    message_cache: list[discord.Message] | None = None,
    bot_user_id: Optional[int] = None,
    max_age_sec: float | None = None,
    min_keep: int = 5,
) -> list[ContextTurn]:
    turns: list[ContextTurn] = []
    cid = channel_id(message)
    now_ts = message_created_at_ts(message)

    if channel_cache is not None and cid is not None:
        raw_items = list(channel_cache.get(cid, []))
        cached_items = [
            item for i, item in enumerate(raw_items)
            if _is_recent_enough(
                item_ts=context_item_created_at_ts(item),
                now_ts=now_ts,
                max_age_sec=max_age_sec,
            ) or (len(raw_items) - i) <= min_keep
        ][-limit:]
        turns = [t for item in cached_items if (t := _turn_from_cached_item(item))]
        if turns:
            return turns

    if message_cache is not None and cid is not None:
        history: list[discord.Message] = []
        for m in sorted(message_cache, key=lambda msg: getattr(msg, "id", 0)):
            if channel_id(m) != cid:
                continue
            if getattr(m, "id", 0) >= getattr(message, "id", 0):
                continue
            history.append(m)
            
        filtered_history: list[discord.Message] = []
        for i, m in enumerate(history):
            if _is_recent_enough(
                item_ts=message_created_at_ts(m),
                now_ts=now_ts,
                max_age_sec=max_age_sec,
            ) or (len(history) - i) <= min_keep:
                filtered_history.append(m)

        turns = [
            turn for m in filtered_history[-limit:]
            if (turn := to_context_turn(m, bot_user_id=bot_user_id))
        ]
        if turns:
            return turns

    return []


async def fetch_recent_context_lines(
    message: discord.Message,
    *,
    limit: int = 7,
    bot_user_id: Optional[int] = None,
    channel_cache: dict[int, deque[dict[str, Any]]] | None = None,
    message_cache: list[discord.Message] | None = None,
    max_age_sec: float | None = None,
) -> list[str]:
    turns = await fetch_recent_context_turns(
        message,
        limit=limit,
        bot_user_id=bot_user_id,
        channel_cache=channel_cache,
        message_cache=message_cache,
        max_age_sec=max_age_sec,
    )
    return [t["line"] for t in turns if t.get("line")]


async def fetch_recent_context_turns(
    message: discord.Message,
    *,
    limit: int = 7,
    bot_user_id: Optional[int] = None,
    channel_cache: dict[int, deque[dict[str, Any]]] | None = None,
    message_cache: list[discord.Message] | None = None,
    max_age_sec: float | None = None,
    min_keep: int = 5,
) -> list[ContextTurn]:
    cached_turns = _recent_turns_from_cache(
        message,
        limit=limit,
        channel_cache=channel_cache,
        message_cache=message_cache,
        bot_user_id=bot_user_id,
        max_age_sec=max_age_sec,
        min_keep=min_keep,
    )
    if cached_turns:
        return cached_turns

    turns: list[ContextTurn] = []
    history: list[discord.Message] = []
    now_ts = message_created_at_ts(message)
    async for m in message.channel.history(limit=limit, before=message, oldest_first=False):
        history.append(m)
    history.reverse()

    for i, m in enumerate(history):
        if not _is_recent_enough(
            item_ts=message_created_at_ts(m),
            now_ts=now_ts,
            max_age_sec=max_age_sec,
        ) and (len(history) - i) > min_keep:
            continue
        turn = to_context_turn(m, bot_user_id=bot_user_id)
        if turn:
            turns.append(turn)
    return turns


async def find_recent_bot_and_user_pair(
    message: discord.Message,
    *,
    bot_user_id: int,
    limit: int = 12,
) -> tuple[Optional[discord.Message], Optional[discord.Message]]:
    recent: list[discord.Message] = []
    async for m in message.channel.history(limit=limit, before=message, oldest_first=False):
        recent.append(m)

    bot_msg: Optional[discord.Message] = None
    prev_user_msg: Optional[discord.Message] = None
    for m in recent:
        if bot_msg is None and author_id(m) == bot_user_id:
            bot_msg = m
            continue
        if bot_msg is not None and not getattr(m.author, "bot", False):
            prev_user_msg = m
            break

    return bot_msg, prev_user_msg


def safe_message_content(message: Optional[discord.Message]) -> str:
    return "" if message is None else content(message)
