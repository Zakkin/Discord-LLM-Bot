"""§155 第1.5段階: Bot 発言に対する人間の反応を bot_activities (kind=reply_feedback) へ記録するモジュール。

生成・送信の挙動は一切変えず、人間の反応文と決定的マーカー信号を元の Bot 発言に紐付けて台帳へ残す（fail-open）。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import inspect
import json
import logging
import os
import time
from typing import Any

from front.discord.common.discord_helpers import resolve_reference_message
from front.discord.utils import (
    author_id,
    channel_id,
    content,
    datetime_to_ts,
    display_name,
    guild_id,
    snowflake_to_ts,
)
from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_int, cfg_persona_namespace

log = logging.getLogger("front.discord.reply.feedback_mining")

FEEDBACK_FRONT = "discord"
FEEDBACK_ACTIVITY_KIND = "reply_feedback"
DEFAULT_REPLY_FEEDBACK_WINDOW_SEC = 600
FEEDBACK_SUMMARY_MAX_CHARS = 400
FEEDBACK_DETAIL_MAX_CHARS = 800
FEEDBACK_REACTOR_MAX_CHARS = 120
_CLOCK_SKEW_TOLERANCE_SEC = 30.0

_STRONG_CORRECTION_MARKERS: tuple[str, ...] = (
    "違う",
    "違います",
    "そんなこと書いてない",
    "そんな事書いてない",
    "言ってない",
    "書いてない",
    "間違ってる",
    "間違えている",
    "間違いだ",
    "見間違",
    "読み間違",
    "そうじゃない",
)
_WEAK_CORRECTION_MARKERS: tuple[str, ...] = (
    "じゃなくて",
)

_STRONG_INCOMPREHENSION_MARKERS: tuple[str, ...] = (
    "何言ってるかわか",
    "何を言っているかわか",
    "何言ってるのかわか",
    "意味不明",
    "意味がわか",
    "何が言いたいかわか",
    "支離滅裂",
    "何言ってるか分から",
    "何言ってるか分かん",
    "何を言っているか分から",
    "何を言っているか分かん",
    "何言ってるのか分から",
    "何言ってるのか分かん",
    "意味が分から",
    "意味が分かん",
    "何が言いたいか分から",
    "何が言いたいか分かん",
)
_WEAK_INCOMPREHENSION_MARKERS: tuple[str, ...] = (
    "何の話",
)

_POSITIVE_MARKERS: tuple[str, ...] = (
    "ありがとう",
    "助かる",
    "助かり",
    "その通り",
    "なるほど",
    "正解",
    "いいね",
)

_NEGATIVE_MARKERS: tuple[str, ...] = (
    "つまらな",
    "うるさい",
    "黙れ",
    "黙って",
    "しつこい",
    "やめて",
)

_PROXIMITY_TRIGGER_SIGNALS: frozenset[str] = frozenset({"correction", "incomprehension"})

_ORIG_CFG = cfg
_ORIG_CFG_BOOL = cfg_bool
_ORIG_CFG_INT = cfg_int
_CFG_UNSET = object()

# channel_id (str) -> (message_id (str), sent_at_ts (float), target_kind (str))
_LAST_BOT_MESSAGES: dict[str, tuple[str, float, str]] = {}


def _fb_cfg_bool(name: str, default: bool = False) -> bool:
    if cfg_bool is not _ORIG_CFG_BOOL:
        return bool(cfg_bool(name, default))
    raw: Any = cfg(name, _CFG_UNSET)
    if raw is _CFG_UNSET:
        env_val = os.environ.get(name)
        if env_val is not None:
            raw = env_val
        else:
            return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(raw)


def _fb_cfg_int(name: str, default: int = 0) -> int:
    if cfg_int is not _ORIG_CFG_INT:
        try:
            return int(cfg_int(name, default))
        except (TypeError, ValueError):
            return default
    raw: Any = cfg(name, _CFG_UNSET)
    if raw is _CFG_UNSET:
        env_val = os.environ.get(name)
        if env_val is not None:
            raw = env_val
        else:
            return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _normalize_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        s = value.strip()
        return s if s and s.isdigit() else None
    return None


def _normalize_int_id(value: Any) -> int | None:
    norm = _normalize_id(value)
    if norm is None:
        return None
    try:
        return int(norm)
    except (TypeError, ValueError):
        return None


def _extract_message_ts(message: Any) -> float | None:
    if message is None:
        return None
    created_at = getattr(message, "created_at", None)
    if isinstance(created_at, datetime):
        ts = datetime_to_ts(created_at)
        if ts is not None:
            return ts
    raw_id = getattr(message, "id", None)
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return snowflake_to_ts(raw_id)
    return None


def _safe_reactor_name(message: Any) -> str:
    author = getattr(message, "author", None)
    if author is not None:
        dname = getattr(author, "display_name", None)
        if isinstance(dname, str) and dname.strip():
            return dname.strip()[:FEEDBACK_REACTOR_MAX_CHARS]
        uname = getattr(author, "name", None)
        if isinstance(uname, str) and uname.strip():
            return uname.strip()[:FEEDBACK_REACTOR_MAX_CHARS]
    raw = display_name(message)
    return (str(raw or "").strip() or "unknown")[:FEEDBACK_REACTOR_MAX_CHARS]


def record_last_bot_message(
    channel_id_value: Any,
    message_id_value: Any,
    *,
    sent_at: float | None = None,
    kind: str = "reply",
    overwrite_same_id: bool = True,
) -> bool:
    """チャンネルごとの Bot 直近発言 (message_id, sent_at, kind) をオンメモリ辞書へ保持する。"""
    cid = _normalize_id(channel_id_value)
    mid = _normalize_id(message_id_value)
    if cid is None or mid is None:
        return False
    if not overwrite_same_id:
        prev = _LAST_BOT_MESSAGES.get(cid)
        if prev is not None and prev[0] == mid:
            return False
    ts = (
        float(sent_at)
        if isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool)
        else time.time()
    )
    clean_kind = str(kind or "reply").strip()[:32] or "reply"
    _LAST_BOT_MESSAGES[cid] = (mid, ts, clean_kind)
    return True


def record_bot_sent_message(
    channel_id_value: Any,
    sent_message: Any,
    *,
    kind: str = "reply",
    overwrite_same_id: bool = True,
) -> bool:
    """送信完了した Bot メッセージオブジェクトから直近発言を記録する（fail-open）。"""
    try:
        if sent_message is None:
            return False
        cid = channel_id_value if channel_id_value is not None else channel_id(sent_message)
        mid = getattr(sent_message, "id", None)
        sent_at = _extract_message_ts(sent_message)
        return record_last_bot_message(
            cid,
            mid,
            sent_at=sent_at,
            kind=kind,
            overwrite_same_id=overwrite_same_id,
        )
    except Exception as exc:
        log.debug("[FEEDBACK] failed to cache last bot message: %r", exc)
        return False


def get_last_bot_message(channel_id_value: Any) -> tuple[str, float, str] | None:
    """指定チャンネルの Bot 直近発言 (message_id, sent_at_ts, kind) を返す。"""
    cid = _normalize_id(channel_id_value)
    if cid is None:
        return None
    return _LAST_BOT_MESSAGES.get(cid)


def clear_last_bot_messages() -> None:
    """Bot 直近発言のオンメモリ辞書を初期化する（主にテスト用）。"""
    _LAST_BOT_MESSAGES.clear()


def detect_feedback_signals(text: str, *, reference_confirmed: bool = False) -> list[str]:
    """§4 の決定的マーカーで人間の反応信号を検出する純関数。

    Args:
        text: 人間の発言本文。
        reference_confirmed: 返信参照により対象が Bot 発言と確定している場合は True。
            True のときのみ弱いマーカー（『じゃなくて』『何の話』）も有効化する。
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    signals: list[str] = []

    has_correction = any(m in raw for m in _STRONG_CORRECTION_MARKERS) or (
        reference_confirmed and any(m in raw for m in _WEAK_CORRECTION_MARKERS)
    )
    if has_correction:
        signals.append("correction")

    has_incomprehension = any(m in raw for m in _STRONG_INCOMPREHENSION_MARKERS) or (
        reference_confirmed and any(m in raw for m in _WEAK_INCOMPREHENSION_MARKERS)
    )
    if has_incomprehension:
        signals.append("incomprehension")

    if any(m in raw for m in _POSITIVE_MARKERS):
        signals.append("positive")

    if any(m in raw for m in _NEGATIVE_MARKERS):
        signals.append("negative")

    return signals


