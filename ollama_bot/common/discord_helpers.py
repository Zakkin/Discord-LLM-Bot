# ollama_bot/common/discord_helpers.py
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

import discord


from lib.discord_utils import (
    DISCORD_EPOCH_MS,
    _datetime_to_ts,
    _normalize_emoji_str,
    _snowflake_to_ts,
    author_id,
    channel_id,
    clean_user_input_text,
    content,
    datetime_to_ts,
    display_name,
    guild_id,
    message_created_at_ts,
    normalize_emoji_str,
    snowflake_to_ts,
    strip_discord_mentions,
)

from .config_helpers import cfg


class ContextTurn(TypedDict, total=False):
    id: int
    author_id: Optional[int]
    role: str
    name: str
    content: str
    line: str
    created_at_ts: float
    reactions: list[dict[str, Any]]


def get_japanese_weekday(dt: datetime) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]


def context_item_created_at_ts(item: dict[str, Any]) -> float | None:
    raw_ts = item.get("created_at_ts")
    try:
        if raw_ts is not None:
            return float(raw_ts)
    except Exception:
        pass
    return _datetime_to_ts(item.get("created_at")) or _snowflake_to_ts(item.get("id"))


def _context_item_is_recent(
    item: dict[str, Any],
    *,
    now_ts: float | None,
    max_age_sec: float | None,
) -> bool:
    if max_age_sec is None or max_age_sec <= 0 or now_ts is None:
        return True
    item_ts = context_item_created_at_ts(item)
    if item_ts is None:
        return True
    return (now_ts - item_ts) <= max_age_sec


def recent_context_items(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int | None,
    *,
    before_message: discord.Message | None = None,
    max_age_sec: float | None = None,
    min_keep: int = 5,
) -> list[dict[str, Any]]:
    if channel_id_value is None:
        return []
    q = cache.get(channel_id_value)
    if not q:
        return []
    now_ts = message_created_at_ts(before_message) if before_message is not None else None
    items = list(q)
    return [
        item for i, item in enumerate(items)
        if _context_item_is_recent(item, now_ts=now_ts, max_age_sec=max_age_sec) or (len(items) - i) <= min_keep
    ]


def prune_stale_context_cache(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int | None,
    *,
    before_message: discord.Message | None = None,
    max_age_sec: float | None = None,
    min_keep: int = 5,
) -> None:
    if channel_id_value is None:
        return
    q = cache.get(channel_id_value)
    if not q:
        return
    now_ts = message_created_at_ts(before_message) if before_message is not None else None
    items = list(q)
    kept = [
        item for i, item in enumerate(items)
        if _context_item_is_recent(item, now_ts=now_ts, max_age_sec=max_age_sec) or (len(items) - i) <= min_keep
    ]
    if len(kept) != len(q):
        q.clear()
        q.extend(kept)


def _is_matching_configured_emoji(emoji: Any, configured_value: Any) -> bool:
    if not emoji or not configured_value:
        return False
    cfg_raw = str(configured_value).strip()
    if not cfg_raw:
        return False
    if cfg_raw.isdigit():
        eid = getattr(emoji, "id", None)
        if eid is not None and str(eid) == cfg_raw:
            return True
        if str(getattr(emoji, "id", "")) == cfg_raw:
            return True
    if isinstance(emoji, str) and emoji.strip() == cfg_raw:
        return True
    if str(emoji).strip() == cfg_raw:
        return True
    return False


