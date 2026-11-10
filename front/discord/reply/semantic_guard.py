"""危険信号判定および中位モデルによる意味レベル返信ガード"""
from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any, Awaitable, Callable

from ollama_bot.common.config_helpers import cfg

log = logging.getLogger("front.discord.reply.semantic_guard")


def assess_reply_risk(
    *,
    reply_text: str,
    intent_info: dict[str, Any] | None,
    denied_claims: list[dict[str, str]] | None,
    previous_reply_text: str | None,
) -> dict[str, Any]:
    needs_check = False
    reasons: list[str] = []
    substring_hit: str | None = None

    # 1. 今ターンの訂正がある
    if intent_info and intent_info.get("user_correction"):
        needs_check = True
        reasons.append("turn_has_user_correction")

    # 2. 否定済みレジストリが存在
    claims = denied_claims or []
    if claims:
        needs_check = True
        reasons.append("has_denied_claims")
        # 部分文字列の即時違反チェック（LLM不要）
        norm_reply = re.sub(r"[\s、。！？!?,.]+", "", reply_text).lower()
        for c in claims:
            c_text = c.get("claim", "")
            if len(c_text) >= 8:
                norm_c = re.sub(r"[\s、。！？!?,.]+", "", c_text).lower()
                if norm_c in norm_reply:
                    substring_hit = c_text
                    break

    # 3. 直前返信との類似度
    if previous_reply_text:
        sim = difflib.SequenceMatcher(None, reply_text, previous_reply_text).ratio()
        if sim >= 0.35:
            needs_check = True
            reasons.append(f"similarity_high_{sim:.2f}")

    return {
        "needs_check": needs_check,
        "reasons": reasons,
        "substring_hit": substring_hit,
    }


async def inspect_reply_semantics(
    *,
    llm_caller_fn: Callable[[str], Awaitable[Any]],
    user_text: str,
    bot_previous_text: str,
    generated_reply: str,
    denied_claims: list[dict[str, str]],
) -> dict[str, bool]:
    """中位モデルで固定JSON意味検査を実行"""
    prompt = (
        f"ユーザー直前発言: {user_text}\n"
        f"Bot直前発言: {bot_previous_text}\n"
        f"生成された返信: {generated_reply}\n"
        f"否定済みリスト: {[c.get('claim') for c in denied_claims]}\n"
        "以下の3つの違反がないか検査しJSONで答えなさい:\n"
        "1. reasserts_denied: 否定済みリストの内容を言い換え含めて再主張しているか\n"
        "2. subject_swap: 誰が何をしたか主語や立場を取り違えているか\n"
        "3. contradicts_latest_user: 直近ユーザー発言と矛盾する前提か\n"
        '{"reasserts_denied": false, "subject_swap": false, "contradicts_latest_user": false, "ok": true}'
    )
    try:
        res = await llm_caller_fn(prompt)
        if isinstance(res, str):
            clean_res = res.strip()
            if "```json" in clean_res:
                clean_res = clean_res.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in clean_res:
                clean_res = clean_res.split("```", 1)[1].split("```", 1)[0].strip()
            try:
                res = json.loads(clean_res)
            except Exception:
                pass
        if isinstance(res, dict) and "ok" in res:
            log.info("[SEMANTIC_GUARD] inspection result: %r", res)
            reasserts = bool(res.get("reasserts_denied"))
            subject_swap = bool(res.get("subject_swap"))
            contradicts = bool(res.get("contradicts_latest_user"))
            ok_val = bool(res.get("ok")) and not (reasserts or subject_swap or contradicts)
            return {
                "reasserts_denied": reasserts,
                "subject_swap": subject_swap,
                "contradicts_latest_user": contradicts,
                "ok": ok_val,
            }
    except Exception as e:
        log.warning("[SEMANTIC_GUARD] inspection failed (fallback ok=True): %r", e)
    return {"reasserts_denied": False, "subject_swap": False, "contradicts_latest_user": False, "ok": True}
