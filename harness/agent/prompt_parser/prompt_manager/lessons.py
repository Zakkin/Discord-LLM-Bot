"""振り返り教訓の共有読み取り・ブロック構築基盤（§162）。

§158 が全フロント共通行動台帳（bot_activities, kind="lesson"）へ保存した教訓を
対象 scope ごとに読み取り、Discord 返信プロンプト（§162）・ふたば投稿プロンプト（§163）・
Discord 意味検査（§164）へ共有インターフェースで提供する。
LLM 呼び出しは行わず、すべての取得・構築は fail-open で動作する。
"""
from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool

log = logging.getLogger("harness.agent.prompt_manager.lessons")

LESSON_KIND = "lesson"
VALID_SCOPES: frozenset[str] = frozenset({"discord_reply", "hutaba_post"})
VALID_POLARITIES: frozenset[str] = frozenset({"avoid", "prefer"})

DEFAULT_LESSONS_INJECT_ENABLED = True
DEFAULT_LESSONS_INJECT_MAX_ITEMS = 5
DEFAULT_LESSONS_MAX_AGE_DAYS = 30
DEFAULT_LESSONS_BLOCK_MAX_CHARS = 600

LESSONS_BLOCK_HEADER = "【最近の振り返りからの教訓】"
PREFIX_AVOID = "・避ける: "
PREFIX_PREFER = "・心がける: "

def is_lessons_inject_enabled() -> bool:
    """教訓ブロック注入の全体有効フラグ（LESSONS_INJECT_ENABLED）を返す。"""
    try:
        return bool(cfg_bool("LESSONS_INJECT_ENABLED", DEFAULT_LESSONS_INJECT_ENABLED))
    except Exception:
        return DEFAULT_LESSONS_INJECT_ENABLED


def _resolve_max_items(max_items: int | None) -> int:
    raw = cfg("LESSONS_INJECT_MAX_ITEMS", DEFAULT_LESSONS_INJECT_MAX_ITEMS) if max_items is None else max_items
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LESSONS_INJECT_MAX_ITEMS


def _resolve_max_age_days(max_age_days: int | None) -> int:
    raw = cfg("LESSONS_MAX_AGE_DAYS", DEFAULT_LESSONS_MAX_AGE_DAYS) if max_age_days is None else max_age_days
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return DEFAULT_LESSONS_MAX_AGE_DAYS


def _resolve_max_chars(max_chars: int | None) -> int:
    raw = cfg("LESSONS_BLOCK_MAX_CHARS", DEFAULT_LESSONS_BLOCK_MAX_CHARS) if max_chars is None else max_chars
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LESSONS_BLOCK_MAX_CHARS


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_now(now: datetime | str | None) -> datetime:
    if isinstance(now, datetime):
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    if isinstance(now, str):
        parsed = _parse_iso_datetime(now)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _parse_detail_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_lesson_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())


async def fetch_active_lessons(
    memory_store: Any,
    *,
    persona: str,
    scope: str,
    max_items: int | None = None,
    max_age_days: int | None = None,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """全フロント共通行動台帳から対象 scope の有効な教訓を新しい順に取得する（fail-open）。"""
    if not is_lessons_inject_enabled() or memory_store is None:
        return []
    search_fn = getattr(memory_store, "search_bot_activities", None)
    if not callable(search_fn):
        return []
    try:
        resolved_max_items = _resolve_max_items(max_items)
        target_scope = str(scope or "").strip()
        if resolved_max_items <= 0 or not target_scope:
            return []
        resolved_max_age_days = _resolve_max_age_days(max_age_days)
        resolved_now = _resolve_now(now)
        cutoff = resolved_now - timedelta(days=resolved_max_age_days)
        since_iso = cutoff.replace(microsecond=0).isoformat()
        persona_str = str(persona or "").strip() or "default"
        fetch_limit = max(resolved_max_items * 5, 50)

        raw_rows = search_fn(
            persona=persona_str,
            kinds=[LESSON_KIND],
            since=since_iso,
            limit=fetch_limit,
        )
        if inspect.isawaitable(raw_rows):
            raw_rows = await raw_rows
        if not isinstance(raw_rows, (list, tuple)):
            return []

        candidates: list[tuple[float, int, dict[str, Any]]] = []
        for idx, row in enumerate(raw_rows):
            if not isinstance(row, Mapping):
                continue
            row_kind = str(row.get("kind") or "").strip().lower()
            if row_kind and row_kind != LESSON_KIND:
                continue
            row_persona = str(row.get("persona") or "").strip()
            if row_persona and row_persona != persona_str:
                continue
            detail = _parse_detail_json(row.get("detail"))
            if detail is None or str(detail.get("scope") or "").strip() != target_scope:
                continue
            polarity = str(detail.get("polarity") or "").strip().lower()
            if polarity not in VALID_POLARITIES:
                continue
            text = _normalize_lesson_text(row.get("summary"))
            if not text:
                continue
            created_at_raw = str(row.get("created_at") or "").strip()
            dt = _parse_iso_datetime(created_at_raw)
            if dt is None or dt < cutoff:
                continue
            candidates.append(
                (-dt.timestamp(), idx, {"text": text, "polarity": polarity, "created_at": created_at_raw})
            )

        candidates.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in candidates[:resolved_max_items]]
    except Exception as exc:
        log.debug("[lessons] Failed to fetch active lessons: %r", exc)
        return []


def build_lessons_block(
    lessons: Sequence[Mapping[str, Any]] | None,
    *,
    max_chars: int | None = None,
    header: str = LESSONS_BLOCK_HEADER,
) -> str:
    """教訓リストからプロンプト注入用ブロックを決定的に組み立てる純関数（fail-open）。"""
    try:
        if not lessons:
            return ""
        resolved_max_chars = _resolve_max_chars(max_chars)
        if resolved_max_chars <= 0:
            return ""

        avoid_items: list[tuple[int, float, int, str]] = []
        prefer_items: list[tuple[int, float, int, str]] = []
        for idx, item in enumerate(lessons):
            if not isinstance(item, Mapping):
                continue
            polarity = str(item.get("polarity") or "").strip().lower()
            if polarity not in VALID_POLARITIES:
                continue
            text = _normalize_lesson_text(item.get("text"))
            if not text:
                continue
            dt = _parse_iso_datetime(item.get("created_at"))
            sort_key = (0, -dt.timestamp(), idx) if dt is not None else (1, 0.0, idx)
            if polarity == "avoid":
                avoid_items.append((*sort_key, f"{PREFIX_AVOID}{text}"))
            else:
                prefer_items.append((*sort_key, f"{PREFIX_PREFER}{text}"))

        avoid_items.sort(key=lambda x: (x[0], x[1], x[2]))
        prefer_items.sort(key=lambda x: (x[0], x[1], x[2]))
        ordered_lines = [x[3] for x in avoid_items] + [x[3] for x in prefer_items]
        if not ordered_lines:
            return ""

        clean_header = str(header or "").strip()
        selected_lines: list[str] = []
        current_len = len(clean_header) if clean_header else 0
        for line in ordered_lines:
            added = (1 + len(line)) if (selected_lines or clean_header) else len(line)
            if current_len + added <= resolved_max_chars:
                selected_lines.append(line)
                current_len += added

        if not selected_lines:
            return ""
        if clean_header:
            return "\n".join([clean_header, *selected_lines])
        return "\n".join(selected_lines)
    except Exception as exc:
        log.debug("[lessons] Failed to build lessons block: %r", exc)
        return ""
