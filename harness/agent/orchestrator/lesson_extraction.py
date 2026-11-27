"""AIエージェント・ハーネス オーケストレーター 第2段階 対照レッスンの抽出 (orchestrator/lesson_extraction.py)

§158 第2段階（対照からのレッスン抽出）。
第1段階・第1.5段階で bot_activities に蓄積された記録を「事故・不発の出力」と「通過・好成果の出力」で
対照し、一般化した教訓（kind="lesson"）を middle 系モデルで抽出して台帳へ保存する。
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

from lib.config_utils import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
)
from ollama_bot.common.ollama_helpers import call_ollama_json

log = logging.getLogger("harness.agent.orchestrator.lesson_extraction")

LESSON_KIND = "lesson"
LESSON_SOURCE_REF = "stage2"
SCOPES: tuple[tuple[str, str], ...] = (
    ("discord_reply", "discord"),
    ("hutaba_post", "hutaba"),
)
POLARITIES: tuple[str, ...] = ("avoid", "prefer")
FEEDBACK_NEGATIVE_SIGNALS: frozenset[str] = frozenset({"negative", "correction", "incomprehension"})
FEEDBACK_POSITIVE_SIGNAL = "positive"

DEFAULT_LESSON_EXTRACTION_ENABLED = True
DEFAULT_LESSON_WINDOW_DAYS = 7
DEFAULT_LESSON_MIN_EVIDENCE = 3
DEFAULT_LESSON_MAX_PER_RUN = 3
DEFAULT_LESSON_EXTRACT_INTERVAL_SEC = 86400
DEFAULT_LESSON_EXTRACT_TIMEOUT_SEC = 90.0
DEFAULT_LESSON_EXTRACT_NUM_PREDICT = 768
DEFAULT_LESSON_FEEDBACK_MATERIAL_ENABLED = True
DEFAULT_LESSON_FEEDBACK_MAX_EXAMPLES = 10

LESSON_DEDUP_WINDOW_DAYS = 90
LESSON_SIMILARITY_THRESHOLD = 0.7
HUTABA_NEG_MIN_AGE_SEC = 24 * 3600.0
HUTABA_POS_MIN_SOUDANE = 3
HUTABA_POS_MIN_REPLIES = 2

_SCAN_LIMIT = 500
_SUMMARY_MAX_CHARS = 400
_DETAIL_MAX_CHARS = 800
_MAX_PROMPT_EXAMPLES_PER_SIDE = 15

_LESSON_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lesson": {"type": "string"},
                    "polarity": {"type": "string", "enum": list(POLARITIES)},
                    "evidence_neg": {"type": "integer"},
                    "evidence_pos": {"type": "integer"},
                },
                "required": ["lesson", "polarity", "evidence_neg", "evidence_pos"],
            },
        },
    },
    "required": ["lessons"],
}

CallJsonFn = Callable[..., Awaitable[Any]]

LESSON_EXTRACT_SYSTEM_PROMPT = (
    "あなたはチャットBotの過去出力の負例（事故・不発）と正例（通過・好成果）を対照し、"
    "今後の出力で活かせる一般的な教訓を抽出する分析器です。指定のJSON形式のみで出力し、説明文は不要です。\n"
    "厳守ルール:\n"
    "- 負例と正例の振る舞いの差だけから一般化すること。\n"
    "- 材料にない事実や、固有名詞（人名・ユーザー名・チャンネル名・URL・具体的な日付）を教訓に入れないこと。\n"
    "- フィードバック材料については、併記された人間の反応文と signals を「その発言がなぜ悪く／良く受け取られたか」の根拠として使うこと。反応文中の固有名詞や個別の出来事は教訓に入れないこと。\n"
    "- 教訓は今後の出力にそのまま使える一般的な do/don't 文にし、1件120字以内にすること。個別の出来事の要約は教訓ではない。\n"
    "- 根拠が弱い（少数例からしか言えない）教訓は出さないこと。該当がなければ空配列 lessons=[] を返すこと。\n"
    "- polarity は避けるべき振る舞いなら \"avoid\"、採るべき振る舞いなら \"prefer\"。\n"
    "- evidence_neg / evidence_pos はその教訓の根拠にした負例・正例の件数（整数）。"
)


def is_lesson_extraction_enabled() -> bool:
    """LESSON_EXTRACTION_ENABLED（既定 True）と ACTIVITY_LOG_ENABLED の両方が有効なときのみ True。"""
    try:
        return bool(cfg_bool("LESSON_EXTRACTION_ENABLED", DEFAULT_LESSON_EXTRACTION_ENABLED)) and bool(
            cfg_bool("ACTIVITY_LOG_ENABLED", True)
        )
    except Exception:
        return False


def is_lesson_feedback_material_enabled() -> bool:
    """LESSON_FEEDBACK_MATERIAL_ENABLED（既定 True）。False 時は reply_feedback を従来の補強のみに留める。"""
    try:
        return bool(cfg_bool("LESSON_FEEDBACK_MATERIAL_ENABLED", DEFAULT_LESSON_FEEDBACK_MATERIAL_ENABLED))
    except Exception:
        return DEFAULT_LESSON_FEEDBACK_MATERIAL_ENABLED


def lesson_feedback_max_examples() -> int:
    """reply_feedback 由来の正式材料の上限件数（既定 10）。"""
    return max(cfg_int("LESSON_FEEDBACK_MAX_EXAMPLES", DEFAULT_LESSON_FEEDBACK_MAX_EXAMPLES), 0)


def lesson_window_days() -> int:
    return max(cfg_int("LESSON_WINDOW_DAYS", DEFAULT_LESSON_WINDOW_DAYS), 1)


def lesson_min_evidence() -> int:
    return max(cfg_int("LESSON_MIN_EVIDENCE", DEFAULT_LESSON_MIN_EVIDENCE), 1)


def lesson_max_per_run() -> int:
    return max(cfg_int("LESSON_MAX_PER_RUN", DEFAULT_LESSON_MAX_PER_RUN), 1)


def lesson_extract_interval_sec() -> int:
    return max(cfg_int("LESSON_EXTRACT_INTERVAL_SEC", DEFAULT_LESSON_EXTRACT_INTERVAL_SEC), 60)


def resolve_lesson_model() -> str:
    """抽出モデル（middle 系）を config の OLLAMA_MIDDLE_MODEL 経由で解決する（§140 準拠）。"""
    try:
        return str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
    except Exception:
        return ""


def _is_conversation_active() -> bool:
    """§133 会話活動レジストリで会話継続中か判定する（参照不可なら False = fail-open）。"""
    try:
        from harness.agent.prompt_parser.prompt_manager.conversation_activity import (
            is_conversation_active,
        )

        window_sec = cfg_float("CONVERSATION_ACTIVE_WINDOW_SEC", 300.0)
        return bool(is_conversation_active(window_sec=window_sec))
    except Exception as e:
        log.warning("[LESSON] conversation registry unavailable; continuing (fail-open): %r", e)
        return False

def _parse_created_at(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        val = str(item or "").strip()
        if val and val not in out:
            out.append(val)
    return out


def _parse_nonneg_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


async def _search(memory_store: Any, **kwargs: Any) -> list[dict[str, Any]]:
    search_fn = getattr(memory_store, "search_bot_activities", None)
    if not callable(search_fn):
        return []
    rows = search_fn(**kwargs)
    if inspect.isawaitable(rows):
        rows = await rows
    if not isinstance(rows, (list, tuple)):
        return []
    return [dict(r) for r in rows if isinstance(r, dict)]


def _normalize_lesson_text(text: str) -> str:
    nfkc = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return re.sub(r"[\s　「」『』\"'.,。、！？!?・:：;；()（）\[\]【】\-ー]+", "", nfkc)


def _normalize_material_text(text: str) -> str:
    norm = _normalize_lesson_text(text)
    if norm:
        return norm
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip().lower()


def _classify_feedback_side(signals: Sequence[str]) -> str | None:
    """reply_feedback の signals を 'neg' / 'pos' / None に分類する（苦情が優先）。"""
    sig_set = {str(s or "").strip().lower() for s in signals if str(s or "").strip()}
    if not sig_set:
        return None
    if sig_set & FEEDBACK_NEGATIVE_SIGNALS:
        return "neg"
    if sig_set == {FEEDBACK_POSITIVE_SIGNAL}:
        return "pos"
    return None


def _append_unique(bucket: list[str], values: Sequence[str]) -> None:
    for v in values:
        val = str(v or "").strip()
        if val and val not in bucket:
            bucket.append(val)


def _fallback_similarity(n1: str, n2: str) -> float:
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    if len(n1) >= 8 and len(n2) >= 8 and (n1 in n2 or n2 in n1):
        return 0.95
    bg1 = {n1[i : i + 2] for i in range(len(n1) - 1)} if len(n1) >= 2 else {n1}
    bg2 = {n2[i : i + 2] for i in range(len(n2) - 1)} if len(n2) >= 2 else {n2}
    common = bg1 & bg2
    return (2.0 * len(common)) / (len(bg1) + len(bg2)) if (bg1 and bg2) else 0.0


def calculate_lesson_similarity(text1: str, text2: str) -> float:
    """正規化済み教訓文の類似度（0.0〜1.0）。calculate_utterance_similarity を優先使用する。"""
    t1 = unicodedata.normalize("NFKC", str(text1 or "")).strip()
    t2 = unicodedata.normalize("NFKC", str(text2 or "")).strip()
    if not t1 or not t2:
        return 0.0
    n1 = _normalize_lesson_text(t1)
    n2 = _normalize_lesson_text(t2)
    if n1 and n1 == n2:
        return 1.0
    try:
        from .proactive import calculate_utterance_similarity

        return float(calculate_utterance_similarity(t1, t2))
    except Exception:
        return _fallback_similarity(n1, n2)

def _row_matches_scope_base(
    row: dict[str, Any], *, persona: str, front: str, since_ts: float
) -> bool:
    row_persona = str(row.get("persona") or "").strip()
    if row_persona and row_persona != persona:
        return False
    row_front = str(row.get("front") or "").strip().lower()
    if row_front and row_front != front:
        return False
    ts = _parse_created_at(row.get("created_at"))
    if ts > 0.0 and ts < since_ts:
        return False
    return True


def _index_reply_evals(
    rows: list[dict[str, Any]], *, persona: str, front: str, since_ts: float
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    by_summary: dict[str, dict[str, Any]] = {}
    by_ref_summary: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("kind") or "").strip().lower() != "reply_eval":
            continue
        if not _row_matches_scope_base(row, persona=persona, front=front, since_ts=since_ts):
            continue
        summary = str(row.get("summary") or "").strip()
        detail = _parse_json_dict(row.get("detail"))
        stance = str(detail.get("stance") or "").strip().lower()
        labels = _parse_str_list(detail.get("labels"))
        if not stance and not labels:
            continue
        target_ref = str(detail.get("target_ref") or "").strip()
        info: dict[str, Any] = {
            "stance": stance,
            "labels": labels,
            "target_ref": target_ref,
        }
        if summary and summary not in by_summary:
            by_summary[summary] = info
        if target_ref and summary and (target_ref, summary) not in by_ref_summary:
            by_ref_summary[(target_ref, summary)] = info
    return by_summary, by_ref_summary


def _merge_discord_feedback_materials(
    raw_fb_rows: list[tuple[float, int, str, str, list[str], str, str]],
    *,
    neg: list[dict[str, Any]],
    pos: list[dict[str, Any]],
    eval_by_summary: dict[str, dict[str, Any]],
    eval_by_ref: dict[tuple[str, str], dict[str, Any]],
    max_examples: int,
) -> None:
    """target_text を持つ reply_feedback をゲート重複排除のうえ neg/pos へ組み込む。"""
    gate_by_norm: dict[str, list[dict[str, Any]]] = {}
    for gate_item in (*neg, *pos):
        norm = _normalize_material_text(gate_item["summary"])
        if norm:
            gate_by_norm.setdefault(norm, []).append(gate_item)

    sorted_rows = sorted(raw_fb_rows, key=lambda x: (x[0], x[1]), reverse=True)
    independent: list[tuple[str, dict[str, Any]]] = []
    for ts, _, side, target_text, sigs, reaction, ref in sorted_rows:
        norm_target = _normalize_material_text(target_text)
        matched_gates = gate_by_norm.get(norm_target) if norm_target else None
        if matched_gates:
            for gate_item in matched_gates:
                _append_unique(gate_item["feedback_signals"], sigs)
                if reaction:
                    _append_unique(gate_item["feedback_reactions"], [reaction])
            continue
        ev = (eval_by_ref.get((ref, target_text)) if ref else None) or eval_by_summary.get(target_text)
        fb_item: dict[str, Any] = {
            "source": "reply_feedback",
            "summary": target_text,
            "flags": [],
            "action": "",
            "eval_stance": ev["stance"] if ev else "",
            "eval_labels": ev["labels"] if ev else [],
            "feedback_signals": list(sigs),
            "feedback_reactions": [reaction] if reaction else [],
            "ts": ts,
        }
        independent.append((side, fb_item))

    for side, fb_item in independent[:max_examples]:
        if side == "neg":
            neg.append(fb_item)
        else:
            pos.append(fb_item)


async def collect_discord_materials(
    memory_store: Any,
    *,
    persona: str,
    since_iso: str,
    since_ts: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """discord_reply 用の対照材料（gate_incident/gate_pass + 改訂1 reply_feedback）を収集する。"""
    inc_rows = await _search(
        memory_store, persona=persona, fronts=["discord"], kinds=["gate_incident"], since=since_iso, limit=_SCAN_LIMIT
    )
    pass_rows = await _search(
        memory_store, persona=persona, fronts=["discord"], kinds=["gate_pass"], since=since_iso, limit=_SCAN_LIMIT
    )
    fb_rows = await _search(
        memory_store, persona=persona, fronts=["discord"], kinds=["reply_feedback"], since=since_iso, limit=_SCAN_LIMIT
    )
    eval_rows = await _search(
        memory_store, persona=persona, fronts=["discord"], kinds=["reply_eval"], since=since_iso, limit=_SCAN_LIMIT
    )
    eval_by_summary, eval_by_ref = _index_reply_evals(eval_rows, persona=persona, front="discord", since_ts=since_ts)
    fb_material_enabled = is_lesson_feedback_material_enabled()

    fb_by_ref: dict[str, list[str]] = {}
    raw_fb_materials: list[tuple[float, int, str, str, list[str], str, str]] = []
    for row in fb_rows:
        if str(row.get("kind") or "").strip().lower() != "reply_feedback":
            continue
        if not _row_matches_scope_base(row, persona=persona, front="discord", since_ts=since_ts):
            continue
        det = _parse_json_dict(row.get("detail"))
        ref = str(det.get("target_ref") or "").strip()
        sigs = _parse_str_list(det.get("signals"))
        raw_target = det.get("target_text")
        target_text = raw_target.strip() if isinstance(raw_target, str) else ""
        if fb_material_enabled and target_text:
            side = _classify_feedback_side(sigs)
            if side is not None:
                reaction = str(row.get("summary") or "").strip()
                raw_fb_materials.append((
                    _parse_created_at(row.get("created_at")),
                    int(row.get("id") or 0),
                    side,
                    target_text,
                    sigs,
                    reaction,
                    ref,
                ))
                continue
        if ref and sigs:
            _append_unique(fb_by_ref.setdefault(ref, []), sigs)

    def _to_item(row: dict[str, Any], *, expected_kind: str, flag_key: str) -> dict[str, Any] | None:
        if str(row.get("kind") or "").strip().lower() != expected_kind:
            return None
        if not _row_matches_scope_base(row, persona=persona, front="discord", since_ts=since_ts):
            return None
        summary = str(row.get("summary") or "").strip()
        if not summary:
            return None
        det = _parse_json_dict(row.get("detail"))
        ev = eval_by_summary.get(summary)
        ref = str(row.get("source_ref") or "").strip() or (ev["target_ref"] if ev else "")
        return {
            "source": "gate",
            "summary": summary,
            "flags": _parse_str_list(det.get(flag_key)),
            "action": str(det.get("action") or "").strip(),
            "eval_stance": ev["stance"] if ev else "",
            "eval_labels": ev["labels"] if ev else [],
            "feedback_signals": list(fb_by_ref.get(ref, [])) if ref else [],
            "feedback_reactions": [],
            "ts": _parse_created_at(row.get("created_at")),
        }

    neg = [it for r in inc_rows if (it := _to_item(r, expected_kind="gate_incident", flag_key="flags")) is not None]
    pos = [it for r in pass_rows if (it := _to_item(r, expected_kind="gate_pass", flag_key="risk_flags")) is not None]
    if raw_fb_materials:
        _merge_discord_feedback_materials(
            raw_fb_materials,
            neg=neg,
            pos=pos,
            eval_by_summary=eval_by_summary,
            eval_by_ref=eval_by_ref,
            max_examples=lesson_feedback_max_examples(),
        )
    neg.sort(key=lambda x: x["ts"], reverse=True)
    pos.sort(key=lambda x: x["ts"], reverse=True)
    return neg, pos

def _index_hutaba_feedbacks(
    rows: list[dict[str, Any]], *, persona: str, since_ts: float
) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if str(row.get("kind") or "").strip().lower() != "post_feedback":
            continue
        if not _row_matches_scope_base(row, persona=persona, front="hutaba", since_ts=since_ts):
            continue
        det = _parse_json_dict(row.get("detail"))
        url = str(row.get("source_ref") or "").strip() or str(det.get("url") or "").strip()[:200]
        post_no = str(det.get("post_no") or "").strip()
        sigs = _parse_str_list(det.get("signals"))
        if url and post_no and sigs:
            bucket = out.setdefault((url, post_no), [])
            for s in sigs:
                if s not in bucket:
                    bucket.append(s)
    return out


async def collect_hutaba_materials(
    memory_store: Any,
    *,
    persona: str,
    since_iso: str,
    since_ts: float,
    now_ts: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """hutaba_post 用の対照材料を post_outcome の最新スナップショットから収集する。"""
    outcome_rows = await _search(
        memory_store, persona=persona, fronts=["hutaba"], kinds=["post_outcome"], since=since_iso, limit=_SCAN_LIMIT
    )
    aux_rows = await _search(
        memory_store,
        persona=persona,
        fronts=["hutaba"],
        kinds=["reply_eval", "post_feedback"],
        since=since_iso,
        limit=_SCAN_LIMIT,
    )
    eval_by_summary, eval_by_ref = _index_reply_evals(aux_rows, persona=persona, front="hutaba", since_ts=since_ts)
    fb_by_key = _index_hutaba_feedbacks(aux_rows, persona=persona, since_ts=since_ts)

    groups: dict[tuple[str, str], list[tuple[float, int, dict[str, Any], dict[str, Any]]]] = {}
    for row in outcome_rows:
        if str(row.get("kind") or "").strip().lower() != "post_outcome":
            continue
        if not _row_matches_scope_base(row, persona=persona, front="hutaba", since_ts=since_ts):
            continue
        det = _parse_json_dict(row.get("detail"))
        url = str(row.get("source_ref") or "").strip() or str(det.get("url") or "").strip()[:200]
        post_no = str(det.get("post_no") or "").strip()
        if not url or not post_no:
            continue
        ts = _parse_created_at(row.get("created_at"))
        groups.setdefault((url, post_no), []).append((ts, int(row.get("id") or 0), row, det))

    neg: list[dict[str, Any]] = []
    pos: list[dict[str, Any]] = []
    for (url, post_no), entries in groups.items():
        latest_ts, _, latest_row, latest_det = max(entries, key=lambda x: (x[0], x[1]))
        valid_ts = [e[0] for e in entries if e[0] > 0.0]
        first_ts = min(valid_ts) if valid_ts else 0.0
        soudane = _parse_nonneg_int(latest_det.get("soudane"))
        reply_count = _parse_nonneg_int(latest_det.get("reply_count"))
        summary = str(latest_row.get("summary") or "").strip()
        if soudane is None or reply_count is None or not summary:
            continue
        ev = eval_by_ref.get((url, summary)) or eval_by_summary.get(summary)
        item = {
            "summary": summary,
            "soudane": soudane,
            "reply_count": reply_count,
            "eval_stance": ev["stance"] if ev else "",
            "eval_labels": ev["labels"] if ev else [],
            "feedback_signals": fb_by_key.get((url, post_no), []),
            "ts": latest_ts,
        }
        if soudane >= HUTABA_POS_MIN_SOUDANE or reply_count >= HUTABA_POS_MIN_REPLIES:
            pos.append(item)
        elif soudane == 0 and reply_count == 0 and first_ts > 0.0 and (now_ts - first_ts) >= HUTABA_NEG_MIN_AGE_SEC:
            neg.append(item)
    neg.sort(key=lambda x: x["ts"], reverse=True)
    pos.sort(key=lambda x: x["ts"], reverse=True)
    return neg, pos

def _allowed_polarities(neg_count: int, pos_count: int, min_evidence: int) -> tuple[str, ...]:
    allowed: list[str] = []
    if neg_count >= min_evidence and pos_count >= 1:
        allowed.append("avoid")
    if pos_count >= min_evidence and neg_count >= 1:
        allowed.append("prefer")
    return tuple(allowed)


def _select_prompt_items(items: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """プロンプト投入件数を cap に抑えつつ、ゲート材料とフィードバック材料が互いに押し出されないように選ぶ。"""
    if len(items) <= cap:
        return list(items)
    fb = [it for it in items if it.get("source") == "reply_feedback"]
    non_fb = [it for it in items if it.get("source") != "reply_feedback"]
    if not fb or not non_fb:
        return list(items[:cap])
    gate_quota = min(len(non_fb), max(cap - len(fb), max(1, cap // 3)))
    fb_quota = min(len(fb), cap - gate_quota)
    selected = non_fb[:gate_quota] + fb[:fb_quota]
    if len(selected) < cap:
        rem = cap - len(selected)
        selected.extend(non_fb[gate_quota : gate_quota + rem])
    if len(selected) < cap:
        rem = cap - len(selected)
        selected.extend(fb[fb_quota : fb_quota + rem])
    selected.sort(key=lambda x: x["ts"], reverse=True)
    return selected[:cap]


def _format_example_line(idx: int, scope: str, item: dict[str, Any], *, is_neg: bool) -> str:
    text = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()[:200]
    parts = [f"{idx}. {text}"]
    if scope == "discord_reply":
        if item.get("source") == "reply_feedback":
            parts.append("source=reply_feedback")
        else:
            label = "flags" if is_neg else "risk_flags"
            flags = item.get("flags") or []
            parts.append(f"{label}={','.join(flags) if flags else 'none'}")
            if is_neg and item.get("action"):
                parts.append(f"action={item['action']}")
    else:
        parts.append(f"soudane={item.get('soudane', 0)}, reply_count={item.get('reply_count', 0)}")
    if item.get("eval_stance") or item.get("eval_labels"):
        ev_bits: list[str] = []
        if item.get("eval_stance"):
            ev_bits.append(f"stance={item['eval_stance']}")
        if item.get("eval_labels"):
            ev_bits.append(f"labels={','.join(item['eval_labels'])}")
        parts.append(f"eval({' '.join(ev_bits)})")
    if item.get("feedback_signals"):
        parts.append(f"feedback={','.join(item['feedback_signals'])}")
    reactions = [
        re.sub(r"\s+", " ", str(r)).strip()[:150]
        for r in (item.get("feedback_reactions") or [])
        if str(r or "").strip()
    ]
    if reactions:
        parts.append(f"human_reaction={' / '.join(reactions[:3])}")
    return " | ".join(parts)


def _build_user_prompt(
    scope: str,
    neg_items: list[dict[str, Any]],
    pos_items: list[dict[str, Any]],
    *,
    allowed_polarities: tuple[str, ...],
    min_evidence: int,
    max_lessons: int,
) -> str:
    scope_desc = "Discord返信候補" if scope == "discord_reply" else "ふたば投稿"
    neg_lines = "\n".join(_format_example_line(i + 1, scope, it, is_neg=True) for i, it in enumerate(neg_items))
    pos_lines = "\n".join(_format_example_line(i + 1, scope, it, is_neg=False) for i, it in enumerate(pos_items))
    return (
        f"対象領域: {scope}（{scope_desc}）\n"
        f"抽出許可 polarity: {', '.join(allowed_polarities)} / 最低根拠件数: {min_evidence}件以上 / 最大件数: {max_lessons}件\n\n"
        f"【負例（事故・不発: {len(neg_items)}件）】\n{neg_lines}\n\n"
        f"【正例（通過・好成果: {len(pos_items)}件）】\n{pos_lines}\n\n"
        '出力形式: {"lessons": [{"lesson": "...", "polarity": "avoid|prefer", "evidence_neg": 3, "evidence_pos": 1}]}'
    )

def _normalize_extracted_lessons(
    result: Any,
    *,
    allowed_polarities: tuple[str, ...],
    min_evidence: int,
    max_neg: int,
    max_pos: int,
) -> list[dict[str, Any]] | None:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if not isinstance(result, dict):
        return None
    raw_lessons = result.get("lessons")
    if not isinstance(raw_lessons, list):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw_lessons:
        if not isinstance(entry, dict):
            continue
        lesson = re.sub(r"\s+", " ", str(entry.get("lesson") or "")).strip()[:_SUMMARY_MAX_CHARS]
        polarity = str(entry.get("polarity") or "").strip().lower()
        ev_neg = _parse_nonneg_int(entry.get("evidence_neg"))
        ev_pos = _parse_nonneg_int(entry.get("evidence_pos"))
        if not lesson or polarity not in allowed_polarities or ev_neg is None or ev_pos is None:
            continue
        ev_neg = min(ev_neg, max_neg)
        ev_pos = min(ev_pos, max_pos)
        if polarity == "avoid" and (ev_neg < min_evidence or ev_pos < 1):
            continue
        if polarity == "prefer" and (ev_pos < min_evidence or ev_neg < 1):
            continue
        out.append({"lesson": lesson, "polarity": polarity, "evidence_neg": ev_neg, "evidence_pos": ev_pos})
    return out


async def _extract_lessons_for_scope(
    scope: str,
    neg_items: list[dict[str, Any]],
    pos_items: list[dict[str, Any]],
    *,
    allowed_polarities: tuple[str, ...],
    min_evidence: int,
    max_lessons: int,
    model: str,
    call_json: CallJsonFn,
) -> list[dict[str, Any]] | None:
    prompt = _build_user_prompt(
        scope, neg_items, pos_items,
        allowed_polarities=allowed_polarities, min_evidence=min_evidence, max_lessons=max_lessons,
    )
    try:
        result = await call_json(
            prompt,
            system_prompt=LESSON_EXTRACT_SYSTEM_PROMPT,
            schema=_LESSON_EXTRACT_SCHEMA,
            model=model,
            think=False,
            temperature=0.0,
            timeout_sec=max(cfg_float("LESSON_EXTRACT_TIMEOUT_SEC", DEFAULT_LESSON_EXTRACT_TIMEOUT_SEC), 10.0),
            num_predict=max(cfg_int("LESSON_EXTRACT_NUM_PREDICT", DEFAULT_LESSON_EXTRACT_NUM_PREDICT), 128),
            retries=0,
        )
    except Exception as e:
        log.warning("[LESSON] extraction failed; will retry next run: scope=%s err=%r", scope, e)
        return None
    normalized = _normalize_extracted_lessons(
        result, allowed_polarities=allowed_polarities, min_evidence=min_evidence,
        max_neg=len(neg_items), max_pos=len(pos_items),
    )
    if normalized is None:
        log.warning("[LESSON] unparsable extraction result; will retry next run: scope=%s", scope)
    return normalized

async def _load_existing_lesson_texts(
    memory_store: Any,
    *,
    persona: str,
    scope: str,
    front: str,
    current: datetime,
) -> list[str]:
    since_90d = (current - timedelta(days=LESSON_DEDUP_WINDOW_DAYS)).replace(microsecond=0)
    since_iso = since_90d.isoformat()
    since_ts = since_90d.timestamp()
    rows = await _search(memory_store, persona=persona, kinds=[LESSON_KIND], since=since_iso, limit=_SCAN_LIMIT)
    texts: list[str] = []
    for row in rows:
        if str(row.get("kind") or "").strip().lower() != LESSON_KIND:
            continue
        row_persona = str(row.get("persona") or "").strip()
        if row_persona and row_persona != persona:
            continue
        ts = _parse_created_at(row.get("created_at"))
        if ts > 0.0 and ts < since_ts:
            continue
        det = _parse_json_dict(row.get("detail"))
        row_scope = str(det.get("scope") or "").strip()
        if row_scope:
            if row_scope != scope:
                continue
        else:
            row_front = str(row.get("front") or "").strip().lower()
            if row_front and row_front != front:
                continue
        summary = str(row.get("summary") or "").strip()
        if summary:
            texts.append(summary)
    return texts


def _is_duplicate_lesson(candidate: str, existing_texts: Sequence[str]) -> bool:
    for prev in existing_texts:
        if calculate_lesson_similarity(candidate, prev) >= LESSON_SIMILARITY_THRESHOLD:
            return True
    return False


async def _record_lesson(
    memory_store: Any,
    *,
    persona: str,
    front: str,
    scope: str,
    polarity: str,
    lesson: str,
    evidence_neg: int,
    evidence_pos: int,
    feedback_neg: int = 0,
    feedback_pos: int = 0,
    window_days: int,
) -> None:
    add_fn = getattr(memory_store, "add_bot_activity", None)
    if not callable(add_fn):
        raise RuntimeError("memory_store has no add_bot_activity")
    clean_summary = str(lesson or "").strip()[:_SUMMARY_MAX_CHARS]
    detail = json.dumps(
        {
            "scope": scope,
            "polarity": polarity,
            "evidence_neg": int(evidence_neg),
            "evidence_pos": int(evidence_pos),
            "feedback_neg": int(feedback_neg),
            "feedback_pos": int(feedback_pos),
            "window_days": int(window_days),
        },
        ensure_ascii=False,
    )[:_DETAIL_MAX_CHARS]
    res = add_fn(
        persona=persona,
        front=front,
        kind=LESSON_KIND,
        summary=clean_summary,
        detail=detail,
        source_ref=LESSON_SOURCE_REF,
    )
    if inspect.isawaitable(res):
        await res
    log.info("[LESSON] stored: scope=%s polarity=%s lesson=%s", scope, polarity, clean_summary)

async def run_lesson_extraction_once(
    memory_store: Any,
    *,
    personas: Sequence[str] | None = None,
    call_json: CallJsonFn | None = None,
    now: datetime | None = None,
) -> int:
    """1回分の対照レッスン抽出を実行し、保存した lesson の件数を返す（例外は外へ出さない）。"""
    stored_total = 0
    try:
        if not is_lesson_extraction_enabled() or memory_store is None:
            return 0
        if _is_conversation_active():
            log.info("[LESSON] skipped: reason=conversation_active")
            return 0
        model = resolve_lesson_model()
        if not model:
            log.info("[LESSON] skipped: reason=no_middle_model")
            return 0

        current = now or datetime.now(timezone.utc)
        window_days = lesson_window_days()
        min_ev = lesson_min_evidence()
        max_per_run = lesson_max_per_run()
        since_dt = (current - timedelta(days=window_days)).replace(microsecond=0)
        since_iso = since_dt.isoformat()
        since_ts = since_dt.timestamp()
        now_ts = current.timestamp()
        fn: CallJsonFn = call_json or call_ollama_json

        raw_personas = [cfg_persona_namespace(custom_cfg=cfg)] if personas is None else list(personas)
        target_personas = list(dict.fromkeys(str(p or "").strip() for p in raw_personas if str(p or "").strip()))
        cap = max(_MAX_PROMPT_EXAMPLES_PER_SIDE, min_ev)
        step = 0
        for persona in target_personas:
            for scope, front in SCOPES:
                if stored_total >= max_per_run:
                    return stored_total
                if step > 0 and _is_conversation_active():
                    log.info("[LESSON] stopped: reason=conversation_active")
                    return stored_total
                step += 1
                if scope == "discord_reply":
                    neg_items, pos_items = await collect_discord_materials(
                        memory_store, persona=persona, since_iso=since_iso, since_ts=since_ts
                    )
                else:
                    neg_items, pos_items = await collect_hutaba_materials(
                        memory_store, persona=persona, since_iso=since_iso, since_ts=since_ts, now_ts=now_ts
                    )
                allowed = _allowed_polarities(len(neg_items), len(pos_items), min_ev)
                if not allowed:
                    log.debug(
                        "[LESSON] skipped scope=%s persona=%s: insufficient evidence (neg=%d pos=%d min=%d)",
                        scope, persona, len(neg_items), len(pos_items), min_ev,
                    )
                    continue
                prompt_neg = _select_prompt_items(neg_items, cap)
                prompt_pos = _select_prompt_items(pos_items, cap)
                extracted = await _extract_lessons_for_scope(
                    scope, prompt_neg, prompt_pos,
                    allowed_polarities=allowed, min_evidence=min_ev,
                    max_lessons=max_per_run - stored_total, model=model, call_json=fn,
                )
                if not extracted:
                    continue
                fb_neg_pool = sum(1 for it in prompt_neg if it.get("source") == "reply_feedback")
                fb_pos_pool = sum(1 for it in prompt_pos if it.get("source") == "reply_feedback")
                existing = await _load_existing_lesson_texts(
                    memory_store, persona=persona, scope=scope, front=front, current=current
                )
                for item in extracted:
                    if stored_total >= max_per_run:
                        break
                    if _is_duplicate_lesson(item["lesson"], existing):
                        log.debug("[LESSON] skipped duplicate: scope=%s lesson=%s", scope, item["lesson"])
                        continue
                    await _record_lesson(
                        memory_store, persona=persona, front=front, scope=scope,
                        polarity=item["polarity"], lesson=item["lesson"],
                        evidence_neg=item["evidence_neg"], evidence_pos=item["evidence_pos"],
                        feedback_neg=min(fb_neg_pool, int(item["evidence_neg"])),
                        feedback_pos=min(fb_pos_pool, int(item["evidence_pos"])),
                        window_days=window_days,
                    )
                    existing.append(item["lesson"])
                    stored_total += 1
        return stored_total
    except Exception as e:
        log.warning("[LESSON] run failed (fail-open): %r", e)
        return stored_total

async def maybe_run_lesson_extraction(owner: Any, *, now_ts: float | None = None) -> None:
    """定期ループから呼ばれる入口。LESSON_EXTRACT_INTERVAL_SEC 経過時のみ1回分を背景実行する。"""
    try:
        if not is_lesson_extraction_enabled():
            return
        current = time.time() if now_ts is None else float(now_ts)
        last = float(getattr(owner, "_lesson_extract_last_run_ts", 0.0) or 0.0)
        if last > 0.0 and (current - last) < lesson_extract_interval_sec():
            return
        busy_fn = getattr(owner, "_is_user_reply_busy", None)
        if callable(busy_fn) and busy_fn():
            log.info("[LESSON] skipped: reason=user_reply_busy")
            return
        if _is_conversation_active():
            log.info("[LESSON] skipped: reason=conversation_active")
            return
        owner._lesson_extract_last_run_ts = current
        coro = run_lesson_extraction_once(getattr(owner, "memory_store", None))
        runner = getattr(owner, "_run_cancellable_background_task", None)
        try:
            if callable(runner):
                res = runner(coro)
                if inspect.isawaitable(res):
                    await res
            else:
                await coro
        finally:
            coro.close()
    except Exception as e:
        log.warning("[LESSON] scheduler failed (fail-open): %r", e)


__all__ = [
    "LESSON_KIND",
    "LESSON_SOURCE_REF",
    "SCOPES",
    "POLARITIES",
    "FEEDBACK_NEGATIVE_SIGNALS",
    "FEEDBACK_POSITIVE_SIGNAL",
    "LESSON_SIMILARITY_THRESHOLD",
    "is_lesson_extraction_enabled",
    "is_lesson_feedback_material_enabled",
    "lesson_window_days",
    "lesson_min_evidence",
    "lesson_max_per_run",
    "lesson_extract_interval_sec",
    "lesson_feedback_max_examples",
    "resolve_lesson_model",
    "calculate_lesson_similarity",
    "collect_discord_materials",
    "collect_hutaba_materials",
    "run_lesson_extraction_once",
    "maybe_run_lesson_extraction",
]