def _classify_reaction_emoji(emoji: Any) -> tuple[str, str]:
    good_cfg = cfg("GOOD_EMOJI", "👍")
    bad_cfg = cfg("BAD_EMOJI", "👎")
    if _is_matching_configured_emoji(emoji, good_cfg):
        return ("👍" if str(good_cfg) in ("👍", "") else _normalize_emoji_str(emoji)), "GOOD"
    if _is_matching_configured_emoji(emoji, bad_cfg):
        return ("👎" if str(bad_cfg) in ("👎", "") else _normalize_emoji_str(emoji)), "BAD"
    # Fallback for standard unicode thumbs up / down
    emoji_clean = _normalize_emoji_str(emoji)
    if emoji_clean in ("👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", ":+1:", ":thumbsup:"):
        return "👍", "GOOD"
    if emoji_clean in ("👎", "👎🏻", "👎🏼", "👎🏽", "👎🏾", "👎🏿", ":-1:", ":thumbsdown:"):
        return "👎", "BAD"
    return emoji_clean, "OTHER"


def format_message_reactions(
    message: discord.Message,
    bot_user_id: Optional[int] = None,
) -> tuple[str, list[dict[str, Any]]]:
    reactions_list: list[dict[str, Any]] = []
    raw_reactions = getattr(message, "reactions", None) or []
    if not raw_reactions:
        return "", []

    bot_reactions: list[str] = []
    user_reactions: list[str] = []

    for r in raw_reactions:
        try:
            emoji = getattr(r, "emoji", None)
            if emoji is None:
                continue
            count = getattr(r, "count", 1)
            is_me = getattr(r, "me", False)
            emoji_str, category = _classify_reaction_emoji(emoji)
            if not emoji_str:
                continue
            reactions_list.append({
                "emoji": emoji_str,
                "category": category,
                "is_bot": is_me,
                "count": count,
            })
            if is_me:
                bot_reactions.append(emoji_str)
            else:
                user_reactions.append(emoji_str if count <= 1 else f"{emoji_str}x{count}")
        except Exception:
            continue

    parts: list[str] = []
    if bot_reactions:
        parts.append(f"[Botリアクション: {' '.join(bot_reactions)}]")
    if user_reactions:
        parts.append(f"[リアクション: {' '.join(user_reactions)}]")

    suffix = (" " + " ".join(parts)) if parts else ""
    return suffix, reactions_list


def to_context_turn(
    message: discord.Message,
    bot_user_id: Optional[int] = None,
) -> Optional[ContextTurn]:
    text = content(message)
    if not text:
        return None

    aid = author_id(message)
    is_assistant = bot_user_id is not None and aid == bot_user_id
    role = "assistant" if is_assistant else "user"
    name = "Assistant" if is_assistant else display_name(message)
    reactions_suffix, reactions_data = format_message_reactions(message, bot_user_id=bot_user_id)
    line = f"{name}: {text}{reactions_suffix}"
    mid = getattr(message, "id", None)
    turn: ContextTurn = {
        "id": mid if isinstance(mid, int) else 0,
        "author_id": aid,
        "role": role,
        "name": name,
        "content": text,
        "line": line,
    }
    if reactions_data:
        turn["reactions"] = reactions_data
    created_at_ts = message_created_at_ts(message)
    if created_at_ts is not None:
        turn["created_at_ts"] = created_at_ts
    return turn


def update_context_message_reaction(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int | None,
    message_id: int | None,
    reaction_emoji: Any,
    *,
    is_bot: bool = True,
) -> bool:
    if channel_id_value is None or message_id is None:
        return False
    q = cache.get(channel_id_value)
    if not q:
        return False

    emoji_str, category = _classify_reaction_emoji(reaction_emoji)
    if not emoji_str:
        return False

    for item in q:
        if item.get("id") == message_id:
            reactions = item.setdefault("reactions", [])
            existing = next((r for r in reactions if r.get("emoji") == emoji_str and r.get("is_bot") == is_bot), None)
            if existing:
                existing["count"] = existing.get("count", 1) + 1
            else:
                reactions.append({
                    "emoji": emoji_str,
                    "category": category,
                    "is_bot": is_bot,
                    "count": 1,
                })
            name = item.get("name", "unknown")
            text = item.get("content", "")
            bot_reacts = [r["emoji"] for r in reactions if r.get("is_bot")]
            user_reacts = [
                (r["emoji"] if r.get("count", 1) <= 1 else f"{r['emoji']}x{r['count']}")
                for r in reactions if not r.get("is_bot")
            ]
            parts: list[str] = []
            if bot_reacts:
                parts.append(f"[Botリアクション: {' '.join(bot_reacts)}]")
            if user_reacts:
                parts.append(f"[リアクション: {' '.join(user_reacts)}]")
            suffix = (" " + " ".join(parts)) if parts else ""
            item["line"] = f"{name}: {text}{suffix}"
            return True
    return False


