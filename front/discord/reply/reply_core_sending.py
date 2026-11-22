"""OllamaChatReplyCoreMixinの返信送信・分割送信・絵文字相槌・直接返信判定を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import inspect
import logging
import random
import re
import sys
from typing import Any, Awaitable, Callable, Sequence, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from ollama_bot.common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    channel_id,
    display_name,
    recent_context_items,
)
from front.discord.common.bot_identity import resolve_bot_identity
import front.discord.turn_budget as _turn_budget_mod
from front.discord.turn_budget import (
    record_turn_budget_skip,
    turn_budget_exceeded,
    turn_budget_skips_recorded,
)
from front.discord.reply import semantic_guard as _semantic_guard_mod
from front.discord.reply.semantic_guard import (
    SEMANTIC_INSPECTION_SCHEMA,
    assess_reply_risk,
    extract_recent_events_from_runtime,
    extract_semantic_violation_flags,
    inspect_reply_semantics,
)
from front.discord.reply.classifier import decide_reply_mode, is_stale_reply
import harness.agent.prompt_parser as _prompt_parser_mod
from harness.agent.prompt_parser import looks_like_character_deviation
from harness.agent.prompt_parser.parser.reply_helpers import _format_reply
from front.discord.reply.reply_core_common import (
    _AIZUCHI_PATTERN,
    _OllamaChatReplyCoreBase,
    _split_reply_text,
    ReplyTargetDeletedError,
    call_ollama_json,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    log,
)

_pick_character_deviation_fallback: Callable[..., Any] | None = None
try:
    import harness.agent.prompt_parser.parser.reply_guards as _reply_guards_mod
    _cand_picker = getattr(_reply_guards_mod, "_pick_character_deviation_fallback", None)
    if callable(_cand_picker):
        _pick_character_deviation_fallback = _cand_picker
except Exception:
    _pick_character_deviation_fallback = None
if _pick_character_deviation_fallback is None:
    _cand_picker_top = getattr(_prompt_parser_mod, "_pick_character_deviation_fallback", None)
    if callable(_cand_picker_top):
        _pick_character_deviation_fallback = _cand_picker_top

_ORIG_ASSESS_REPLY_RISK = assess_reply_risk
_ORIG_INSPECT_REPLY_SEMANTICS = inspect_reply_semantics
_ORIG_LOOKS_LIKE_CHARACTER_DEVIATION = looks_like_character_deviation
_ORIG_TURN_BUDGET_EXCEEDED = turn_budget_exceeded
_ORIG_RECORD_TURN_BUDGET_SKIP = record_turn_budget_skip
_ORIG_TURN_BUDGET_SKIPS_RECORDED = turn_budget_skips_recorded
_ORIG_PICK_CHARACTER_DEVIATION_FALLBACK = _pick_character_deviation_fallback
_ORIG_CFG = cfg
_ORIG_CFG_BOOL = cfg_bool
_ORIG_CFG_FLOAT = cfg_float

NEAR_DUPLICATE_PREVIOUS_RATIO_THRESHOLD = 0.9
_DEFAULT_SEND_GATE_TEXT_FALLBACK = "今のはうまく返せなかった。もう一回言ってくれ。"
_CFG_UNSET_SENTINEL = object()


def _is_turn_budget_exceeded() -> bool:
    fn = (
        turn_budget_exceeded
        if turn_budget_exceeded is not _ORIG_TURN_BUDGET_EXCEEDED
        else getattr(_turn_budget_mod, "turn_budget_exceeded", turn_budget_exceeded)
    )
    return bool(fn())


def _record_budget_skip(step_name: str) -> None:
    fn = (
        record_turn_budget_skip
        if record_turn_budget_skip is not _ORIG_RECORD_TURN_BUDGET_SKIP
        else getattr(_turn_budget_mod, "record_turn_budget_skip", record_turn_budget_skip)
    )
    fn(step_name)


def _is_turn_budget_skips_recorded() -> bool:
    fn = (
        turn_budget_skips_recorded
        if turn_budget_skips_recorded is not _ORIG_TURN_BUDGET_SKIPS_RECORDED
        else getattr(_turn_budget_mod, "turn_budget_skips_recorded", turn_budget_skips_recorded)
    )
    return bool(fn())


def _check_budget_reply_unsendable(
    reply_text: str,
    previous_reply_text: str,
) -> tuple[bool, bool, float]:
    """予算超過時の違反候補が送信不能（空・直前送信文とほぼ同一）かを判定する（§87）。

    Returns:
        (unsendable, near_duplicate_previous, similarity_ratio)
    """
    cand = str(reply_text or "").strip()
    if not cand:
        return True, False, 0.0
    prev = str(previous_reply_text or "").strip()
    if not prev:
        return False, False, 0.0
    ratio = difflib.SequenceMatcher(None, cand, prev).ratio()
    if ratio >= NEAR_DUPLICATE_PREVIOUS_RATIO_THRESHOLD:
        return True, True, ratio
    return False, False, ratio


def _select_fallback_from_raw(raw: Any) -> str:
    if raw is _CFG_UNSET_SENTINEL:
        return str(_DEFAULT_SEND_GATE_TEXT_FALLBACK or "").strip()
    if isinstance(raw, (list, tuple)):
        candidates = [str(item).strip() for item in raw if str(item or "").strip()]
        return random.choice(candidates) if candidates else ""
    return str(raw or "").strip()


def _resolve_send_gate_text_fallback(owner: Any = None) -> str:
    """予算超過で原文も送信不能な場合のテキストフォールバック文面を取得する（§87）。

    取得順:
    1. §84 の _pick_character_deviation_fallback が利用可能なら 1 文を取得
    2. 無ければ config の OLLAMA_CHARACTER_DEVIATION_FALLBACK を参照
    3. 未設定時はハードコード既定文を使用（明示的に空の場合は空文字を返す）
    """
    # テスト等でモジュール変数の _pick_character_deviation_fallback が明示パッチされた場合を最優先
    if _pick_character_deviation_fallback is not _ORIG_PICK_CHARACTER_DEVIATION_FALLBACK:
        if callable(_pick_character_deviation_fallback):
            try:
                return str(_pick_character_deviation_fallback() or "").strip()
            except Exception as e:
                log.warning("[SEND_GATE] patched _pick_character_deviation_fallback failed: %r", e)
    else:
        # テスト等で reply_core_sending.cfg のみパッチされた場合はその設定値を尊重
        if cfg is not _ORIG_CFG:
            raw_patched = cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", _CFG_UNSET_SENTINEL)
            if raw_patched is not _CFG_UNSET_SENTINEL:
                return _select_fallback_from_raw(raw_patched)

        picker: Callable[..., Any] | None = None
        if owner is not None:
            cand = getattr(owner, "_pick_character_deviation_fallback", None)
            if callable(cand):
                picker = cand
        if picker is None:
            rg_mod = sys.modules.get("harness.agent.prompt_parser.parser.reply_guards")
            cand = getattr(rg_mod, "_pick_character_deviation_fallback", None) if rg_mod is not None else None
            if callable(cand):
                picker = cand
        if picker is None:
            pp_mod = sys.modules.get("harness.agent.prompt_parser")
            cand = getattr(pp_mod, "_pick_character_deviation_fallback", None) if pp_mod is not None else None
            if callable(cand):
                picker = cand
        if picker is None and callable(_pick_character_deviation_fallback):
            picker = _pick_character_deviation_fallback

        if picker is not None:
            try:
                return str(picker() or "").strip()
            except Exception as e:
                log.warning("[SEND_GATE] _pick_character_deviation_fallback failed: %r", e)

    raw_cfg = cfg("OLLAMA_CHARACTER_DEVIATION_FALLBACK", _CFG_UNSET_SENTINEL)
    return _select_fallback_from_raw(raw_cfg)


def _gate_cfg_bool(name: str, default: bool = False) -> bool:
    if cfg_bool is not _ORIG_CFG_BOOL:
        return bool(cfg_bool(name, default))
    if cfg is not _ORIG_CFG:
        val = cfg(name, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off"):
                return False
        return bool(val)
    return bool(cfg_bool(name, default))


def _gate_cfg_float(name: str, default: float = 0.0) -> float:
    if cfg_float is not _ORIG_CFG_FLOAT:
        return float(cfg_float(name, default) or default)
    if cfg is not _ORIG_CFG:
        val = cfg(name, default)
        try:
            return float(val if val is not None else default)
        except (TypeError, ValueError):
            return default
    return float(cfg_float(name, default) or default)


def _build_semantic_deliberation_note(
    *,
    violation_reason: str,
    insp: dict[str, bool],
    recent_events_data: list[dict[str, Any]] | None,
) -> str:
    """意味検査違反時の再生成用再考注記を組み立てる。"""
    if insp.get("actor_mismatch"):
        ev_quote = ""
        if recent_events_data:
            ev_lines = [
                f"- {ev.get('actor', '不明')} が {ev.get('action', '')}"
                for ev in recent_events_data[-3:]
            ]
            ev_quote = "\n【出来事メモ】:\n" + "\n".join(ev_lines)
        return (
            f"\n\n[再考注記: 直前の返信候補は行為者の取り違え（{violation_reason}）が検知されたため破棄されました。"
            f"{ev_quote}\n"
            "行為者は出来事メモの通りにすること。確信が持てない場合は行為者の名を出さずに出来事だけに触れること。短く簡潔に返信してください。]"
        )
    return (
        f"\n\n[再考注記: 直前の返信候補はユーザーの指摘や前提と矛盾（{violation_reason}）していたため破棄されました。"
        "ユーザーの発言を受け止め、否定された前提を繰り返さず、短く簡潔に返信してください。]"
    )


def _build_deviation_deliberation_note() -> str:
    """キャラクター逸脱検知時の再生成用再考注記を組み立てる（§81）。"""
    return (
        "\n\n[再考注記: 直前の返信候補はキャラクター設定から逸脱した表現"
        "（乱暴な二人称「お前」「あんた」等・暴言・攻撃的な語尾・不自然な対話拒絶）が含まれていたため破棄されました。"
        "相手を乱暴な二人称や暴言で呼ばず、設定された一人称・口調・キャラクター性を厳格に維持して、"
        "同じ会話の流れで自然に短く返信してください。]"
    )


async def _invoke_regenerate_fn(
    regenerate_fn: Callable[..., Awaitable[str]],
    delib_correction: str,
) -> str:
    try:
        sig = inspect.signature(regenerate_fn)
        params = list(sig.parameters.values())
        accepts_positional = any(
            p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            )
            for p in params
        )
        accepts_kw_delib = any(
            p.kind == inspect.Parameter.KEYWORD_ONLY and p.name == "delib_correction"
            for p in params
        )
    except (ValueError, TypeError):
        accepts_positional = True
        accepts_kw_delib = False

    raw_res: Any
    if accepts_positional:
        raw_res = regenerate_fn(delib_correction)
    elif accepts_kw_delib:
        raw_res = regenerate_fn(delib_correction=delib_correction)
    else:
        raw_res = regenerate_fn()
    if inspect.isawaitable(raw_res):
        raw_res = await raw_res
    return str(raw_res or "")


def _log_send_gate(
    *,
    path: str,
    inspected: bool,
    needs_check: bool,
    flags: Sequence[str],
    action: str,
    budget_exceeded: bool = False,
    near_duplicate_previous: bool = False,
) -> None:
    flags_str = ",".join(flags) if flags else "none"
    if budget_exceeded:
        if near_duplicate_previous:
            log.warning(
                "[SEND_GATE] path=%s inspected=%s needs_check=%s flags=%s action=%s budget_exceeded=True near_duplicate_previous=True",
                path,
                inspected,
                needs_check,
                flags_str,
                action,
            )
        else:
            log.warning(
                "[SEND_GATE] path=%s inspected=%s needs_check=%s flags=%s action=%s budget_exceeded=True",
                path,
                inspected,
                needs_check,
                flags_str,
                action,
            )
    elif action in ("adopted_violation", "text_fallback", "error_fail_open"):
        log.warning(
            "[SEND_GATE] path=%s inspected=%s needs_check=%s flags=%s action=%s",
            path,
            inspected,
            needs_check,
            flags_str,
            action,
        )
    else:
        log.info(
            "[SEND_GATE] path=%s inspected=%s needs_check=%s flags=%s action=%s",
            path,
            inspected,
            needs_check,
            flags_str,
            action,
        )


async def run_semantic_send_gate(
    *,
    path: str,
    reply: str,
    user_text: str = "",
    bot_previous_text: str = "",
    context_lines: Sequence[str] | None = None,
    regenerate_fn: Callable[..., Awaitable[str]] | None = None,
    on_violation: str = "adopt",
    intent_info: dict[str, Any] | None = None,
    denied_claims: list[dict[str, str]] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
    current_speaker: str | None = None,
    bot_name: str | None = None,
    owner: Any = None,
    message: Any = None,
    runtime: Any = None,
    bot_identity: Any = None,
    channel_id: int | str | None = None,
    reaction_fallback_used: bool = False,
) -> tuple[str, str]:
    """送信層の単一検査ゲート（§79, §81, §87）。

    すべてのモデル生成テキスト送信経路がここを通過し、
    決定的キャラクター逸脱チェック（§81）と意味検査（§79）を実施して
    (最終本文, action) を返す。
    action: "skipped" | "gated_out" | "passed" | "regenerated" |
            "adopted_violation" | "text_fallback" | "reaction_fallback" |
            "error_fail_open"
    """
    if not _gate_cfg_bool("SEMANTIC_GUARD_ENABLED", True) or not str(reply or "").strip():
        _log_send_gate(path=path, inspected=False, needs_check=False, flags=(), action="skipped")
        return reply, "skipped"

    inspected_any = False
    needs_check_any = False
    last_flags: list[str] = []

    try:
        ctx_lines_list = list(context_lines) if context_lines else []
        resolved_identity = bot_identity
        if resolved_identity is None and (owner is not None or message is not None):
            with contextlib.suppress(Exception):
                resolved_identity = resolve_bot_identity(bot=getattr(owner, "bot", None), message=message)

        resolved_claims: list[dict[str, str]]
        if denied_claims is not None:
            resolved_claims = list(denied_claims)
        elif _gate_cfg_bool("CORRECTION_UPTAKE_ENABLED", True):
            resolved_claims = []
            try:
                from harness.agent.prompt_parser.prompt_manager.denied_claims import list_denied_claims
                p_name = getattr(resolved_identity, "primary_name", "default") if resolved_identity is not None else "default"
                c_key = (
                    channel_id
                    if channel_id is not None
                    else getattr(getattr(message, "channel", None), "id", None)
                    or getattr(runtime, "cid", None)
                    or "default"
                )
                resolved_claims = list_denied_claims(persona=p_name, channel_key=str(c_key))
            except Exception as e:
                log.debug("list_denied_claims skipped in send gate: %r", e)
        else:
            resolved_claims = []

        if current_speaker is not None:
            resolved_speaker = str(current_speaker).strip()
        else:
            resolved_speaker = (
                (display_name(message) if message is not None else "")
                or (getattr(runtime, "author_display_name", "") if runtime is not None else "")
                or (getattr(runtime, "author_name", "") if runtime is not None else "")
                or ""
            ).strip()

        resolved_bot_name = (
            str(bot_name).strip()
            if bot_name is not None and str(bot_name).strip()
            else str(cfg("BOT_NAME", "コイト") or "コイト").strip()
        )
        resolved_recent_events = (
            recent_events
            if recent_events is not None
            else (extract_recent_events_from_runtime(runtime) if runtime is not None else None)
        )

        eff_assess = (
            assess_reply_risk
            if assess_reply_risk is not _ORIG_ASSESS_REPLY_RISK
            else _semantic_guard_mod.assess_reply_risk
        )
        eff_inspect = (
            inspect_reply_semantics
            if inspect_reply_semantics is not _ORIG_INSPECT_REPLY_SEMANTICS
            else _semantic_guard_mod.inspect_reply_semantics
        )
        eff_deviation_fn = (
            looks_like_character_deviation
            if looks_like_character_deviation is not _ORIG_LOOKS_LIKE_CHARACTER_DEVIATION
            else getattr(_prompt_parser_mod, "looks_like_character_deviation", looks_like_character_deviation)
        )

        def _check_character_deviation(candidate_text: str) -> bool:
            if not callable(eff_deviation_fn):
                return False
            try:
                return bool(eff_deviation_fn(candidate_text, user_text=user_text))
            except Exception as dev_err:
                log.warning(
                    "[SEND_GATE] character deviation check failed (fail-open, path=%s): %r",
                    path,
                    dev_err,
                )
                return False

        async def _inspect_llm_call(prompt_text: str) -> Any:
            middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm")
            return await call_ollama_json(
                prompt_text,
                system_prompt="あなたは会話の整合性を検証する意味検査AIです。",
                schema=SEMANTIC_INSPECTION_SCHEMA,
                model=middle_model,
                temperature=0.0,
                think=False,
                timeout_sec=max(_gate_cfg_float("SEMANTIC_GUARD_TIMEOUT_SEC", 15.0), 1.0),
            )

        async def _evaluate_candidate(
            candidate_text: str,
        ) -> tuple[bool, bool, bool, list[str], str, dict[str, bool]]:
            if _check_character_deviation(candidate_text):
                return True, False, False, ["character_deviation"], "character_deviation", {}
            risk = eff_assess(
                reply_text=candidate_text,
                intent_info=intent_info,
                denied_claims=resolved_claims,
                previous_reply_text=bot_previous_text,
                context_lines=ctx_lines_list,
                current_speaker=resolved_speaker,
                bot_name=resolved_bot_name,
            )
            if risk.get("substring_hit"):
                return True, True, False, ["substring_hit"], f"substring_hit: {risk['substring_hit']}", {}
            if not risk.get("needs_check"):
                return False, False, False, [], "", {}
            has_actor_risk = bool(
                risk.get("actor_attribution_risk")
                or ("actor_attribution_risk" in (risk.get("reasons") or []))
            )
            insp = await eff_inspect(
                llm_caller_fn=_inspect_llm_call,
                user_text=user_text,
                bot_previous_text=bot_previous_text or "",
                generated_reply=candidate_text,
                denied_claims=resolved_claims,
                context_lines=ctx_lines_list if (has_actor_risk or (path == "break" and ctx_lines_list)) else None,
                recent_events=resolved_recent_events if has_actor_risk else None,
            )
            flags = extract_semantic_violation_flags(insp)
            violated = (
                (not insp.get("ok", True))
                or bool(insp.get("incoherent"))
                or bool(insp.get("self_contradiction"))
                or bool(flags)
            )
            if violated and not flags:
                flags = ["ok=False"]
            if not violated:
                return False, True, True, [], "", insp
            reason = f"semantic_violation({','.join(flags)}): {insp}"
            return True, True, True, flags, reason, insp

        violated, needs_check, inspected, flags, reason, insp = await _evaluate_candidate(reply)
        needs_check_any = needs_check_any or needs_check
        inspected_any = inspected_any or inspected
        if flags:
            last_flags = flags

        if not violated:
            if not needs_check:
                _log_send_gate(
                    path=path,
                    inspected=False,
                    needs_check=False,
                    flags=(),
                    action="gated_out",
                )
                return reply, "gated_out"
            _log_send_gate(
                path=path,
                inspected=True,
                needs_check=True,
                flags=(),
                action="passed",
            )
            return reply, "passed"

        budget_exceeded = _is_turn_budget_exceeded()
        if regenerate_fn is not None:
            if budget_exceeded:
                log.warning(
                    "[SEMANTIC_GUARD] violation detected (path=%s, flags=%s); skipping regeneration due to turn budget exceeded",
                    path,
                    ",".join(last_flags) or "none",
                )
                _record_budget_skip("send_gate_regen")
            else:
                log.warning(
                    "[SEMANTIC_GUARD] violation detected (path=%s, flags=%s); retrying generation once...",
                    path,
                    ",".join(last_flags) or "none",
                )
                if "character_deviation" in last_flags:
                    delib_correction = _build_deviation_deliberation_note()
                else:
                    delib_correction = _build_semantic_deliberation_note(
                        violation_reason=reason,
                        insp=insp,
                        recent_events_data=resolved_recent_events,
                    )
                retry_reply = await _invoke_regenerate_fn(regenerate_fn, delib_correction)
                if str(retry_reply or "").strip():
                    (
                        retry_violated,
                        retry_needs_check,
                        retry_inspected,
                        retry_flags,
                        _,
                        _,
                    ) = await _evaluate_candidate(retry_reply)
                    needs_check_any = needs_check_any or retry_needs_check
                    inspected_any = inspected_any or retry_inspected
                    if not retry_violated:
                        _log_send_gate(
                            path=path,
                            inspected=inspected_any,
                            needs_check=needs_check_any,
                            flags=last_flags,
                            action="regenerated",
                        )
                        return retry_reply, "regenerated"
                    if retry_flags:
                        if "character_deviation" in last_flags and "character_deviation" not in retry_flags:
                            last_flags = ["character_deviation", *retry_flags]
                        else:
                            last_flags = retry_flags

        effective_already_reacted = bool(
            reaction_fallback_used
            or (runtime is not None and getattr(runtime, "semantic_guard_reaction_fallback_used", False))
        )
        landing_budget_exceeded = (
            budget_exceeded
            or _is_turn_budget_exceeded()
            or _is_turn_budget_skips_recorded()
        )
        if landing_budget_exceeded:
            unsendable, near_dup, sim_ratio = _check_budget_reply_unsendable(reply, bot_previous_text)
            if not unsendable:
                _log_send_gate(
                    path=path,
                    inspected=inspected_any,
                    needs_check=needs_check_any,
                    flags=last_flags,
                    action="adopted_violation",
                    budget_exceeded=True,
                )
                return reply, "adopted_violation"

            fallback_text = _resolve_send_gate_text_fallback(owner=owner)
            if fallback_text:
                log.warning(
                    "[SEND_GATE] budget exceeded and reply unsendable (path=%s, flags=%s, near_duplicate_previous=%s, ratio=%.3f); falling back to text",
                    path,
                    ",".join(last_flags) or "none",
                    near_dup,
                    sim_ratio,
                )
                _log_send_gate(
                    path=path,
                    inspected=inspected_any,
                    needs_check=needs_check_any,
                    flags=last_flags,
                    action="text_fallback",
                    budget_exceeded=True,
                    near_duplicate_previous=near_dup,
                )
                return fallback_text, "text_fallback"

            if on_violation == "reaction" and not effective_already_reacted:
                _log_send_gate(
                    path=path,
                    inspected=inspected_any,
                    needs_check=needs_check_any,
                    flags=last_flags,
                    action="reaction_fallback",
                    budget_exceeded=True,
                    near_duplicate_previous=near_dup,
                )
                return "", "reaction_fallback"

            _log_send_gate(
                path=path,
                inspected=inspected_any,
                needs_check=needs_check_any,
                flags=last_flags,
                action="adopted_violation",
                budget_exceeded=True,
                near_duplicate_previous=near_dup,
            )
            return reply, "adopted_violation"

        if on_violation == "reaction" and not effective_already_reacted:
            _log_send_gate(
                path=path,
                inspected=inspected_any,
                needs_check=needs_check_any,
                flags=last_flags,
                action="reaction_fallback",
                budget_exceeded=False,
            )
            return "", "reaction_fallback"

        _log_send_gate(
            path=path,
            inspected=inspected_any,
            needs_check=needs_check_any,
            flags=last_flags,
            action="adopted_violation",
            budget_exceeded=False,
        )
        return reply, "adopted_violation"
    except Exception as e:
        log.warning("[SEMANTIC_GUARD] send gate execution failed (fail-open, path=%s): %r", path, e)
        _log_send_gate(
            path=path,
            inspected=inspected_any,
            needs_check=needs_check_any,
            flags=last_flags,
            action="error_fail_open",
        )
        return reply, "error_fail_open"

try:
    from harness.agent.hooks import REPLY_PRE_SEND, HookContext, register_hook, run_hooks
    _HOOKS_AVAILABLE = True
except Exception:  # pragma: no cover
    _HOOKS_AVAILABLE = False


def _reply_shape_observer(context: Any) -> None:
    """返信の形（行数・文字数）の逸脱を観測する（遮断しない）。"""
    text = str(context.text or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 5 or len(text) > 400:
        log.warning(
            "[discord-hooks] reply_shape_observer: lines=%d chars=%d (soft contract: <=5 lines, <=400 chars)",
            len(lines), len(text),
        )
    return None


if _HOOKS_AVAILABLE:
    register_hook(REPLY_PRE_SEND, "reply_shape_observer", _reply_shape_observer, order=100)



class ReplySendingMixin(_OllamaChatReplyCoreBase):
    """返信メッセージの送信、分割送信、直接リプライ/チャンネル投稿の判定、相槌リアクション化を担当するMixin。"""

    async def _store_bot_utterance(
        self,
        message: discord.Message,
        raw_reply: str,
        *,
        reference_note: str | None = None,
    ) -> None:
        try:
            cleaned_reply = extract_first_user_facing_reply(raw_reply)
            if cleaned_reply and not looks_like_abnormal_assistant_reply(cleaned_reply):
                guild_obj = getattr(message, "guild", None)
                channel_obj = getattr(message, "channel", None)
                add_fn = getattr(self.memory_store, "add_bot_utterance", None)
                if callable(add_fn):
                    try:
                        await add_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            guild_id=getattr(guild_obj, "id", None),
                            channel_id=getattr(channel_obj, "id", None),
                            content=cleaned_reply,
                            reference_note=reference_note,
                        )
                    except TypeError:
                        await add_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            guild_id=getattr(guild_obj, "id", None),
                            channel_id=getattr(channel_obj, "id", None),
                            content=cleaned_reply,
                        )
                store = getattr(self, "memory_store", None)
                add_act_fn = getattr(store, "add_bot_activity", None)
                if callable(add_act_fn) and bool(cfg("ACTIVITY_LOG_ENABLED", True)):
                    try:
                        await add_act_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            front="discord",
                            kind="reply",
                            summary=cleaned_reply[:400],
                            detail=reference_note[:80] if reference_note else None,
                            guild_id=getattr(guild_obj, "id", None),
                            channel_id=getattr(channel_obj, "id", None),
                        )
                    except Exception as act_e:
                        log.debug("failed to store bot activity (reply): %s", act_e)
        except Exception as e:
            log.exception("bot utterance save failed: %s", e)

    async def _record_reaction_activity(self, message: discord.Message, emoji: str) -> None:
        if not bool(cfg("ACTIVITY_LOG_ENABLED", True)):
            return
        store = getattr(self, "memory_store", None)
        add_act_fn = getattr(store, "add_bot_activity", None)
        if not callable(add_act_fn):
            return
        try:
            target_author = (display_name(message) if message is not None else "").strip() or "誰か"
            target_content = str(getattr(message, "content", "") or "").strip().replace("\n", " ")[:80]
            summary = f"{target_author}の発言『{target_content}』に{emoji}を付けた"
            guild_obj = getattr(message, "guild", None)
            channel_obj = getattr(message, "channel", None)
            await add_act_fn(
                persona=cfg_persona_namespace(custom_cfg=cfg),
                front="discord",
                kind="reaction",
                summary=summary[:400],
                guild_id=getattr(guild_obj, "id", None),
                channel_id=getattr(channel_obj, "id", None),
            )
        except Exception as e:
            log.debug("failed to record reaction activity: %s", e)

    def _should_use_direct_reply(
        self,
        message: discord.Message,
        *,
        is_direct: bool = False,
        reply: str = "",
    ) -> bool:
        if cfg_bool("ALWAYS_CHANNEL_SEND", False):
            return False
        if cfg_bool("ALWAYS_DIRECT_REPLY", False):
            return True
        if is_direct and not cfg_bool("DIRECT_REPLY_ON_MENTION", True):
            return False

        # 後方互換・強制返信フラグ: DIRECT_REPLY_RATE >= 1.0 の場合は即座に True
        rate = cfg_float("DIRECT_REPLY_RATE", 0.0)
        if rate >= 1.0:
            return True

        cid = channel_id(message) if message is not None else None
        cache = getattr(self, "channel_context_cache", None)
        queue = cache.get(cid) if cache and cid is not None else None
        tracker = getattr(self, "thread_tracker", None)
        return decide_reply_mode(
            target_message=message,
            reply_text=reply,
            is_direct=is_direct,
            context_queue=queue,
            thread_tracker=tracker,
        )

    def _is_stale_reply(
        self,
        message: discord.Message | None,
        *,
        is_direct: bool = False,
    ) -> bool:
        if message is None:
            return False
        queue = self._get_channel_context_queue(message)
        bot_user_id = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        return is_stale_reply(
            message,
            is_direct=is_direct,
            context_queue=queue,
            bot_user_id=bot_user_id,
        )

    async def _reply_to_message(
        self,
        target_message: discord.Message,
        reply: str,
        *,
        mention_author: bool = False,
        is_direct: bool = False,
        drop_if_target_deleted: bool = False,
    ) -> discord.Message:
        raw_reply = str(reply or "").strip()
        payload = _format_reply(reply)
        if self._should_use_direct_reply(target_message, reply=raw_reply, is_direct=is_direct):
            try:
                sent = await target_message.reply(payload, mention_author=mention_author)
            except discord.NotFound:
                if drop_if_target_deleted:
                    log.info(
                        "reply target deleted; dropping non-direct reply (message_id=%s)",
                        getattr(target_message, "id", None),
                    )
                    raise ReplyTargetDeletedError(str(getattr(target_message, "id", "")))
                log.info("reply target disappeared; falling back to channel send (message_id=%s)", getattr(target_message, "id", None))
                sent = await target_message.channel.send(payload)
            except discord.Forbidden as e:
                # 160002: Cannot reply without permission to read message history
                # 50013: Missing Permissions (general send/reply permission)
                err_code = getattr(e, "code", None)
                if err_code in (160002, 50013):
                    log.warning(
                        "reply forbidden (code=%s); falling back to channel send (message_id=%s channel=%s)",
                        err_code,
                        getattr(target_message, "id", None),
                        getattr(getattr(target_message, "channel", None), "id", None),
                    )
                    sent = await target_message.channel.send(payload)
                else:
                    raise
            except discord.HTTPException as e:
                if getattr(e, "code", None) == 50035 and "Unknown message" in str(e):
                    if drop_if_target_deleted:
                        log.info(
                            "reply target deleted; dropping non-direct reply (message_id=%s)",
                            getattr(target_message, "id", None),
                        )
                        raise ReplyTargetDeletedError(str(getattr(target_message, "id", "")))
                    log.info(
                        "reply target unknown; falling back to channel send (message_id=%s, code=%s)",
                        getattr(target_message, "id", None),
                        getattr(e, "code", None),
                    )
                    sent = await target_message.channel.send(payload)
                else:
                    raise
        else:
            sent = await target_message.channel.send(payload)
        return sent

    def _resolve_send_other_names(
        self,
        message: discord.Message | None,
        target_name: str,
    ) -> set[str]:
        """送信直前にサニタイズ対象とする他者名（直近会話話者、返信元作成者、メンション対象等）を収集する。"""
        other_names: set[str] = set()
        clean_target = (target_name or "").strip()
        if message is None:
            return other_names

        cid = getattr(getattr(message, "channel", None), "id", None)
        # 1. チャンネル直近キャッシュから取得
        if cid:
            cached_items = recent_context_items(
                self.channel_context_cache,
                cid,
                before_message=message,
                max_age_sec=3600,
            )
            for item in cached_items:
                line = str(item.get("line") or "")
                line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
                if ":" in line_no_ts:
                    speaker_part = line_no_ts.split(":", 1)[0].strip()
                    speaker = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
                    if speaker and speaker != clean_target:
                        other_names.add(speaker)

        # 2. 返信元の親メッセージ（reference）の作成者名を無条件で追加
        ref = getattr(message, "reference", None)
        if ref:
            ref_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
            if ref_msg is not None:
                ref_author = display_name(ref_msg).strip()
                if ref_author and ref_author != clean_target:
                    other_names.add(ref_author)
                ref_raw_author = getattr(getattr(ref_msg, "author", None), "name", "")
                if ref_raw_author and ref_raw_author != clean_target:
                    other_names.add(ref_raw_author)
            mid = getattr(ref, "message_id", None)
            if mid and cid:
                q = self.channel_context_cache.get(cid)
                if q:
                    for itm in q:
                        if itm.get("id") == mid:
                            line = str(itm.get("line") or "")
                            line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
                            if ":" in line_no_ts:
                                speaker_part = line_no_ts.split(":", 1)[0].strip()
                                spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
                                if spk and spk != clean_target:
                                    other_names.add(spk)

        # 3. メンションされているユーザーの名前を追加
        for m in getattr(message, "mentions", []) or []:
            m_name = display_name(m).strip()
            if m_name and m_name != clean_target:
                other_names.add(m_name)
            raw_name = getattr(m, "name", "")
            if raw_name and raw_name != clean_target:
                other_names.add(raw_name)

        # 4. スレッド作成者等の追跡
        ch = getattr(message, "channel", None)
        if ch is not None:
            thread_owner = getattr(ch, "owner", None)
            if thread_owner is not None:
                to_name = display_name(thread_owner).strip()
                if to_name and to_name != clean_target:
                    other_names.add(to_name)

        # 5. 発言者本人の生アカウント名（サーバーニックネームと乖離している場合の多重防御）
        author = getattr(message, "author", None)
        if author is not None:
            raw_author = getattr(author, "name", "")
            if raw_author and raw_author != clean_target:
                other_names.add(raw_author)

        return other_names

    def _resolve_aizuchi_emoji(self, text: str) -> str:
        """相槌テキストに応じた絵文字を決定する。"""
        if any(m in text for m in ("わはは", "あはは", "草", "笑")):
            return "😆"
        if any(m in text for m in ("すごい", "すごーい", "いいね")):
            return "✨"
        if any(m in text for m in ("了解", "おっけー", "オッケー", "はい")):
            return "🙆"
        if any(m in text for m in ("おつかれ", "お疲れ", "感謝", "ありがとう")):
            return "🙏"
        return "👍"

    async def _run_semantic_send_gate(
        self,
        *,
        path: str,
        reply: str,
        user_text: str = "",
        bot_previous_text: str = "",
        context_lines: Sequence[str] | None = None,
        regenerate_fn: Callable[..., Awaitable[str]] | None = None,
        on_violation: str = "adopt",
        intent_info: dict[str, Any] | None = None,
        denied_claims: list[dict[str, str]] | None = None,
        recent_events: list[dict[str, Any]] | None = None,
        current_speaker: str | None = None,
        bot_name: str | None = None,
        message: Any = None,
        runtime: Any = None,
        bot_identity: Any = None,
        channel_id: int | str | None = None,
        reaction_fallback_used: bool = False,
    ) -> tuple[str, str]:
        return await run_semantic_send_gate(
            path=path,
            reply=reply,
            user_text=user_text,
            bot_previous_text=bot_previous_text,
            context_lines=context_lines,
            regenerate_fn=regenerate_fn,
            on_violation=on_violation,
            intent_info=intent_info,
            denied_claims=denied_claims,
            recent_events=recent_events,
            current_speaker=current_speaker,
            bot_name=bot_name,
            owner=self,
            message=message,
            runtime=runtime,
            bot_identity=bot_identity,
            channel_id=channel_id,
            reaction_fallback_used=reaction_fallback_used,
        )

    async def _send_reply(
        self,
        message: discord.Message,
        reply: str,
        *,
        is_direct: bool = False,
        reference_note: str | None = None,
        runtime: Any = None,
        path: str | None = None,
        user_text: str | None = None,
        bot_previous_text: str | None = None,
        context_lines: Sequence[str] | None = None,
        regenerate_fn: Callable[..., Awaitable[str]] | None = None,
        on_violation: str = "adopt",
        intent_info: dict[str, Any] | None = None,
        bot_identity: Any = None,
    ) -> discord.Message | None:
        if self._is_stale_reply(message, is_direct=is_direct):
            elapsed_sec = self._calculate_message_elapsed_sec(message)
            gap = self._calculate_channel_gap(message)
            log.info(
                "TRACE stale reply dropped: channel=%s message_id=%s author=%s elapsed_sec=%.1f gap=%d (conversation already moved forward)",
                getattr(message, "channel", None),
                getattr(message, "id", None),
                getattr(message, "author", None),
                elapsed_sec,
                gap,
            )
            return None

        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(message) if message is not None else "").strip()
        other_names = self._resolve_send_other_names(message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(message, "content", "") or ""),
        )

        should_run_gate = (
            path is not None
            or user_text is not None
            or bot_previous_text is not None
            or context_lines is not None
            or regenerate_fn is not None
        )
        if should_run_gate:
            eff_user_text = (
                user_text
                if user_text is not None
                else (
                    (
                        getattr(runtime, "effective_user_text", None)
                        or getattr(runtime, "original_user_text", None)
                        or str(getattr(message, "content", "") or "")
                    )
                    if runtime is not None
                    else str(getattr(message, "content", "") or "")
                )
            )
            eff_context_lines = (
                list(context_lines)
                if context_lines is not None
                else list(getattr(runtime, "context_lines", None) or [])
            )
            gated_reply, gate_action = await self._run_semantic_send_gate(
                path=path or "reply",
                reply=raw_reply,
                user_text=str(eff_user_text or ""),
                bot_previous_text=bot_previous_text or "",
                context_lines=eff_context_lines,
                regenerate_fn=regenerate_fn,
                on_violation=on_violation,
                intent_info=intent_info,
                current_speaker=target_name,
                message=message,
                runtime=runtime,
                bot_identity=bot_identity,
            )
            if gate_action == "reaction_fallback":
                fallback_emoji = "🙏"
                safe_add = getattr(self, "_safe_add_reaction", None)
                if callable(safe_add):
                    await safe_add(message, fallback_emoji)
                elif hasattr(message, "add_reaction"):
                    with contextlib.suppress(Exception):
                        await message.add_reaction(fallback_emoji)
                await self._record_reaction_activity(message, fallback_emoji)
                if runtime is not None:
                    with contextlib.suppress(Exception):
                        setattr(runtime, "semantic_guard_reaction_fallback_used", True)
                log.info(
                    "TRACE reply fell back to reaction by send gate: channel=%s message_id=%s emoji=%s",
                    getattr(message, "channel", None),
                    getattr(message, "id", None),
                    fallback_emoji,
                )
                return None
            if gate_action == "regenerated":
                raw_reply = sanitize_generated_reply(
                    str(gated_reply or "").strip(),
                    bot_user_id=bot_uid,
                    replacement_name=target_name,
                    target_name=target_name,
                    other_names=other_names,
                    user_text=str(getattr(message, "content", "") or ""),
                )
            else:
                raw_reply = gated_reply

        # ステップ3: 相槌のリアクション化
        # 返信文が12文字以内の相槌パターンであり、かつ random.random() < REPLY_REACTION_ONLY_RATE の場合、
        # 文章送信を行わず _safe_add_reaction で感情に応じた絵文字を付与して終了する（メンション返信時は除外）
        is_aizuchi = (
            not is_direct
            and len(raw_reply) <= 12
            and bool(_AIZUCHI_PATTERN.match(raw_reply))
        )
        if is_aizuchi:
            reaction_only_rate = cfg_float("REPLY_REACTION_ONLY_RATE", 0.0)
            if reaction_only_rate > 0.0 and random.random() < reaction_only_rate:
                emoji = self._resolve_aizuchi_emoji(raw_reply)
                safe_add = getattr(self, "_safe_add_reaction", None)
                if callable(safe_add):
                    await safe_add(message, emoji)
                elif hasattr(message, "add_reaction"):
                    with contextlib.suppress(Exception):
                        await message.add_reaction(emoji)
                await self._record_reaction_activity(message, emoji)
                log.info(
                    "TRACE reply converted to reaction: channel=%s message_id=%s reply=%r emoji=%s",
                    getattr(message, "channel", None),
                    getattr(message, "id", None),
                    raw_reply,
                    emoji,
                )
                return None

        if _HOOKS_AVAILABLE:
            _hook_outcome = run_hooks(
                REPLY_PRE_SEND,
                HookContext(
                    point=REPLY_PRE_SEND,
                    front="discord",
                    text=raw_reply,
                    metadata={
                        "channel_id": getattr(getattr(message, "channel", None), "id", None),
                        "user_id": getattr(getattr(message, "author", None), "id", None),
                    },
                ),
            )
            if _hook_outcome.blocked:
                log.warning("[discord] フックにより返信送信を見送り: reason=%s", _hook_outcome.reason)
                return None
            raw_reply = _hook_outcome.text

        # ステップ2: タイピング表示付き分割送信の実装
        split_enabled = cfg_bool("REPLY_SPLIT_ENABLED", False)
        max_chunks = cfg_int("REPLY_SPLIT_MAX_CHUNKS", 3)
        chunks = _split_reply_text(raw_reply, max_chunks=max_chunks) if split_enabled else [raw_reply]

        if not chunks:
            chunks = [raw_reply]

        # 1. 先頭チャンク: 既存ロジック（直接リプライ要否に従う）で送信
        first_chunk = chunks[0]
        try:
            sent = await self._reply_to_message(
                message,
                first_chunk,
                mention_author=False,
                is_direct=is_direct,
                drop_if_target_deleted=not is_direct,
            )
        except ReplyTargetDeletedError:
            return None

        log.info(
            "TRACE sent reply: channel=%s target_author=%s sent_id=%s content=%r",
            getattr(message, "channel", None),
            getattr(message, "author", None),
            getattr(sent, "id", None),
            first_chunk,
        )

        # 2. 2つ目以降のチャンク: タイピング表示付き送信
        if len(chunks) > 1:
            channel = getattr(message, "channel", None)
            sec_per_char = cfg_float("REPLY_TYPING_SEC_PER_CHAR", 0.05)
            remaining_chunks = list(chunks[1:])

            for idx, chunk in enumerate(remaining_chunks, start=2):
                typing_delay = max(0.8, min(3.0, len(chunk) * sec_per_char))
                try:
                    if channel is not None and hasattr(channel, "typing"):
                        async with channel.typing():
                            await asyncio.sleep(typing_delay)
                    else:
                        await asyncio.sleep(typing_delay)

                    if channel is not None and hasattr(channel, "send"):
                        await channel.send(chunk)
                        log.info(
                            "TRACE sent reply chunk: channel=%s chunk_idx=%d/%d content=%r",
                            channel,
                            idx,
                            len(chunks),
                            chunk,
                        )
                except Exception as e:
                    log.warning("failed to send chunk %d/%d; attempting fallback combine send: %r", idx, len(chunks), e)
                    # 3. 送信例外時は残りのチャンクを1つにまとめて即時送信するフォールバック
                    current_remaining = [chunk] + remaining_chunks[idx - 1 :]
                    combined = " ".join(current_remaining)
                    if channel is not None and hasattr(channel, "send"):
                        try:
                            await channel.send(combined)
                            log.info("TRACE sent fallback combined chunk: channel=%s content=%r", channel, combined)
                        except Exception as fb_err:
                            log.error("failed to send combined fallback chunks: %r", fb_err)
                    break

        effective_note = reference_note
        if effective_note is None and runtime is not None:
            effective_note = getattr(runtime, "utterance_reference_note", None) or None

        try:
            await self._store_bot_utterance(message, raw_reply, reference_note=effective_note)
        except TypeError:
            await self._store_bot_utterance(message, raw_reply)

        if sent is not None and hasattr(self, "_append_sent_message"):
            self._append_sent_message(sent, full_text=raw_reply)

        if runtime is not None and sent is not None:
            with contextlib.suppress(Exception):
                setattr(runtime, "assistant_text", raw_reply)

        return sent

    async def _send_provisional_reply(
        self,
        message: discord.Message,
        reply: str,
        *,
        is_direct: bool = False,
    ) -> discord.Message:
        return await self._reply_to_message(message, reply, mention_author=False, is_direct=is_direct)

    async def _send_followup_reply(
        self,
        parent_message: discord.Message,
        reply: str,
        *,
        is_direct: bool = True,
        runtime: Any = None,
        path: str | None = None,
        user_text: str | None = None,
        bot_previous_text: str | None = None,
        context_lines: Sequence[str] | None = None,
        intent_info: dict[str, Any] | None = None,
        bot_identity: Any = None,
    ) -> discord.Message:
        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(parent_message) if parent_message is not None else "").strip()
        other_names = self._resolve_send_other_names(parent_message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(parent_message, "content", "") or ""),
        )
        should_run_gate = (
            path is not None
            or user_text is not None
            or bot_previous_text is not None
            or context_lines is not None
        )
        if should_run_gate:
            eff_user_text = (
                user_text
                if user_text is not None
                else (
                    (
                        getattr(runtime, "effective_user_text", None)
                        or getattr(runtime, "original_user_text", None)
                        or ""
                    )
                    if runtime is not None
                    else ""
                )
            )
            eff_context_lines = (
                list(context_lines)
                if context_lines is not None
                else list(getattr(runtime, "context_lines", None) or [])
            )
            eff_speaker = (
                (
                    getattr(runtime, "author_display_name", None)
                    or getattr(runtime, "author_name", None)
                    or target_name
                )
                if runtime is not None
                else target_name
            )
            raw_reply, _ = await self._run_semantic_send_gate(
                path=path or "followup",
                reply=raw_reply,
                user_text=str(eff_user_text or ""),
                bot_previous_text=bot_previous_text or "",
                context_lines=eff_context_lines,
                regenerate_fn=None,
                on_violation="adopt",
                intent_info=intent_info,
                current_speaker=str(eff_speaker or "").strip(),
                message=parent_message,
                runtime=runtime,
                bot_identity=bot_identity,
            )
        sent = await self._reply_to_message(parent_message, raw_reply, mention_author=False, is_direct=is_direct)
        log.info(
            "TRACE sent reply: channel=%s target_author=%s sent_id=%s content=%r",
            getattr(parent_message, "channel", None),
            getattr(parent_message, "author", None),
            getattr(sent, "id", None),
            raw_reply,
        )
        await self._store_bot_utterance(parent_message, raw_reply)
        if runtime is not None and sent is not None:
            with contextlib.suppress(Exception):
                setattr(runtime, "assistant_text", raw_reply)
        return sent


OllamaChatSendingMixin = ReplySendingMixin

