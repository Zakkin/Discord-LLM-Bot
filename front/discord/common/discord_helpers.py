# ollama_bot/common/discord_helpers.py
from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, TypedDict

import discord


from front.discord.utils import DISCORD_EPOCH_MS, _datetime_to_ts, _normalize_emoji_str, _snowflake_to_ts, author_id, channel_id, clean_user_input_text, content, datetime_to_ts, display_name, guild_id, message_created_at_ts, normalize_emoji_str, snowflake_to_ts, strip_discord_mentions

from ollama_bot.common.config_helpers import cfg


class ContextTurn(TypedDict, total=False):
    id: int
    author_id: Optional[int]
    role: str
    name: str
    content: str
    line: str
    created_at_ts: float
    reactions: list[dict[str, Any]]
    reference_id: Optional[int]
    reply_target_name: Optional[str]


_JST = timezone(timedelta(hours=9))


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


def context_boundary_of(message: Any) -> tuple[int | None, float | None]:
    """返信対象メッセージから「未来メッセージ混入防止」用の上限境界 (ID, 作成時刻) を取得する。

    返信対象より後に届いたメッセージ（短文連投の後続など）をプロンプトに混入させないための基準値。
    ID が正の整数でない場合（識別不能なモック等）は ID 境界を None とし、時刻境界のみで判定する。
    """
    if message is None:
        return None, None
    raw_id = getattr(message, "id", None)
    boundary_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0 else None
    return boundary_id, message_created_at_ts(message)


def is_context_item_after_boundary(
    item: Mapping[str, Any],
    *,
    boundary_id: int | None,
    boundary_ts: float | None,
    exclude_boundary_message: bool = True,
) -> bool:
    """キャッシュ項目／ターンが返信対象メッセージ以降（未来）の発言であれば True を返す。

    - ``exclude_boundary_message=True``: 返信対象メッセージ自身（同一ID）も除外対象とする（ID ``>=`` 判定）。
    - ``exclude_boundary_message=False``: 返信対象メッセージ自身は残し、それより後のみ除外する（ID ``>`` 判定）。
    ID 比較とタイムスタンプ比較を組み合わせ、多重防御で未来メッセージを判定する。
    """
    raw_id = item.get("id")
    item_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0 else None
    item_ts = context_item_created_at_ts(dict(item))

    # 1. 同一メッセージ（ID一致）の場合
    if boundary_id is not None and item_id is not None and item_id == boundary_id:
        return exclude_boundary_message

    # 2. タイムスタンプによる未来判定（両方の時刻が取得できる場合）
    if boundary_ts is not None and item_ts is not None:
        if item_ts > boundary_ts:
            return True
        if item_ts < boundary_ts:
            # タイムスタンプが明確に過去の場合:
            # 本物の Discord Snowflake (>= 1 << 22) 同士でかつ item_id > boundary_id であれば未来。
            # モック等の小さなIDの場合は時刻の前後関係を最優先し、過去と判定する。
            if boundary_id is not None and item_id is not None:
                is_real_snowflake = (item_id >= (1 << 22)) and (boundary_id >= (1 << 22))
                if is_real_snowflake and item_id > boundary_id:
                    return True
            return False

    # 3. タイムスタンプが取得できない、あるいは同時刻（item_ts == boundary_ts）の場合
    if boundary_id is not None and item_id is not None:
        if item_id > boundary_id:
            return True

    return False


def filter_context_items_before(
    items: Iterable[dict[str, Any]],
    before_message: Any,
    *,
    exclude_boundary_message: bool = True,
) -> list[dict[str, Any]]:
    """返信対象メッセージ（``before_message``）より未来の項目を除外したリストを返す。"""
    item_list = list(items)
    if before_message is None:
        return item_list
    boundary_id, boundary_ts = context_boundary_of(before_message)
    if boundary_id is None and boundary_ts is None:
        return item_list
    return [
        item for item in item_list
        if not is_context_item_after_boundary(
            item,
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            exclude_boundary_message=exclude_boundary_message,
        )
    ]


