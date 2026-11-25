"""話題レベル照合の共有基盤（§137）。

全フロント共通行動台帳（bot_activities）から直近の Bot 発言・投稿を取得し、
話題語の重なりを決定論的に照合して、通常返信・自発発言・ふたば投稿の
各フロントへ共有インターフェースを提供する。
LLM 呼び出しは行わず、すべての取得・照合は fail-open で動作する。
"""
from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool

log = logging.getLogger("harness.agent.prompt_manager.discussed_topics")

# 2文字以上の漢字熟語・カタカナ語・英数字語（orchestrator/proactive.py と揃えた基準）
_TOPIC_TERM_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[ァ-ヶー]{2,}|[a-zA-Z0-9]{2,}")
_MIN_COMMON_TERMS = 2

DEFAULT_DISCUSSED_TOPICS_ENABLED = True
DEFAULT_DISCUSSED_TOPICS_WINDOW_HOURS = 24.0
DEFAULT_DISCUSSED_TOPICS_OVERLAP_THRESHOLD = 0.5
DEFAULT_DISCUSSED_TOPICS_MAX_ITEMS = 5
DEFAULT_DISCUSSED_TOPICS_BLOCK_MAX_CHARS = 600

DISCUSSED_TOPICS_RULE_TEXT = (
    "上記はあなた自身が最近すでに話した話題です。"
    "同じ話題が再び出たら、初めて聞いた・初めて見たように扱わず、前に話したことに触れて続けること。"
    "自分から切り出す場合も、既出の話題を新しい話題として紹介し直さないこと。"
)


def is_discussed_topics_enabled() -> bool:
    """話題レベル照合の全体有効フラグ（DISCUSSED_TOPICS_ENABLED）を返す。"""
    try:
        return bool(cfg_bool("DISCUSSED_TOPICS_ENABLED", DEFAULT_DISCUSSED_TOPICS_ENABLED))
    except Exception:
        return DEFAULT_DISCUSSED_TOPICS_ENABLED


def _resolve_window_hours(window_hours: float | int | None) -> float:
    raw = cfg("DISCUSSED_TOPICS_WINDOW_HOURS", DEFAULT_DISCUSSED_TOPICS_WINDOW_HOURS) if window_hours is None else window_hours
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISCUSSED_TOPICS_WINDOW_HOURS
    return max(val, 0.0)


def _resolve_threshold(threshold: float | None) -> float:
    raw = cfg("DISCUSSED_TOPICS_OVERLAP_THRESHOLD", DEFAULT_DISCUSSED_TOPICS_OVERLAP_THRESHOLD) if threshold is None else threshold
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISCUSSED_TOPICS_OVERLAP_THRESHOLD
    return max(0.0, min(val, 1.0))


def _resolve_max_items(max_items: int | None) -> int:
    raw = cfg("DISCUSSED_TOPICS_MAX_ITEMS", DEFAULT_DISCUSSED_TOPICS_MAX_ITEMS) if max_items is None else max_items
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISCUSSED_TOPICS_MAX_ITEMS


def _resolve_max_chars(max_chars: int | None) -> int:
    raw = cfg("DISCUSSED_TOPICS_BLOCK_MAX_CHARS", DEFAULT_DISCUSSED_TOPICS_BLOCK_MAX_CHARS) if max_chars is None else max_chars
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISCUSSED_TOPICS_BLOCK_MAX_CHARS


def extract_topic_terms(text: str) -> set[str]:
    """話題語（2文字以上の漢字熟語・カタカナ語・英数字語）を抽出する。"""
    raw = str(text or "")
    if not raw:
        return set()
    return {term for term in _TOPIC_TERM_RE.findall(raw.lower()) if term.strip("ー")}


def topic_overlap(terms_a: Iterable[str], terms_b: Iterable[str]) -> float:
    """2つの話題語集合の overlap 係数（共通語数 / 小さい方の語数）を返す。"""
    set_a = set(terms_a) if terms_a else set()
    set_b = set(terms_b) if terms_b else set()
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


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