async def record_reply_feedback(
    *,
    store: Any,
    target_ref: str | int,
    target_kind: str = "reply",
    signals: Sequence[str] = (),
    reactor: str,
    window_sec: int,
    summary_text: str,
    channel_id_value: int | str | None,
    guild_id_value: int | None = None,
    persona: str | None = None,
) -> bool:
    """Bot 発言に対する人間の反応を bot_activities (kind=reply_feedback) へ記録する（fail-open）。

    例外はすべて握り warning ログを1行出力して False を返す。
    """
    try:
        if not _fb_cfg_bool("REPLY_FEEDBACK_ENABLED", True):
            return False
        if not _fb_cfg_bool("ACTIVITY_LOG_ENABLED", True):
            return False

        add_act_fn = getattr(store, "add_bot_activity", None) if store is not None else None
        if not callable(add_act_fn):
            return False

        summary = str(summary_text or "").strip()[:FEEDBACK_SUMMARY_MAX_CHARS]
        if not summary:
            return False

        target_ref_str = str(target_ref or "").strip()[:64]
        if not target_ref_str:
            return False

        clean_kind = str(target_kind or "reply").strip()[:32] or "reply"
        signals_list = [str(s).strip() for s in (signals or ()) if str(s).strip()]
        clean_reactor = (str(reactor or "").strip() or "unknown")[:FEEDBACK_REACTOR_MAX_CHARS]
        clean_window = max(0, int(window_sec))

        detail_payload: dict[str, Any] = {
            "target_ref": target_ref_str,
            "target_kind": clean_kind,
            "signals": signals_list,
            "reactor": clean_reactor,
            "window_sec": clean_window,
        }
        detail = json.dumps(detail_payload, ensure_ascii=False)[:FEEDBACK_DETAIL_MAX_CHARS]

        resolved_persona = (
            str(persona).strip()
            if persona and str(persona).strip()
            else cfg_persona_namespace(custom_cfg=cfg)
        )
        cid_int = _normalize_int_id(channel_id_value)
        source_ref_str: str | None = None
        if channel_id_value is not None:
            raw_source_ref = str(channel_id_value).strip()
            if raw_source_ref:
                source_ref_str = raw_source_ref

        res = add_act_fn(
            persona=resolved_persona,
            front=FEEDBACK_FRONT,
            kind=FEEDBACK_ACTIVITY_KIND,
            summary=summary,
            detail=detail,
            guild_id=guild_id_value,
            channel_id=cid_int,
            source_ref=source_ref_str,
        )
        if inspect.isawaitable(res):
            await res

        log.info(
            "[FEEDBACK] reply feedback recorded: target=%s signals=%s",
            target_ref_str,
            ",".join(signals_list),
        )
        return True
    except Exception as exc:
        log.warning("[FEEDBACK] failed to record reply_feedback: %r", exc)
        return False


