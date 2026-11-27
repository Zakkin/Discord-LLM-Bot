"""§167 第4段階: ふたば投稿の候補比較選定（共有契約・記録・順位づけ）。

巨大ファイル規則に従い、選定ロジックと bot_activities（kind="candidate_compare"）
への記録ロジックを img2chan_posting.py から分離して提供する。
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any

from front.hutaba.monitor.img2chan_context import cfg, log
from lib.config_utils import cfg_persona_namespace

discussed_topics: Any
try:
    import harness.agent.prompt_parser.prompt_manager.discussed_topics as _discussed_topics
    discussed_topics = _discussed_topics
except Exception:
    discussed_topics = None

CANDIDATE_COMPARE_KIND = "candidate_compare"
CANDIDATE_COMPARE_FRONT = "hutaba"
CANDIDATE_COMPARE_SCOPE = "proactive_hutaba"
CANDIDATE_COMPARE_DEFAULT_SOURCE_REF = "stage4"
CANDIDATE_COMPARE_DEFAULT_COUNT = 3
CANDIDATE_COMPARE_SUMMARY_MAX_CHARS = 400
CANDIDATE_COMPARE_HEAD_MAX_CHARS = 80
CANDIDATE_COMPARE_DETAIL_MAX_CHARS = 800

_UNSET: Any = object()


def resolve_hutaba_candidate_count(raw_value: Any, *, default: int = CANDIDATE_COMPARE_DEFAULT_COUNT) -> int:
    """HUTABA_CANDIDATE_COUNT の値を 1 以上の整数へ正規化する。"""
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def build_hutaba_compare_context(
    thread: Mapping[str, Any] | None,
    target_reply: Mapping[str, Any] | None = None,
    posts: Sequence[Mapping[str, Any]] | None = None,
    *,
    recent_limit: int = 5,
) -> str:
    """§167 順位づけ用のスレッド文脈（スレタイトル・OP・対象レス・直近レス）を結合する。"""
    parts: list[str] = []
    if isinstance(thread, Mapping):
        for key in ("title", "op_text"):
            val = str(thread.get(key) or "").strip()
            if val:
                parts.append(val)
    if isinstance(target_reply, Mapping):
        t_val = str(target_reply.get("text") or target_reply.get("body") or "").strip()
        if t_val:
            parts.append(t_val)
    recent_source: Sequence[Any] = ()
    if posts:
        recent_source = list(posts)[-recent_limit:]
    elif isinstance(thread, Mapping):
        raw_replies = thread.get("replies")
        if isinstance(raw_replies, Sequence) and not isinstance(raw_replies, (str, bytes)):
            recent_source = list(raw_replies)[-recent_limit:]
    for item in recent_source:
        if isinstance(item, Mapping):
            p_val = str(item.get("text") or item.get("body") or "").strip()
            if p_val:
                parts.append(p_val)
    return " ".join(parts)


def append_hutaba_candidate(
    candidates: list[dict[str, Any]],
    *,
    comment: str,
    verdict: str,
    quote_post_numbers: Sequence[str] = (),
    quote_reply_numbers: Sequence[int] = (),
    model: str = "",
    prompt_variants: list[tuple[str, str]] | None = None,
    variant_idx: int = 0,
    target_count: int = CANDIDATE_COMPARE_DEFAULT_COUNT,
    next_variant: tuple[str, str] | None = None,
) -> None:
    """候補1件の結果を記録し、上限未満なら次スロットの prompt_variant を補充する。"""
    candidates.append(
        {
            "comment": str(comment or ""),
            "verdict": str(verdict or "rejected"),
            "quote_post_numbers": list(quote_post_numbers),
            "quote_reply_numbers": list(quote_reply_numbers),
            "model": str(model or ""),
        }
    )
    if (
        prompt_variants is not None
        and len(candidates) < target_count
        and variant_idx >= len(prompt_variants)
        and next_variant is not None
    ):
        prompt_variants.append(next_variant)


def select_best_hutaba_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    context_text: str,
    discussed_topics_mod: Any = _UNSET,
) -> tuple[int, Mapping[str, Any] | None]:
    """検証通過（verdict=="ok"）の候補から、スレッド文脈との話題語重なり最大の1件を選ぶ。

    同点の場合は生成順（index が小さい方）を優先する。
    discussed_topics が利用不可または例外発生時は生成順の先頭通過候補を返す（fail-open）。
    通過候補が0件の場合は (-1, None) を返す。
    """
    passing: list[tuple[int, Mapping[str, Any]]] = [
        (idx, cand)
        for idx, cand in enumerate(candidates)
        if isinstance(cand, Mapping) and str(cand.get("verdict") or "") == "ok"
    ]
    if not passing:
        return -1, None
    if len(passing) == 1:
        return passing[0]

    dt_mod = discussed_topics if discussed_topics_mod is _UNSET else discussed_topics_mod
    extract_fn = getattr(dt_mod, "extract_topic_terms", None) if dt_mod is not None else None
    overlap_fn = getattr(dt_mod, "topic_overlap", None) if dt_mod is not None else None
    if not callable(extract_fn) or not callable(overlap_fn):
        return passing[0]

    try:
        ctx_terms = extract_fn(str(context_text or ""))
        if not ctx_terms:
            return passing[0]
        best_idx, best_cand = passing[0]
        best_score = -1.0
        for idx, cand in passing:
            cand_terms = extract_fn(str(cand.get("comment") or ""))
            score = float(overlap_fn(cand_terms, ctx_terms))
            if score > best_score:
                best_score = score
                best_idx = idx
                best_cand = cand
        return best_idx, best_cand
    except Exception as exc:
        log.warning(
            "[hutaba] 候補比較の順位計算に失敗したため先頭通過候補を採用します（fail-open）: err=%r",
            exc,
        )
        return passing[0]


def resolve_hutaba_candidate_compare_summary(
    candidates: Sequence[Mapping[str, Any]],
    *,
    chosen: int,
    max_chars: int = CANDIDATE_COMPARE_SUMMARY_MAX_CHARS,
) -> str:
    """共有契約の summary（選定された投稿文、全棄却なら最初の候補、最大400字）を返す。"""
    if 0 <= chosen < len(candidates):
        return str(candidates[chosen].get("comment") or "").strip()[:max_chars]
    if candidates:
        return str(candidates[0].get("comment") or "").strip()[:max_chars]
    return ""


def build_hutaba_candidate_compare_detail(
    candidates: Sequence[Mapping[str, Any]],
    *,
    chosen: int,
    scope: str = CANDIDATE_COMPARE_SCOPE,
    head_max_chars: int = CANDIDATE_COMPARE_HEAD_MAX_CHARS,
    detail_max_chars: int = CANDIDATE_COMPARE_DETAIL_MAX_CHARS,
) -> str:
    """共有契約の detail JSON（最大800字・妥当なJSON保証）を生成する。"""
    for cap in (head_max_chars, 60, 40, 24, 12, 0):
        entries = [
            {
                "head": str(c.get("comment") or "").strip()[:cap],
                "verdict": str(c.get("verdict") or "rejected")[:40],
            }
            for c in candidates
        ]
        payload = {
            "scope": scope,
            "chosen": int(chosen),
            "candidates": entries,
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= detail_max_chars:
            return serialized

    minimal_entries: list[dict[str, str]] = []
    for c in candidates:
        minimal_entries.append({"head": "", "verdict": str(c.get("verdict") or "rejected")[:24]})
        candidate_json = json.dumps(
            {"scope": scope, "chosen": int(chosen), "candidates": minimal_entries},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(candidate_json) > detail_max_chars:
            minimal_entries.pop()
            break
    return json.dumps(
        {"scope": scope, "chosen": int(chosen), "candidates": minimal_entries},
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def record_hutaba_candidate_compare(
    memory_store: Any,
    *,
    candidates: Sequence[Mapping[str, Any]],
    chosen: int,
    url: str = "",
    persona: str | None = None,
    scope: str = CANDIDATE_COMPARE_SCOPE,
) -> bool:
    """候補比較選定の結果を bot_activities（kind="candidate_compare"）へ記録する（fail-open）。"""
    try:
        if memory_store is None or not candidates:
            return False
        add_act_fn: Any = getattr(memory_store, "add_bot_activity", None)
        if not callable(add_act_fn):
            return False
        resolved_persona = (
            str(persona).strip()
            if persona is not None and str(persona).strip()
            else cfg_persona_namespace(custom_cfg=cfg)
        )
        summary = resolve_hutaba_candidate_compare_summary(candidates, chosen=chosen)
        detail = build_hutaba_candidate_compare_detail(candidates, chosen=chosen, scope=scope)
        clean_url = str(url or "").strip()
        source_ref = clean_url[:200] if clean_url else CANDIDATE_COMPARE_DEFAULT_SOURCE_REF
        result = add_act_fn(
            persona=resolved_persona,
            front=CANDIDATE_COMPARE_FRONT,
            kind=CANDIDATE_COMPARE_KIND,
            summary=summary,
            detail=detail,
            guild_id=None,
            channel_id=None,
            source_ref=source_ref,
        )
        if inspect.isawaitable(result):
            await result
        log.info(
            "[CANDIDATE_COMPARE] scope=%s chosen=%d total=%d",
            scope,
            int(chosen),
            len(candidates),
        )
        return True
    except Exception as exc:
        log.warning(
            "[hutaba] 候補比較の記録に失敗しました（fail-open）: url=%s chosen=%s err=%r",
            url,
            chosen,
            exc,
        )
        return False