def _context_item_is_recent(
    item: dict[str, Any],
    *,
    now_ts: float | None,
    max_age_sec: float | None,
    reject_future: bool = True,
) -> bool:
    item_ts = context_item_created_at_ts(item)
    # 未来タイムスタンプ（返信対象より後に届いた発言）は「直近」ではなく除外対象として扱う。
    # 差分が負になって常に True を返していた不具合のガード。
    if reject_future and now_ts is not None and item_ts is not None and item_ts > now_ts:
        return False
    if max_age_sec is None or max_age_sec <= 0 or now_ts is None:
        return True
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
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    if channel_id_value is None:
        return []
    q = cache.get(channel_id_value)
    if not q:
        return []
    now = now_ts
    if now is None and before_message is not None:
        now = message_created_at_ts(before_message)
    if now is None:
        now = time.time()
    # 先に未来メッセージ（返信対象自身を含む ID >= 対象ID / 時刻 > 対象時刻）を除外してから
    # 鮮度判定と min_keep を適用する（min_keep が未来メッセージを救済しないようにする）。
    items = filter_context_items_before(q, before_message, exclude_boundary_message=True)

    cutoff = (now - max(1.0, float(max_age_sec))) if (max_age_sec is not None and max_age_sec > 0) else None

    def _get_ts(it: dict[str, Any]) -> float | None:
        if "ts" in it and it["ts"] is not None:
            try:
                return float(it["ts"])
            except Exception:
                pass
        return context_item_created_at_ts(it)

    def _is_recent(it: dict[str, Any]) -> bool:
        if cutoff is None:
            return True
        t = _get_ts(it)
        if t is None:
            return True
        return t >= cutoff

    fresh = [item for item in items if _is_recent(item)]
    if len(fresh) < min_keep and min_keep > 0:
        # Exclude expired system internal memos from min_keep backfill so stale 800-char memos do not bloat context
        def _is_stale_sys(it: dict[str, Any]) -> bool:
            role = str(it.get("role") or "").strip().lower()
            name = str(it.get("name") or "").strip().lower()
            line = str(it.get("line") or "").strip()
            is_sys = (
                role == "system"
                or name == "system"
                or line.startswith(("System:", "(内心のメモ", "（内心のメモ"))
            )
            if not is_sys:
                return False
            if cutoff is None:
                return False
            t = _get_ts(it)
            return t is not None and t < cutoff

        non_stale_sys = [item for item in items if not _is_stale_sys(item)]
        fresh = non_stale_sys[-min_keep:]

    # Keep at most the latest 1 system memo in context items
    sys_indices = [
        i for i, item in enumerate(fresh)
        if str(item.get("role") or "").strip().lower() == "system"
        or str(item.get("name") or "").strip().lower() == "system"
        or str(item.get("line") or "").strip().startswith(("System:", "(内心のメモ", "（内心のメモ"))
    ]
    if len(sys_indices) > 1:
        keep_sys_idx = sys_indices[-1]
        fresh = [
            item for i, item in enumerate(fresh)
            if i not in sys_indices or i == keep_sys_idx
        ]
    return fresh


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
    # 注意: 剪定は「古すぎる項目の物理削除」専用。返信対象より後に届いた未来メッセージは
    # 後続メッセージ自身の返信文脈として必要なため、ここでは削除しない（reject_future=False）。
    kept = [
        item for i, item in enumerate(items)
        if _context_item_is_recent(item, now_ts=now_ts, max_age_sec=max_age_sec, reject_future=False)
        or (len(items) - i) <= min_keep
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


def _extract_single_embed_text(embed: Any) -> str:
    if embed is None:
        return ""
    parts: list[str] = []
    if isinstance(embed, Mapping):
        title = str(embed.get("title") or "").strip()
        desc = str(embed.get("description") or "").strip()
        fields = embed.get("fields") or []
    else:
        title = str(getattr(embed, "title", "") or "").strip()
        desc = str(getattr(embed, "description", "") or "").strip()
        fields = getattr(embed, "fields", None) or []

    if title:
        parts.append(title)
    if desc:
        parts.append(desc)

    if isinstance(fields, Iterable) and not isinstance(fields, (str, bytes)):
        for f in fields:
            if isinstance(f, Mapping):
                fn = str(f.get("name") or "").strip()
                fv = str(f.get("value") or "").strip()
            else:
                fn = str(getattr(f, "name", "") or "").strip()
                fv = str(getattr(f, "value", "") or "").strip()
            if fn and fv:
                parts.append(f"{fn}: {fv}")
            elif fn:
                parts.append(fn)
            elif fv:
                parts.append(fv)

    return " | ".join(parts)


def extract_embed_summary_text(message: Any, *, max_chars: int = 220) -> str:
    """メッセージ内のEmbedからタイトル・説明・フィールドを抽出して平文要約を返す。"""
    if message is None:
        return ""
    raw_embeds = getattr(message, "embeds", None)
    if not raw_embeds:
        return ""
    embed_texts: list[str] = []
    for e in raw_embeds:
        t = _extract_single_embed_text(e)
        if t:
            embed_texts.append(t)
    if not embed_texts:
        return ""
    combined = " / ".join(embed_texts)
    combined = re.sub(r"\s+", " ", combined).strip()
    if len(combined) > max_chars:
        return combined[: max_chars - 1] + "…"
    return combined


def to_context_turn(
    message: discord.Message,
    bot_user_id: Optional[int] = None,
) -> Optional[ContextTurn]:
    text = content(message)
    if not text:
        # 本文が空でも添付ファイル・スタンプ・Embedがあればプレースホルダ／要約を生成
        attachments = list(getattr(message, "attachments", []) or [])
        stickers = list(getattr(message, "stickers", []) or [])
        embed_summary = extract_embed_summary_text(message)
        if attachments:
            content_types = [str(getattr(a, "content_type", "") or "") for a in attachments]
            filenames = [str(getattr(a, "filename", "") or "").lower() for a in attachments]
            if any(ct.startswith("video/") for ct in content_types) or any(fn.endswith((".mp4", ".mov", ".webm", ".avi", ".mkv")) for fn in filenames):
                text = "[動画]"
            elif any(ct.startswith("image/") for ct in content_types) or any(fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")) for fn in filenames):
                text = "[画像]"
            else:
                text = "[添付ファイル]"
        elif stickers:
            text = "[スタンプ]"
        elif embed_summary:
            text = f"[Embed: {embed_summary}]"
        else:
            return None

    aid = author_id(message)
    is_assistant = bot_user_id is not None and aid == bot_user_id
    role = "assistant" if is_assistant else "user"
    name = "Assistant" if is_assistant else display_name(message)
    reactions_suffix, reactions_data = format_message_reactions(message, bot_user_id=bot_user_id)

    reply_target_name: Optional[str] = None
    ref = getattr(message, "reference", None)
    if ref is not None:
        resolved_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
        if isinstance(resolved_msg, discord.Message) or hasattr(resolved_msg, "author"):
            target_name = display_name(resolved_msg)
            if target_name and target_name != "unknown":
                reply_target_name = target_name

    created_at_dt = getattr(message, "created_at", None)
    time_prefix = ""
    if created_at_dt is not None and isinstance(created_at_dt, datetime):
        if created_at_dt.tzinfo is None:
            created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
        dt_jst = created_at_dt.astimezone(_JST)
        time_prefix = f"[{dt_jst.strftime('%H:%M')}] "

    prefix_target = f" (>>{reply_target_name})" if reply_target_name else ""
    line = f"{time_prefix}{name}{prefix_target}: {text}{reactions_suffix}"
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
    ref_mid = getattr(ref, "message_id", None) if ref is not None else None
    if isinstance(ref_mid, int):
        turn["reference_id"] = ref_mid
    if reply_target_name:
        turn["reply_target_name"] = reply_target_name
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
            reply_target_name = item.get("reply_target_name")
            prefix_target = f" (>>{reply_target_name})" if reply_target_name else ""
            item["line"] = f"{name}{prefix_target}: {text}{suffix}"
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


def remove_context_message(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int,
    message_id: Optional[int],
) -> None:
    """指定されたメッセージIDをキャッシュから削除する。"""
    if message_id is None:
        return
    q = cache.get(channel_id_value)
    if not q:
        return
    kept = [item for item in q if item.get("id") != message_id]
    if len(kept) != len(q):
        q.clear()
        q.extend(kept)


def prune_reply_chain_from_cache(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int,
    chain: list[discord.Message],
    *,
    max_chain_len: int = 8,
) -> list[discord.Message]:
    """リプライツリー内のメッセージが通常履歴キャッシュと二重表示されないようキャッシュ側を整理し、
    会話の起点（最古メッセージ）から最新までを含むチェーン全体を返す。
    """
    if not chain:
        return []
    for msg in chain:
        mid = getattr(msg, "id", None)
        if isinstance(mid, int):
            remove_context_message(cache, channel_id_value, mid)
    if len(chain) > max_chain_len:
        return chain[-max_chain_len:]
    return chain


def find_reply_parent_from_cache(
    cache: dict[int, deque[dict[str, Any]]],
    channel_id_value: int | None,
    target_message_id: int | None,
    *,
    bot_user_id: int | None = None,
    before_message: Any = None,
) -> dict[str, Any] | None:
    if channel_id_value is None or not cache.get(channel_id_value):
        return None
    boundary_id, boundary_ts = context_boundary_of(before_message)

    def _is_future(cand: dict[str, Any]) -> bool:
        # 返信対象メッセージ自身およびそれ以降の発言は「親」として採用しない
        if before_message is None:
            return False
        return is_context_item_after_boundary(
            cand,
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            exclude_boundary_message=True,
        )

    def _is_system_memo_entry(entry: dict[str, Any]) -> bool:
        role = str(entry.get("role") or "").strip().lower()
        name = str(entry.get("name") or "").strip().lower()
        line = str(entry.get("line") or "").strip()
        return (
            role == "system"
            or name == "system"
            or line.startswith(("System:", "(内心のメモ", "（内心のメモ"))
        )

    q = cache[channel_id_value]
    target_idx = -1
    if isinstance(target_message_id, int):
        for idx in range(len(q) - 1, -1, -1):
            item = q[idx]
            if not isinstance(item, dict) or _is_system_memo_entry(item):
                continue
            if item.get("message_id") == target_message_id or item.get("id") == target_message_id:
                target_idx = idx
                break
        if target_idx == -1:
            return None

    search_start = target_idx - 1 if target_idx > 0 else (len(q) - 1 if target_message_id is None else -1)
    for idx in range(search_start, -1, -1):
        cand = q[idx]
        if not isinstance(cand, dict) or _is_system_memo_entry(cand):
            continue
        line = str(cand.get("line") or "").strip()
        if not line:
            continue
        cand_aid = cand.get("author_id")
        if bot_user_id is not None and cand_aid == bot_user_id:
            continue
        if str(cand.get("role") or "").strip().lower() == "assistant":
            continue
        if _is_future(cand):
            continue
        return cand
    return None


def format_discord_context_line_with_relation(
    msg: Any,
    author_name: str,
    text: str,
) -> str:
    """メッセージの返信先参照（reference）の有無に応じて構造化ラベルを付与する。"""
    clean_text = " ".join(str(text or "").split())
    if not clean_text:
        return ""

    ref_label = ""
    ref = getattr(msg, "reference", None)
    if ref:
        resolved_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
        if resolved_msg is not None:
            author = getattr(resolved_msg, "author", None)
            if author is not None:
                target_author = getattr(author, "display_name", "") or getattr(author, "name", "")
                if not target_author and isinstance(author, str):
                    target_author = author
                if target_author:
                    ref_label = f" [>>{target_author} への返信]"

    if not ref_label:
        ref_label = " [独立発言]"

    return f"{author_name}{ref_label}: {clean_text}"


_RE_TIME_PREFIX = re.compile(r"^(\[\d{1,2}:\d{2}\]\s*)(.*)$")
_RE_PLAIN_LINE = re.compile(r"^([^:]+?):(?!/)(.*)$")


def format_context_lines_with_relation_labels(context_lines: list[str]) -> list[str]:
    """既存の context_lines リスト内の各行に親子関係ラベル（[>>誰々 への返信] / [独立発言]）を補完する。"""
    formatted: list[str] = []
    for line in context_lines:
        line_str = str(line or "").strip()
        if not line_str:
            continue
        if "[>>" in line_str and " への返信]" in line_str:
            formatted.append(line_str)
            continue
        if "[独立発言]" in line_str:
            formatted.append(line_str)
            continue

        time_pfx = ""
        m_time = _RE_TIME_PREFIX.match(line_str)
        content_to_parse = line_str
        if m_time:
            time_pfx = m_time.group(1)
            content_to_parse = m_time.group(2).strip()

        m_old = re.match(r"^([^:]*?)\s*\(>>([^)]+)\)\s*:(.*)$", content_to_parse)
        if m_old:
            speaker = m_old.group(1).strip()
            target = m_old.group(2).strip()
            body = m_old.group(3).strip()
            formatted.append(f"{time_pfx}{speaker} [>>{target} への返信]: {body}")
            continue

        m_plain = _RE_PLAIN_LINE.match(content_to_parse)
        if m_plain:
            speaker = m_plain.group(1).strip()
            body = m_plain.group(2).strip()
            formatted.append(f"{time_pfx}{speaker} [独立発言]: {body}")
            continue

        formatted.append(line_str)
    return formatted


_INCOMPLETE_TOPIC_OPENER_RE = re.compile(
    r"^(.{2,24}?)(?:ってさ[あぁー]*|(?<![ァ-ヶー])さ[あぁー]+|なんだけど(?:さ[あぁー]*)?|だけど(?:さ[あぁー]*)?|の話(?:だけど|なんだけど|さ[あぁー]*)?|ってどうよ|ってどうなの)[!！?？…・\s]*$",
    re.IGNORECASE,
)


def looks_like_incomplete_topic_opener(text: str) -> bool:
    """『novelaiさあ』『〇〇ってさ』『〇〇なんだけど』のように、直後の連投で本論が続く未完結な話題切り出し短文かを判定する。"""
    raw = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", str(text or "")).strip()
    raw = re.sub(r"<@!?\d+>", "", raw).strip()
    if not raw or len(raw) > 24 or "\n" in raw:
        return False
    return bool(_INCOMPLETE_TOPIC_OPENER_RE.match(raw))


def collect_same_author_burst_texts(
    cache: Mapping[int, Iterable[Mapping[str, Any]]] | None,
    channel_id_value: int | None,
    target_message: Any,
    *,
    max_window_sec: float = 15.0,
    max_messages: int = 3,
) -> list[str]:
    """返信対象メッセージの直後（max_window_sec 秒以内）に同一 author_id が連投したキャッシュ内の後続メッセージ本文を収集する。"""
    if not cache or channel_id_value is None or target_message is None:
        return []
    q = cache.get(channel_id_value)
    if not q:
        return []

    target_aid = author_id(target_message)
    if target_aid is None and isinstance(target_message, Mapping):
        target_aid = target_message.get("author_id")
    if target_aid is None:
        return []

    boundary_id, boundary_ts = context_boundary_of(target_message)
    if boundary_id is None and isinstance(target_message, Mapping):
        raw_id = target_message.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
            boundary_id = raw_id
    if boundary_ts is None and isinstance(target_message, Mapping):
        boundary_ts = context_item_created_at_ts(dict(target_message))
    if boundary_id is None and boundary_ts is None:
        return []

    target_text = str(
        content(target_message)
        or (target_message.get("content") if isinstance(target_message, Mapping) else "")
        or ""
    ).strip()
    collected: list[str] = []
    seen: set[str] = {target_text} if target_text else set()

    for item in list(q):
        if item.get("author_id") != target_aid:
            continue
        if item.get("role") == "assistant":
            continue
        item_id = item.get("id")
        if boundary_id is not None and item_id is not None and item_id == boundary_id:
            continue
        if not is_context_item_after_boundary(
            item,
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            exclude_boundary_message=True,
        ):
            continue
        item_ts = context_item_created_at_ts(dict(item))
        if boundary_ts is not None and item_ts is not None:
            if (item_ts - boundary_ts) > max_window_sec:
                continue
        text = str(item.get("content") or "").strip()
        if not text:
            line = str(item.get("line") or "").strip()
            if ":" in line:
                text = line.split(":", 1)[1].strip()
        text = re.sub(r"\s*\[(?:Bot)?リアクション:[^\]]+\]\s*$", "", text).strip()
        if text and text not in seen:
            seen.add(text)
            collected.append(text)
            if len(collected) >= max_messages:
                break

    return collected


def build_different_author_boundary_note(
    target_message: Any,
    last_history_message: Any,
    *,
    bot_user_id: int | None = None,
    prior_context_items: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Botが返信する対象メッセージと直前履歴メッセージの発言者が異なり、引用・返信関係がない場合に境界注記を返す。"""
    if target_message is None or last_history_message is None:
        return ""

    t_aid = author_id(target_message)
    if t_aid is None and isinstance(target_message, dict):
        t_aid = target_message.get("author_id")

    l_aid = author_id(last_history_message)
    if l_aid is None and isinstance(last_history_message, dict):
        l_aid = last_history_message.get("author_id")

    # 同一ユーザーの場合はガード不要
    if t_aid is not None and l_aid is not None and t_aid == l_aid:
        return ""

    # 直前が Bot 自身の場合はガード不要
    if bot_user_id is not None and l_aid == bot_user_id:
        return ""

    if isinstance(last_history_message, dict):
        l_author_id = last_history_message.get("author_id")
        l_name = str(last_history_message.get("name") or "").strip()
        l_role = str(last_history_message.get("role") or "").strip().lower()
        l_line = str(last_history_message.get("line") or last_history_message.get("content") or "").strip()
        if l_role in ("assistant", "system", "bot", "ai"):
            return ""
        if l_name.lower() in ("assistant", "system", "bot", "ai"):
            return ""
        if l_line.startswith(("System:", "Assistant:", "(内心のメモ", "（内心のメモ")):
            return ""
    else:
        l_author_ref = getattr(last_history_message, "author", None)
        l_author_id = (
            getattr(l_author_ref, "id", None)
            if l_author_ref is not None
            else getattr(last_history_message, "author_id", None)
        )
        l_name = str(
            getattr(l_author_ref, "display_name", None)
            or getattr(l_author_ref, "name", None)
            or getattr(last_history_message, "name", "")
            or ""
        ).strip()
        l_role = str(getattr(last_history_message, "role", "") or "").strip().lower()
        if l_role in ("assistant", "system", "bot", "ai") or l_name.lower() in ("assistant", "system", "bot", "ai"):
            return ""
        if bool(getattr(l_author_ref, "bot", False)):
            return ""

    # 表示名の取得
    t_name = (
        target_message.get("name") if isinstance(target_message, dict) else ""
    ) or display_name(target_message) or "ユーザー"
    if not l_name:
        l_name = (
            last_history_message.get("name") if isinstance(last_history_message, dict) else ""
        ) or display_name(last_history_message) or "別ユーザー"

    if t_name == "unknown":
        t_name = (
            target_message.get("name") if isinstance(target_message, dict) else ""
        ) or getattr(getattr(target_message, "author", None), "name", "") or "ユーザー"
    if l_name == "unknown":
        l_name = (
            last_history_message.get("name") if isinstance(last_history_message, dict) else ""
        ) or getattr(getattr(last_history_message, "author", None), "name", "") or "別ユーザー"

    if t_name == l_name:
        return ""

    # 引用・返信関係の確認（target_message が last_history_message を参照しているか）
    ref = getattr(target_message, "reference", None) or (
        target_message.get("reference") if isinstance(target_message, dict) else None
    )
    if ref is not None:
        ref_mid = getattr(ref, "message_id", None) or (
            ref.get("message_id") if isinstance(ref, dict) else None
        )
        l_mid = getattr(last_history_message, "id", None) or (
            last_history_message.get("id") if isinstance(last_history_message, dict) else None
        )
        if ref_mid is not None and l_mid is not None and ref_mid == l_mid:
            return ""
        resolved = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
        if resolved is not None:
            res_mid = getattr(resolved, "id", None)
            if res_mid is not None and l_mid is not None and res_mid == l_mid:
                return ""
            res_aid = author_id(resolved)
            if res_aid is not None and l_aid is not None and res_aid == l_aid:
                return ""

    l_ref = getattr(last_history_message, "reference", None) or (
        last_history_message.get("reference") if isinstance(last_history_message, dict) else None
    )
    if l_ref is not None:
        l_ref_mid = getattr(l_ref, "message_id", None) or (
            l_ref.get("message_id") if isinstance(l_ref, dict) else None
        )
        t_mid = getattr(target_message, "id", None) or (
            target_message.get("id") if isinstance(target_message, dict) else None
        )
        if l_ref_mid is not None and t_mid is not None and l_ref_mid == t_mid:
            return ""

    base_note = (
        f"※直前にある別ユーザーの発言（{l_name}の発言）は別件の話題です。"
        f"返信対象である {t_name} の発言内容にのみ集中し、直前発言の語彙を混入させないでください。"
    )
    if prior_context_items:
        return (
            f"{base_note}"
            f"特に『{l_name}の話題が〇〇に繋がる』のように直前の別話題と {t_name} の発言を無理やり関連付けたり合体（キメラ合成）させることは絶対厳禁です。"
            f"{l_name}の発言より前に話されていた同テーマの会話文脈および {t_name} の発言の続きとして反応してください。"
        )
    return base_note


_META_IGNORANCE_PATTERNS = [
    re.compile(r"(?:急に|いきなり)?.*(?:置いてけぼり|おいてけぼり|蚊帳の外)", re.IGNORECASE),
    re.compile(r"(?:急に|いきなり).*(?:言われても|振られても|言われたって).*(?:分から|わから|困|ついていけ)", re.IGNORECASE),
    re.compile(r"(?:話|会話|話題)についていけ(?:ない|ません|なくな)", re.IGNORECASE),
    re.compile(r"(?:急に|いきなり).*(?:何の話|何の話題|何のこと).*(?:分から|わから)", re.IGNORECASE),
]


def looks_like_meta_ignorance_reply(text: str) -> bool:
    """「置いてけぼり」や「急に言われても分からない」等のメタ無知・疎外感表明を検知する。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    return any(p.search(cleaned) for p in _META_IGNORANCE_PATTERNS)


def sanitize_meta_ignorance_reply(
    text: str,
    *,
    replacement: str = "ふふ、なるほどね！そういうこともあるんだね。",
) -> str:
    """メタ無知・疎外感表明が検知された場合、等身大の共感・相槌に書き換える。"""
    if looks_like_meta_ignorance_reply(text):
        return replacement
    return text