async def maybe_record_reply_feedback(
    owner: Any,
    message: Any,
    *,
    bot_user_id: int | None = None,
    now_ts: float | None = None,
) -> bool:
    """on_message から呼ばれる人間の反応記録エントリポイント（§5, fail-open）。

    発火条件:
    1. 返信参照で確定: resolve_reference_message で解決した参照先の作者が Bot 自身の場合、
       信号の有無にかかわらず reply_feedback を記録する。
    2. 近接＋強い信号: 参照がなく、同一チャンネルの直近 Bot 発言から
       REPLY_FEEDBACK_WINDOW_SEC（既定 600）秒以内で、correction または incomprehension を含む場合。
    """
    try:
        if not _fb_cfg_bool("REPLY_FEEDBACK_ENABLED", True):
            return False
        if not _fb_cfg_bool("ACTIVITY_LOG_ENABLED", True):
            return False
        if message is None:
            return False

        author = getattr(message, "author", None)
        if getattr(author, "bot", False):
            return False

        resolved_bot_uid = bot_user_id
        if resolved_bot_uid is None and owner is not None:
            bot_obj = getattr(owner, "bot", None)
            bot_user = getattr(bot_obj, "user", None) if bot_obj is not None else None
            resolved_bot_uid = getattr(bot_user, "id", None)

        a_id = author_id(message)
        if resolved_bot_uid is not None and a_id == resolved_bot_uid:
            return False

        text = content(message)
        if not text:
            return False

        cid = channel_id(message)
        if cid is None:
            return False

        store = getattr(owner, "memory_store", None) if owner is not None else None
        if not callable(getattr(store, "add_bot_activity", None)):
            return False

        msg_ts = (
            float(now_ts)
            if isinstance(now_ts, (int, float)) and not isinstance(now_ts, bool)
            else (_extract_message_ts(message) or time.time())
        )
        reactor = _safe_reactor_name(message)
        gid = guild_id(message)

        raw_ref = getattr(message, "reference", None)
        has_explicit_ref = raw_ref is not None and (
            getattr(raw_ref, "resolved", None) is not None
            or getattr(raw_ref, "cached_message", None) is not None
            or _normalize_id(getattr(raw_ref, "message_id", None)) is not None
        )

        ref_msg = await resolve_reference_message(message)
        if ref_msg is not None:
            ref_author_id = author_id(ref_msg)
            if resolved_bot_uid is None or ref_author_id != resolved_bot_uid:
                return False

            target_ref_str = _normalize_id(getattr(ref_msg, "id", None))
            if target_ref_str is None:
                raw_mid = str(getattr(ref_msg, "id", "") or "").strip()
                if not raw_mid:
                    return False
                target_ref_str = raw_mid

            last_bot = get_last_bot_message(cid)
            ref_ts: float | None = None
            if last_bot is not None and last_bot[0] == target_ref_str:
                target_kind = last_bot[2]
                ref_ts = _extract_message_ts(ref_msg) or last_bot[1]
            else:
                target_kind = "reply"
                ref_ts = _extract_message_ts(ref_msg)

            window_sec = max(0, int(msg_ts - ref_ts)) if ref_ts is not None else 0
            signals = detect_feedback_signals(text, reference_confirmed=True)
            return await record_reply_feedback(
                store=store,
                target_ref=target_ref_str,
                target_kind=target_kind,
                signals=signals,
                reactor=reactor,
                window_sec=window_sec,
                summary_text=text,
                channel_id_value=cid,
                guild_id_value=gid,
            )

        # 参照先が解決不能な明示返信は別メッセージ宛ての誤紐付けを避けるため近接判定へ流さない
        if has_explicit_ref:
            return False

        last_bot = get_last_bot_message(cid)
        if last_bot is None:
            return False

        last_mid, last_ts, last_kind = last_bot
        elapsed = msg_ts - last_ts
        max_window_sec = max(0, _fb_cfg_int("REPLY_FEEDBACK_WINDOW_SEC", DEFAULT_REPLY_FEEDBACK_WINDOW_SEC))
        if elapsed < -_CLOCK_SKEW_TOLERANCE_SEC or elapsed > max_window_sec:
            return False

        signals = detect_feedback_signals(text, reference_confirmed=False)
        if not any(sig in _PROXIMITY_TRIGGER_SIGNALS for sig in signals):
            return False

        return await record_reply_feedback(
            store=store,
            target_ref=last_mid,
            target_kind=last_kind,
            signals=signals,
            reactor=reactor,
            window_sec=max(0, int(elapsed)),
            summary_text=text,
            channel_id_value=cid,
            guild_id_value=gid,
        )
    except Exception as exc:
        log.warning("[FEEDBACK] reply feedback evaluation failed: %r", exc)
        return False


__all__ = [
    "DEFAULT_REPLY_FEEDBACK_WINDOW_SEC",
    "FEEDBACK_ACTIVITY_KIND",
    "FEEDBACK_FRONT",
    "clear_last_bot_messages",
    "detect_feedback_signals",
    "get_last_bot_message",
    "maybe_record_reply_feedback",
    "record_bot_sent_message",
    "record_last_bot_message",
    "record_reply_feedback",
]