def _entry_terms(entry: Mapping[str, Any]) -> set[str]:
    existing = entry.get("terms")
    if isinstance(existing, set):
        return existing
    if isinstance(existing, frozenset):
        return set(existing)
    return extract_topic_terms(str(entry.get("summary") or ""))


def _is_topic_match(terms_a: set[str], terms_b: set[str], threshold: float) -> bool:
    if len(terms_a & terms_b) < _MIN_COMMON_TERMS:
        return False
    return topic_overlap(terms_a, terms_b) >= threshold


def _sort_entries_newest_first(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    indexed: list[tuple[int, float, int, dict[str, Any]]] = []
    for idx, row in enumerate(entries):
        if not isinstance(row, Mapping):
            continue
        dt = _parse_iso_datetime(row.get("created_at"))
        if dt is not None:
            indexed.append((0, -dt.timestamp(), idx, dict(row)))
        else:
            indexed.append((1, 0.0, idx, dict(row)))
    indexed.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in indexed]


async def fetch_recent_discussed_topics(
    memory_store: Any,
    *,
    persona: str,
    window_hours: float | int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """全フロント共通行動台帳から直近の返信・投稿を取得し、話題語集合付きで返す（fail-open）。"""
    if not is_discussed_topics_enabled() or memory_store is None:
        return []
    search_fn = getattr(memory_store, "search_bot_activities", None)
    if not callable(search_fn):
        return []
    try:
        hours = _resolve_window_hours(window_hours)
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        resolved_limit = max(1, int(limit))
        persona_str = str(persona or "").strip() or "default"
        raw_rows = search_fn(
            persona=persona_str,
            kinds=["reply", "post"],
            since=since_iso,
            limit=resolved_limit,
        )
        if inspect.isawaitable(raw_rows):
            raw_rows = await raw_rows
        if not isinstance(raw_rows, (list, tuple)):
            return []
        entries = _sort_entries_newest_first(raw_rows)
        for entry in entries:
            entry["terms"] = extract_topic_terms(str(entry.get("summary") or ""))
        return entries
    except Exception as exc:
        log.debug("[discussed-topics] Failed to fetch recent discussed topics: %r", exc)
        return []


async def find_discussed_topic_match(
    memory_store: Any,
    *,
    persona: str,
    candidate_text: str,
    window_hours: float | int | None = None,
    threshold: float | None = None,
) -> dict[str, Any] | None:
    """候補文と話題レベルで一致する最も新しい台帳エントリを返す（無ければ None、fail-open）。"""
    try:
        if not is_discussed_topics_enabled():
            return None
        candidate_terms = extract_topic_terms(candidate_text)
        if len(candidate_terms) < _MIN_COMMON_TERMS:
            return None
        resolved_threshold = _resolve_threshold(threshold)
        topics = await fetch_recent_discussed_topics(
            memory_store,
            persona=persona,
            window_hours=window_hours,
        )
        for entry in topics:
            if _is_topic_match(candidate_terms, _entry_terms(entry), resolved_threshold):
                return entry
        return None
    except Exception as exc:
        log.debug("[discussed-topics] Failed to match discussed topic: %r", exc)
        return None


def _format_block_header(window_hours: float) -> str:
    hours_label = str(int(window_hours)) if window_hours == int(window_hours) else f"{window_hours:g}"
    return f"【最近すでに話した話題（直近{hours_label}時間）】"


def _format_short_timestamp(value: Any) -> str:
    dt = _parse_iso_datetime(value)
    if dt is not None:
        return dt.strftime("%m-%d %H:%M")
    raw = str(value or "").strip()
    if len(raw) >= 16:
        return raw[5:16].replace("T", " ")
    return raw or "--:--"


def _truncate_summary(summary: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(summary) <= max_len:
        return summary
    if max_len == 1:
        return summary[:1]
    return f"{summary[: max_len - 1]}…"


def _fit_summary_lines(prefixes: list[str], summaries: list[str], items_budget: int) -> list[str]:
    if not prefixes or items_budget <= 0:
        return []
    selected_count = 0
    for count in range(len(prefixes), 0, -1):
        overhead = (count - 1) + sum(len(prefixes[i]) for i in range(count))
        avail = items_budget - overhead
        if count == 1 and avail >= 1:
            selected_count = 1
            break
        if count > 1 and avail >= count * 8:
            selected_count = count
            break
    if selected_count == 0:
        return [f"{prefixes[0]}{summaries[0]}"[:items_budget]]

    chosen_prefixes = prefixes[:selected_count]
    chosen_summaries = summaries[:selected_count]
    summaries_budget = items_budget - (selected_count - 1) - sum(len(p) for p in chosen_prefixes)
    lengths = [len(s) for s in chosen_summaries]
    if sum(lengths) <= summaries_budget:
        return [f"{p}{s}" for p, s in zip(chosen_prefixes, chosen_summaries)]

    allocated = [0] * selected_count
    remaining = list(range(selected_count))
    budget_left = summaries_budget
    while remaining:
        share = budget_left // len(remaining)
        short_indices = [i for i in remaining if lengths[i] <= share]
        if not short_indices:
            break
        for i in short_indices:
            allocated[i] = lengths[i]
            budget_left -= lengths[i]
        remaining = [i for i in remaining if i not in short_indices]
    if remaining:
        base = budget_left // len(remaining)
        extra = budget_left % len(remaining)
        for pos, i in enumerate(remaining):
            allocated[i] = base + (1 if pos < extra else 0)
    return [
        f"{p}{_truncate_summary(s, alloc)}"
        for p, s, alloc in zip(chosen_prefixes, chosen_summaries, allocated)
    ]


def build_discussed_topics_block(
    topics: Sequence[Mapping[str, Any]] | None,
    *,
    current_text: str,
    window_hours: float | int | None = None,
    max_items: int | None = None,
    max_chars: int | None = None,
) -> str:
    """現在発言と重なる既出話題だけを抽出し、通常返信用のプロンプトブロックを組み立てる。"""
    try:
        if not is_discussed_topics_enabled() or not topics:
            return ""
        current_terms = extract_topic_terms(current_text)
        if len(current_terms) < _MIN_COMMON_TERMS:
            return ""
        resolved_threshold = _resolve_threshold(None)
        resolved_max_items = _resolve_max_items(max_items)
        resolved_max_chars = _resolve_max_chars(max_chars)
        if resolved_max_items <= 0 or resolved_max_chars <= 0:
            return ""
        resolved_window_hours = _resolve_window_hours(window_hours)

        sorted_topics = _sort_entries_newest_first(topics)
        matched: list[dict[str, Any]] = []
        for entry in sorted_topics:
            if _is_topic_match(current_terms, _entry_terms(entry), resolved_threshold):
                matched.append(entry)
                if len(matched) >= resolved_max_items:
                    break
        if not matched:
            return ""

        header = _format_block_header(resolved_window_hours)
        prefixes: list[str] = []
        summaries: list[str] = []
        for entry in matched:
            ts_label = _format_short_timestamp(entry.get("created_at"))
            front_label = str(entry.get("front") or "不明").strip() or "不明"
            summary_clean = " ".join(str(entry.get("summary") or "").split())
            prefixes.append(f"- [{ts_label} / {front_label}] ")
            summaries.append(summary_clean)

        fixed_overhead = len(header) + len(DISCUSSED_TOPICS_RULE_TEXT) + 2
        items_budget = resolved_max_chars - fixed_overhead
        if items_budget <= 0:
            fallback = "\n".join([header, f"{prefixes[0]}{summaries[0]}", DISCUSSED_TOPICS_RULE_TEXT])
            return fallback[:resolved_max_chars]

        item_lines = _fit_summary_lines(prefixes, summaries, items_budget)
        if not item_lines:
            return ""
        block = "\n".join([header, *item_lines, DISCUSSED_TOPICS_RULE_TEXT])
        if len(block) > resolved_max_chars:
            return block[:resolved_max_chars]
        return block
    except Exception as exc:
        log.debug("[discussed-topics] Failed to build discussed topics block: %r", exc)
        return ""
