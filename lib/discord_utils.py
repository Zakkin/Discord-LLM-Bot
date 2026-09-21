"""
Discord API および UI 処理に関する共通ユーティリティ。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import discord

from .text_utils import compact_whitespace, normalize_text, strip_urls

log = logging.getLogger(__name__)

__all__ = [
    "send_interaction_or_fallback",
    "inject_action_history",
    "DISCORD_MENTION_RE",
    "DISCORD_CHANNEL_RE",
    "DISCORD_EMOJI_RE",
    "DISCORD_EPOCH_MS",
    "strip_discord_mentions",
    "clean_user_input_text",
    "normalize_emoji_str",
    "author_id",
    "channel_id",
    "guild_id",
    "content",
    "display_name",
    "datetime_to_ts",
    "snowflake_to_ts",
    "message_created_at_ts",
]


async def send_interaction_or_fallback(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = False,
    defer_first: bool = False,
    log_tag: str = "interaction",
) -> bool:
    """Interaction に対する応答を行い、期限切れや失敗時には channel.send へ安全にフォールバックする。"""
    response = getattr(interaction, "response", None)
    followup = getattr(interaction, "followup", None)

    if defer_first and response is not None:
        try:
            is_done = bool(response.is_done()) if hasattr(response, "is_done") else False
            if not is_done:
                await response.defer(thinking=True, ephemeral=ephemeral)
        except discord.NotFound as e:
            log.warning("%s interaction defer expired; fallback send starts: %r", log_tag, e)
        except Exception as e:
            log.warning("%s interaction defer failed; fallback send starts: %r", log_tag, e)

    try:
        is_done = bool(response.is_done()) if response is not None and hasattr(response, "is_done") else False
        if response is not None and not is_done:
            await response.send_message(content, ephemeral=ephemeral)
            return True
        if followup is not None:
            await followup.send(content, ephemeral=ephemeral)
            return True
    except discord.NotFound as e:
        log.warning("%s interaction response expired; channel fallback starts: %r", log_tag, e)
    except Exception as e:
        log.warning("%s interaction response failed; channel fallback starts: %r", log_tag, e)

    channel = getattr(interaction, "channel", None)
    if channel is not None:
        try:
            await channel.send(content)
            return True
        except Exception as e:
            log.exception("%s channel fallback send failed: %s", log_tag, e)
    return False


def inject_action_history(
    cache_q: Any,
    *,
    user_label: str,
    bot_name: str,
    action_name: str,
    result_text: str,
    max_result_chars: int = 1500,
) -> None:
    """コンテキスト履歴キューへ、システム依頼ログと結果ログのペアを注入する。"""
    safe_result = str(result_text or "").strip()
    if max_result_chars > 0 and len(safe_result) > max_result_chars:
        safe_result = safe_result[: max_result_chars - 3].rstrip() + "..."
    if not safe_result:
        return

    now_ts = time.time()
    synthetic_base_id = -int(now_ts * 1000)

    cache_q.append({
        "id": synthetic_base_id,
        "author_id": None,
        "line": f"システム: {user_label}からの依頼で「{action_name}」を実行しました。",
        "created_at_ts": now_ts,
    })
    cache_q.append({
        "id": synthetic_base_id - 1,
        "author_id": None,
        "line": f"{bot_name}: 【結果】 {safe_result}",
        "created_at_ts": now_ts,
    })


DISCORD_MENTION_RE = re.compile(r"<@[!&]?\d+>")
DISCORD_CHANNEL_RE = re.compile(r"<#\d+>")
DISCORD_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
DISCORD_EPOCH_MS = 1420070400000


def strip_discord_mentions(text: str) -> str:
    """Discord メンション（<@123>, <@!123>, <@&123>）やチャンネルリンク（<#123>）を除去し空白圧縮する。"""
    cleaned = DISCORD_MENTION_RE.sub(" ", str(text or ""))
    cleaned = DISCORD_CHANNEL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[@＠][^\s\u3000]+", " ", cleaned)
    return compact_whitespace(cleaned)


def clean_user_input_text(text: str, *, remove_urls: bool = False) -> str:
    """ユーザー入力テキストからメンションや（必要に応じて）URLを除去し、NFKC正規化と空白整理を行う。"""
    cleaned = strip_discord_mentions(text)
    if remove_urls:
        cleaned = strip_urls(cleaned)
    return normalize_text(cleaned)


def normalize_emoji_str(emoji: Any) -> str:
    """絵文字オブジェクト（discord.Emoji/PartialEmoji）または文字列を安全に文字列（:name: 等）へ正規化する。"""
    if isinstance(emoji, str):
        s = emoji.strip()
        m = DISCORD_EMOJI_RE.fullmatch(s)
        if m:
            return f":{m.group(1)}:"
        return s
    name = getattr(emoji, "name", "")
    eid = getattr(emoji, "id", None)
    if name:
        return f":{name}:"
    if eid is not None:
        return f":emoji_{eid}:"
    return str(emoji or "").strip()


def author_id(message: Any) -> int | None:
    """メッセージから安全に投稿者の user_id を取得する。"""
    return getattr(getattr(message, "author", None), "id", None)


def channel_id(message: Any) -> int | None:
    """メッセージから安全にチャンネルの channel_id を取得する。"""
    return getattr(getattr(message, "channel", None), "id", None)


def guild_id(message: Any) -> int | None:
    """メッセージから安全にギルドの guild_id を取得する。"""
    return getattr(getattr(message, "guild", None), "id", None)


def content(message: Any) -> str:
    """メッセージから安全に本文テキスト（両端トリム済み）を取得する。"""
    return str(getattr(message, "content", "") or "").strip()


def display_name(target: Any) -> str:
    """メッセージまたはユーザーオブジェクトから安全に投稿者の表示名（ニックネームまたはユーザー名）を取得する。"""
    author = getattr(target, "author", None) or target
    return str(getattr(author, "display_name", "") or getattr(author, "name", "unknown"))


def datetime_to_ts(value: Any) -> float | None:
    """datetime オブジェクトから秒単位の UNIX タイムスタンプを取得する（tzinfo なしの場合は UTC 扱い）。"""
    if not isinstance(value, datetime):
        return None
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return float(dt.timestamp())


def snowflake_to_ts(value: Any) -> float | None:
    """Discord Snowflake ID から秒単位の UNIX タイムスタンプを計算する。"""
    try:
        snowflake = int(value)
    except Exception:
        return None
    if snowflake <= 0:
        return None
    if snowflake < (1 << 22):
        return None
    return float(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000.0)


def message_created_at_ts(message: Any) -> float | None:
    """メッセージオブジェクトから安全に作成日時タイムスタンプ（秒）を取得する。"""
    return datetime_to_ts(getattr(message, "created_at", None)) or snowflake_to_ts(getattr(message, "id", None))


# 後方互換エイリアス
_datetime_to_ts = datetime_to_ts
_snowflake_to_ts = snowflake_to_ts
_normalize_emoji_str = normalize_emoji_str