def to_context_line(
    message: discord.Message,
    bot_user_id: Optional[int] = None,
) -> Optional[str]:
    turn = to_context_turn(message, bot_user_id=bot_user_id)
    if not turn:
        return None
    return turn["line"]


def append_context_message(
    cache: dict[int, deque[Any]],
    message: discord.Message,
    bot_user_id: Optional[int] = None,
) -> None:
    cid = channel_id(message)
    mid = getattr(message, "id", None)
    turn = to_context_turn(message, bot_user_id=bot_user_id)
    if cid is None or mid is None or not turn:
        return

    q = cache[cid]
    if any(item.get("id") == mid for item in q):
        return
    q.append(turn)


def remove_context_message(
    cache: dict[int, deque[Any]],
    channel_id_value: int,
    message_id: Optional[int],
) -> None:
    if message_id is None:
        return
    q = cache.get(channel_id_value)
    if not q:
        return
    kept = [item for item in q if item.get("id") != message_id]
    q.clear()
    q.extend(kept)


def has_anon_buttons(message: discord.Message) -> bool:
    try:
        for row in (message.components or []):
            children = getattr(row, "children", None) or getattr(row, "components", None) or []
            if any(
                getattr(c, "custom_id", "") in {"anon_speak_btn", "anon_reply_btn", "anon_edit_btn"}
                for c in children
            ):
                return True
    except Exception:
        pass
    return False


async def resolve_reference_message(message: discord.Message) -> Optional[discord.Message]:
    ref = getattr(message, "reference", None)
    if not ref:
        return None

    ref_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
    if isinstance(ref_msg, discord.Message):
        return ref_msg

    try:
        mid = getattr(ref, "message_id", None)
        if mid:
            fetched = await message.channel.fetch_message(mid)
            if isinstance(fetched, discord.Message):
                return fetched
    except Exception:
        pass
    return None


async def collect_reply_chain(message: discord.Message, *, max_depth: int = 8) -> list[discord.Message]:
    chain: list[discord.Message] = []
    cur = message
    for _ in range(max_depth):
        ref_msg = await resolve_reference_message(cur)
        if not ref_msg:
            break
        chain.append(ref_msg)
        cur = ref_msg
    chain.reverse()
    return chain


def prune_reply_chain_from_cache(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int,
    chain: list[discord.Message],
) -> list[discord.Message]:
    if len(chain) < 4:
        return chain
    return chain[-1:]


def find_reply_parent_from_cache(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int | None,
    target_message_id: int | None,
    *,
    bot_user_id: int | None = None,
) -> dict[str, Any] | None:
    if channel_id_value is None or not cache.get(channel_id_value):
        return None
    q = cache[channel_id_value]
    target_idx = -1
    if target_message_id is not None:
        for idx, item in enumerate(q):
            if item.get("id") == target_message_id:
                target_idx = idx
                break
    if target_idx > 0:
        for i in range(target_idx - 1, -1, -1):
            cand = q[i]
            cand_aid = cand.get("author_id")
            if bot_user_id is not None and cand_aid == bot_user_id:
                continue
            if cand.get("role") == "assistant":
                continue
            return cand
    elif target_idx == -1:
        for i in range(len(q) - 1, -1, -1):
            cand = q[i]
            cand_aid = cand.get("author_id")
            if bot_user_id is not None and cand_aid == bot_user_id:
                continue
            if cand.get("role") == "assistant":
                continue
            return cand
    return None

