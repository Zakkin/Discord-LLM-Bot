"""§165 第4段階: 返信候補の決定的比較選定ヘルパー（candidate_compare）。"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
import logging
import re
from typing import Any, Awaitable, Callable, Sequence

from ollama_bot.common.ollama_helpers import looks_like_abnormal_assistant_reply

DEFAULT_CANDIDATE_COMPARE_ENABLED = True
DEFAULT_CANDIDATE_COMPARE_MAX_ELAPSED_SEC = 45.0
DEFAULT_CANDIDATE_COUNT = 2
CANDIDATE_COMPARE_SOURCE_REF = "stage4"
CANDIDATE_HEAD_MAX_CHARS = 80
CANDIDATE_DETAIL_MAX_CHARS = 800

@dataclass(frozen=True)
class CandidateEval:
    """候補比較選定における1候補の評価結果（§165）。"""

    index: int
    text: str
    verdict: str
    risk_count: int
    prev_similarity: float


def compute_previous_reply_similarity(candidate_text: str, previous_reply_text: str) -> float:
    """候補文と直前 Bot 返信との類似度（0.0〜1.0）を決定的に算出する（§165）。"""
    cand = str(candidate_text or "").strip()
    prev = str(previous_reply_text or "").strip()
    if not cand or not prev:
        return 0.0
    return float(difflib.SequenceMatcher(None, cand, prev).ratio())


def select_best_candidate(candidates: Sequence[CandidateEval]) -> CandidateEval | None:
    """硬い棄却後の残存候補から決定的に最良の1件を選定する（§165）。

    優先順位:
      (a) risk_flags 数（risk_count）が少ない方
      (b) 直前の Bot 返信との類似度（prev_similarity）が低い方
      (c) 生成順（index）が早い方
    全候補が棄却された場合は None を返す。
    """
    valid = [
        cand
        for cand in candidates
        if cand.verdict == "ok" and bool(str(cand.text or "").strip())
    ]
    if not valid:
        return None
    return min(
        valid,
        key=lambda c: (int(c.risk_count), float(c.prev_similarity), int(c.index)),
    )


def build_alternative_candidate_note(previous_candidates: Sequence[str]) -> str:
    """違反修正ではなく別案を求めるプロンプト注記を組み立てる（§165）。"""
    heads = [
        re.sub(r"\s+", " ", str(text or "").strip())[:CANDIDATE_HEAD_MAX_CHARS]
        for text in previous_candidates
        if str(text or "").strip()
    ]
    prev_hint = ""
    if heads:
        quoted = " / ".join(f"「{h}」" for h in heads[:3])
        prev_hint = f"既存の案（{quoted}）とは異なる切り口や言い回しで、"
    return (
        f"\n\n[別案リクエスト: {prev_hint}"
        "同じ会話の流れとキャラクター性を保ったまま、別の自然な返信案を1つだけ短く作成してください。"
        "既存の案をそのまま繰り返さず、返信本文のみを出力してください。]"
    )


def build_candidate_compare_detail(
    *,
    scope: str,
    chosen: int,
    candidates: Sequence[CandidateEval],
    max_chars: int = CANDIDATE_DETAIL_MAX_CHARS,
) -> str:
    """§1 共有契約の detail JSON（最大800字）を有効な JSON として組み立てる。"""
    def _encode(head_limit: int, verdict_limit: int) -> str:
        items: list[dict[str, Any]] = [
            {
                "head": str(c.text or "").strip()[:head_limit],
                "verdict": str(c.verdict or "")[:verdict_limit],
                "risk": int(c.risk_count),
            }
            for c in candidates
        ]
        payload: dict[str, Any] = {
            "scope": str(scope or "reply"),
            "chosen": int(chosen),
            "candidates": items,
        }
        return json.dumps(payload, ensure_ascii=False)

    encoded = _encode(CANDIDATE_HEAD_MAX_CHARS, 80)
    if len(encoded) <= max_chars:
        return encoded
    for h_lim, v_lim in ((60, 60), (40, 40), (20, 32), (10, 24), (0, 16)):
        compact = json.dumps(
            {
                "scope": str(scope or "reply"),
                "chosen": int(chosen),
                "candidates": [
                    {
                        "head": str(c.text or "").strip()[:h_lim],
                        "verdict": str(c.verdict or "")[:v_lim],
                        "risk": int(c.risk_count),
                    }
                    for c in candidates
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(compact) <= max_chars:
            return compact
    return json.dumps(
        {"scope": str(scope or "reply"), "chosen": int(chosen), "candidates": []},
        ensure_ascii=False,
        separators=(",", ":"),
    )[:max_chars]


def check_candidate_text_health(
    text: str,
    *,
    user_text: str = "",
    logger: logging.Logger | None = None,
) -> str:
    """候補本文が空・崩壊していないか決定的に判定する（正常なら 'ok'、棄却なら理由）。"""
    stripped = str(text or "").strip()
    if not stripped:
        return "empty"
    try:
        if looks_like_abnormal_assistant_reply(stripped, user_text=user_text):
            return "abnormal"
    except Exception as e:
        if logger is not None:
            logger.debug("[CANDIDATE_COMPARE] abnormal reply check skipped (fail-open): %r", e)
    return "ok"


async def run_reply_candidate_compare(
    *,
    first_reply: str,
    first_risk_count: int,
    user_text: str,
    bot_previous_text: str,
    path: str,
    target_count: int,
    is_over_time_fn: Callable[[], bool],
    generate_fn: Callable[[str], Awaitable[str]],
    evaluate_fn: Callable[[str], Awaitable[tuple[bool, list[str], int]]],
    record_fn: Callable[[int, Sequence[CandidateEval], str], Awaitable[None]],
    logger: logging.Logger,
) -> str:
    """意味検査通過後の追加候補生成・評価・決定的選定と台帳記録を実行する（§165, fail-open）。"""
    try:
        candidates: list[CandidateEval] = [
            CandidateEval(
                index=0,
                text=first_reply,
                verdict=check_candidate_text_health(first_reply, user_text=user_text, logger=logger),
                risk_count=int(first_risk_count),
                prev_similarity=compute_previous_reply_similarity(first_reply, bot_previous_text),
            )
        ]
        seen_texts: list[str] = [first_reply]
        for cand_idx in range(1, max(1, int(target_count))):
            if cand_idx > 1 and is_over_time_fn():
                break
            alt_note = build_alternative_candidate_note(seen_texts)
            try:
                alt_raw = await generate_fn(alt_note)
            except Exception as gen_err:
                logger.warning(
                    "[CANDIDATE_COMPARE] additional generation failed (fail-open, path=%s, idx=%d): %r",
                    path, cand_idx, gen_err,
                )
                break
            alt_text = str(alt_raw or "").strip()
            health = check_candidate_text_health(alt_text, user_text=user_text, logger=logger)
            if health != "ok":
                candidates.append(
                    CandidateEval(
                        index=cand_idx, text=alt_text, verdict=health, risk_count=0,
                        prev_similarity=compute_previous_reply_similarity(alt_text, bot_previous_text),
                    )
                )
                continue
            try:
                alt_violated, alt_flags, alt_risk_count = await evaluate_fn(alt_text)
            except Exception as eval_err:
                logger.warning(
                    "[CANDIDATE_COMPARE] additional evaluation failed (fail-open, path=%s, idx=%d): %r",
                    path, cand_idx, eval_err,
                )
                break
            alt_verdict = (
                ",".join(str(f) for f in alt_flags if str(f).strip()) or "violation"
                if alt_violated else "ok"
            )
            candidates.append(
                CandidateEval(
                    index=cand_idx, text=alt_text, verdict=alt_verdict, risk_count=int(alt_risk_count),
                    prev_similarity=compute_previous_reply_similarity(alt_text, bot_previous_text),
                )
            )
            seen_texts.append(alt_text)

        if len(candidates) <= 1:
            return first_reply
        best = select_best_candidate(candidates)
        chosen_idx = best.index if best is not None else -1
        chosen_text = best.text if best is not None else first_reply
        await record_fn(chosen_idx, candidates, chosen_text)
        return chosen_text
    except Exception as e:
        logger.warning("[CANDIDATE_COMPARE] comparison failed (fail-open, path=%s): %r", path, e)
        return first_reply

