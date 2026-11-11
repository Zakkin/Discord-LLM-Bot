"""危険信号判定および中位モデルによる意味レベル返信ガード"""
from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any, Awaitable, Callable

from ollama_bot.common.config_helpers import cfg, cfg_bool

log = logging.getLogger("front.discord.reply.semantic_guard")

_ACTOR_ATTRIBUTION_RE = re.compile(
    r"(?:に(?:奪わ|取ら|言わ|教え|もら|され)|が(?:持って|言っ|やっ|奪っ)た)"
)
_NAME_PREFIX_ACTOR_RE = re.compile(
    r"([ぁ-んァ-ヶー\u4e00-\u9fa5a-zA-Z0-9_]{2,})(?:に(?:奪わ|取ら|言わ|教え|もら|され)|が(?:持って|言っ|やっ|奪っ)た)"
)


def assess_reply_risk(
    *,
    reply_text: str,
    intent_info: dict[str, Any] | None = None,
    denied_claims: list[dict[str, str]] | None = None,
    previous_reply_text: str | None = None,
    context_lines: list[str] | None = None,
    current_speaker: str | None = None,
    bot_name: str | None = None,
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

    # 4. 行為者帰属リスク検査
    if cfg_bool("ACTOR_CHECK_ENABLED", True) and (context_lines or reply_text):
        speakers: set[str] = set()
        if context_lines:
            for cl in context_lines:
                cl_clean = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", str(cl).strip())
                if ":" in cl_clean:
                    spk_part = cl_clean.split(":", 1)[0].strip()
                    spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", spk_part).strip()
                    if spk and spk not in ("System", "system", "Assistant", "assistant"):
                        speakers.add(spk)

        excluded = {"私", "僕", "俺", "自分", "あなた", "君", "お前"}
        if current_speaker:
            excluded.add(current_speaker.strip())
        if bot_name:
            excluded.add(bot_name.strip())

        third_parties = {s for s in speakers if s and s not in excluded}

        sentences = [s.strip() for s in re.split(r"[。\n！？!?]+", reply_text) if s.strip()]
        actor_hit = False
        for sent in sentences:
            # 1) 文中に会話履歴の第三者名が存在し、かつ過去行為帰属表現がある
            has_tp = any(tp in sent for tp in third_parties)
            if has_tp and _ACTOR_ATTRIBUTION_RE.search(sent):
                actor_hit = True
                break
            # 2) 行為帰属表現の直前に人名/名詞（第三者）が存在する
            m_actor = _NAME_PREFIX_ACTOR_RE.search(sent)
            if m_actor:
                cand_name = m_actor.group(1).strip()
                if cand_name and cand_name not in excluded:
                    actor_hit = True
                    break

        if actor_hit:
            needs_check = True
            reasons.append("actor_attribution_risk")

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
    context_lines: list[str] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
) -> dict[str, bool]:
    """中位モデルで固定JSON意味検査を実行"""
    prompt_parts = [
        f"ユーザー直前発言: {user_text}",
        f"Bot直前発言: {bot_previous_text}",
        f"生成された返信: {generated_reply}",
        f"否定済みリスト: {[c.get('claim') for c in denied_claims]}",
    ]
    if context_lines:
        recent_ctx = context_lines[-10:]
        prompt_parts.append("会話履歴（直近最大10行）:\n" + "\n".join(recent_ctx))
    if recent_events:
        ev_lines = []
        for ev in recent_events[-5:]:
            at_str = f"[{ev.get('at', '')}] " if ev.get("at") else ""
            target_str = f"（相手: {ev.get('target')}）" if ev.get("target") else ""
            ev_lines.append(f"- {at_str}{ev.get('actor', '不明')} が {ev.get('action', '')}{target_str}")
        prompt_parts.append("出来事メモ:\n" + "\n".join(ev_lines))

    prompt_parts.append(
        "以下の4つの項目について検査しJSONで答えなさい:\n"
        "1. reasserts_denied: 否定済みリストの内容を言い換え含めて再主張しているか\n"
        "2. subject_swap: 誰が何をしたか主語や立場を取り違えているか\n"
        "3. contradicts_latest_user: 直近ユーザー発言と矛盾する前提か\n"
        "4. actor_mismatch: 返信が言及した行為者（誰がやったか）が、会話履歴や出来事メモの記録と食い違っているか\n"
        '{"reasserts_denied": false, "subject_swap": false, "contradicts_latest_user": false, "actor_mismatch": false, "ok": true}'
    )
    prompt = "\n".join(prompt_parts)
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
            actor_mismatch = bool(res.get("actor_mismatch"))
            ok_val = bool(res.get("ok")) and not (reasserts or subject_swap or contradicts or actor_mismatch)
            return {
                "reasserts_denied": reasserts,
                "subject_swap": subject_swap,
                "contradicts_latest_user": contradicts,
                "actor_mismatch": actor_mismatch,
                "ok": ok_val,
            }
    except Exception as e:
        log.warning("[SEMANTIC_GUARD] inspection failed (fallback ok=True): %r", e)
    return {"reasserts_denied": False, "subject_swap": False, "contradicts_latest_user": False, "actor_mismatch": False, "ok": True}
