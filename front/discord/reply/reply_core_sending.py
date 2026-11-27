"""OllamaChatReplyCoreMixinの返信送信・分割送信・絵文字相槌・直接返信判定を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import importlib
import inspect
import json
import logging
import random
import re
import sys
import time
import unicodedata
from typing import Any, Awaitable, Callable, Iterable, Sequence, TYPE_CHECKING

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
from front.discord.utils import author_id, content, message_created_at_ts
from front.discord.common.bot_identity import resolve_bot_identity
import front.discord.turn_budget as _turn_budget_mod
from front.discord.turn_budget import (
    get_turn_budget_snapshot,
    record_turn_budget_skip,
    turn_budget_exceeded,
    turn_budget_skips_recorded,
)
from front.discord.reply.candidate_compare import (
    CANDIDATE_COMPARE_SOURCE_REF,
    DEFAULT_CANDIDATE_COMPARE_ENABLED,
    DEFAULT_CANDIDATE_COMPARE_MAX_ELAPSED_SEC,
    DEFAULT_CANDIDATE_COUNT,
    CandidateEval,
    build_candidate_compare_detail,
    run_reply_candidate_compare,
)
from front.discord.reply import semantic_guard as _semantic_guard_mod
from front.discord.reply.semantic_guard import (
    SEMANTIC_INSPECTION_SCHEMA,
    assess_reply_risk,
    extract_recent_events_from_runtime,
    extract_semantic_violation_flags,
    inspect_reply_semantics,
    resolve_inspection_model,
)
from front.discord.reply.classifier import decide_reply_mode, is_stale_reply
from front.discord.reply.feedback_mining import record_bot_sent_message
import harness.agent.prompt_parser as _prompt_parser_mod
from harness.agent.prompt_parser import looks_like_character_deviation
from harness.agent.prompt_parser.parser.reply_helpers import _format_reply
try:
    from harness.agent.prompt_parser.parser.validation import (
        _normalize_sentence_for_repeat,
        _split_sentences_preserve_endings,
        find_repeated_reply_sentence,
    )
except ImportError:
    def _split_sentences_preserve_endings(text: str) -> list[str]:
        raw = str(text or "")
        if not raw.strip():
            return []
        sentences: list[str] = []
        start, quote_depth, i, n = 0, 0, 0, len(raw)
        while i < n:
            ch = raw[i]
            if ch in ("「", "『"):
                quote_depth += 1
            elif ch in ("」", "』"):
                quote_depth = max(0, quote_depth - 1)
            elif ch == "\n":
                quote_depth = 0
                while i + 1 < n and raw[i + 1] == "\n":
                    i += 1
                part = raw[start : i + 1].strip()
                if part:
                    sentences.append(part)
                start = i + 1
            elif quote_depth == 0 and ch in ("。", "．", "！", "？", "!", "?"):
                while i + 1 < n and raw[i + 1] in ("。", "．", "！", "？", "!", "?"):
                    i += 1
                part = raw[start : i + 1].strip()
                if part:
                    sentences.append(part)
                start = i + 1
            i += 1
        tail = raw[start:].strip()
        if tail:
            sentences.append(tail)
        return sentences

    def _normalize_sentence_for_repeat(sentence: str) -> str:
        text = unicodedata.normalize("NFKC", str(sentence or ""))
        return "".join(
            ch.lower()
            for ch in text
            if not ch.isspace()
            and ch not in ("\u200d", "\ufe0e", "\ufe0f")
            and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
        )

    def find_repeated_reply_sentence(
        recent_texts: Iterable[str] | None,
        candidate: str | None,
        *,
        min_chars: int = 10,
        threshold: float = 0.85,
    ) -> str | None:
        cand_raw = str(candidate or "").strip()
        if not cand_raw or not recent_texts:
            return None
        recent_list = [recent_texts] if isinstance(recent_texts, str) else list(recent_texts)
        recent_norms = [
            norm
            for r in recent_list
            if str(r or "").strip()
            for s in _split_sentences_preserve_endings(str(r).strip())
            if (norm := _normalize_sentence_for_repeat(s))
        ]
        if not recent_norms:
            return None
        for c_sent in _split_sentences_preserve_endings(cand_raw):
            c_norm = _normalize_sentence_for_repeat(c_sent)
            if len(c_norm) < min_chars:
                continue
            for r_norm in recent_norms:
                if difflib.SequenceMatcher(None, r_norm, c_norm).ratio() >= threshold:
                    return c_sent
        return None
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
_ORIG_GET_TURN_BUDGET_SNAPSHOT = get_turn_budget_snapshot
_ORIG_PICK_CHARACTER_DEVIATION_FALLBACK = _pick_character_deviation_fallback
_ORIG_CFG = cfg
_ORIG_CFG_BOOL = cfg_bool
_ORIG_CFG_FLOAT = cfg_float
_ORIG_CFG_INT = cfg_int

NEAR_DUPLICATE_PREVIOUS_RATIO_THRESHOLD = 0.9
_DEFAULT_SEND_GATE_TEXT_FALLBACK = "今のはうまく返せなかった。もう一回言ってくれ。"
_CFG_UNSET_SENTINEL = object()
_HUMAN_OVERLAP_MIN_CHARS = 8
_HUMAN_OVERLAP_THRESHOLD = 0.70
_RECENT_HUMAN_TEXTS_DEFAULT_LIMIT = 10
_DEFAULT_OVERLONG_MAX_SENTENCES = 8
_DEFAULT_OVERLONG_MIN_CHARS = 500
_SYSTEM_MEMO_PREFIXES: tuple[str, ...] = ("System:", "(内心のメモ", "（内心のメモ")
_CONTEXT_TIME_PREFIX_RE = re.compile(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+")
_BOT_REACTION_SUFFIX_RE = re.compile(r"\s*\[Botリアクション:[^\]]*\]\s*$")
_ASSISTANT_ACTION_LINE_RE = re.compile(
    r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?Assistant:\s*（『"
)
_ATTACHED_IMAGE_ANALYSIS_RE = re.compile(
    r"添付画像\d+の解析内容:\s*\n(.*?)(?=\n\n(?:添付(?:画像|音声|動画)\d+の(?:解析内容|文字起こし):|リンク要約:)|\Z)",
    re.DOTALL,
)


def _extract_image_descriptions_from_analysis(text: str | None) -> list[str]:
    """build_message_media_context が生成した添付画像解析テキストから説明文を抽出する。"""
    raw = str(text or "")
    if "添付画像" not in raw or "の解析内容:" not in raw:
        return []
    return [
        m.group(1).strip()
        for m in _ATTACHED_IMAGE_ANALYSIS_RE.finditer(raw)
        if m.group(1) and m.group(1).strip()
    ]


def _resolve_trigger_image_caption(
    *,
    message: Any = None,
    runtime: Any = None,
    user_text: str = "",
) -> str:
    """トリガー発言の画像キャプションを runtime / メモリキャッシュ / user_text から解決する（§153）。"""
    media = getattr(runtime, "media", None) if runtime is not None else None
    analysis_text = getattr(media, "analysis_text", None) if media is not None else None
    descs = _extract_image_descriptions_from_analysis(analysis_text)
    if descs:
        return " / ".join(descs)

    for cand_msg in (getattr(runtime, "message", None) if runtime is not None else None, message):
        mid = getattr(cand_msg, "id", None) if cand_msg is not None else None
        if isinstance(mid, int):
            try:
                from front.discord.common.message_media import get_cached_image_caption
                cached = get_cached_image_caption(mid)
                if cached and str(cached).strip():
                    return str(cached).strip()
            except Exception:
                pass

    user_descs = _extract_image_descriptions_from_analysis(user_text)
    if user_descs:
        return " / ".join(user_descs)
    return ""


def _append_trigger_image_caption_line(
    ctx_lines: list[str],
    *,
    message: Any = None,
    runtime: Any = None,
    user_text: str = "",
    speaker: str = "",
) -> list[str]:
    """トリガー発言に画像キャプションがあり文脈行に未収録なら、[画像内容: ...] 行を末尾へ補完する（§153）。"""
    caption = _resolve_trigger_image_caption(message=message, runtime=runtime, user_text=user_text)
    if not caption:
        return ctx_lines
    clean_caption = re.sub(r"\s+", " ", caption).replace("]", "］").strip()
    if not clean_caption:
        return ctx_lines
    joined_existing = "\n".join(str(line or "") for line in ctx_lines)
    if clean_caption in joined_existing:
        return ctx_lines
    orig_text = str(getattr(runtime, "original_user_text", "") or "").strip() if runtime is not None else ""
    if not orig_text and user_text:
        orig_text = str(user_text).split("\n\nメディア補足:", 1)[0].strip()
        if orig_text.startswith("添付画像") and "の解析内容:" in orig_text:
            orig_text = ""
    orig_single = re.sub(r"\s+", " ", orig_text).strip()[:120]
    spk = str(speaker or "").strip()
    if spk:
        body = f"{orig_single} [画像内容: {clean_caption}]" if orig_single else f"[画像] [画像内容: {clean_caption}]"
        line = f"{spk}: {body}"
    else:
        line = f"System: [画像内容: {clean_caption}]"
    return [*ctx_lines, line]


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


def _get_turn_elapsed_sec(gate_perf_started_at: float | None = None) -> float:
    """ターン予算モジュールから現在ターンの経過秒数を取得する（取得不可時はゲート開始時刻から代用）。"""
    fn = (
        get_turn_budget_snapshot
        if get_turn_budget_snapshot is not _ORIG_GET_TURN_BUDGET_SNAPSHOT
        else getattr(_turn_budget_mod, "get_turn_budget_snapshot", get_turn_budget_snapshot)
    )
    try:
        snapshot = fn()
        if isinstance(snapshot, tuple) and len(snapshot) >= 1:
            elapsed = float(snapshot[0])
            if elapsed > 0.0:
                return elapsed
            started_var = getattr(_turn_budget_mod, "_TURN_BUDGET_STARTED_AT", None)
            if started_var is None or started_var.get() is None:
                if gate_perf_started_at is not None:
                    return max(0.0, time.perf_counter() - gate_perf_started_at)
            return max(0.0, elapsed)
    except Exception:
        pass
    if gate_perf_started_at is not None:
        return max(0.0, time.perf_counter() - gate_perf_started_at)
    return 0.0


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
        try:
            val = cfg_float(name, default)
            return float(default if val is None else val)
        except (TypeError, ValueError):
            return default
    if cfg is not _ORIG_CFG:
        val = cfg(name, default)
        try:
            return float(val if val is not None else default)
        except (TypeError, ValueError):
            return default
    try:
        val = cfg_float(name, default)
        return float(default if val is None else val)
    except (TypeError, ValueError):
        return default


def _gate_cfg_int(name: str, default: int = 0) -> int:
    if cfg_int is not _ORIG_CFG_INT:
        try:
            val = cfg_int(name, default)
            return int(default if val is None else val)
        except (TypeError, ValueError):
            return default
    if cfg is not _ORIG_CFG:
        val = cfg(name, default)
        try:
            return int(default if val is None else val)
        except (TypeError, ValueError):
            return default
    try:
        val = cfg_int(name, default)
        return int(default if val is None else val)
    except (TypeError, ValueError):
        return default


def _extract_cache_item_body_text(item: dict[str, Any]) -> str:
    """文脈キャッシュ項目から発言本文のみを抽出する（§116）。"""
    raw_content = item.get("content")
    if isinstance(raw_content, str) and raw_content.strip():
        return raw_content.strip()
    raw_line = str(item.get("line") or "").strip()
    if not raw_line:
        return ""
    body_lines = [
        ln for ln in raw_line.splitlines()
        if ln.strip() and not _ASSISTANT_ACTION_LINE_RE.match(ln.strip())
    ]
    if not body_lines:
        return ""
    first = _CONTEXT_TIME_PREFIX_RE.sub("", body_lines[0].strip())
    if ": " in first:
        first = first.split(": ", 1)[1].strip()
    elif ":" in first:
        first = first.split(":", 1)[1].strip()
    first = _BOT_REACTION_SUFFIX_RE.sub("", first).strip()
    if len(body_lines) > 1:
        rest = [
            _BOT_REACTION_SUFFIX_RE.sub("", ln.strip()).strip()
            for ln in body_lines[1:]
        ]
        rest = [r for r in rest if r]
        if rest:
            return "\n".join([first, *rest]).strip()
    return first


def _is_assistant_or_system_cache_item(
    item: dict[str, Any],
    *,
    bot_user_id: int | None,
) -> bool:
    """キャッシュ項目が Bot 自身の発言または System 内部メモであれば True を返す（§116）。"""
    if bot_user_id is not None and item.get("author_id") == bot_user_id:
        return True
    role = str(item.get("role") or "").strip().lower()
    if role in ("assistant", "system"):
        return True
    name = str(item.get("name") or "").strip().lower()
    if name in ("assistant", "system"):
        return True
    raw_line = str(item.get("line") or "").strip()
    line_no_ts = _CONTEXT_TIME_PREFIX_RE.sub("", raw_line)
    return line_no_ts.startswith(_SYSTEM_MEMO_PREFIXES) or line_no_ts.startswith("Assistant:")


async def _find_recent_human_message_texts(
    message: Any = None,
    *,
    bot_user_id: int | None = None,
    cache: dict[int, Any] | None = None,
    context_lines: Sequence[str] | None = None,
    bot_name: str | None = None,
    limit: int = _RECENT_HUMAN_TEXTS_DEFAULT_LIMIT,
    max_age_sec: float | None = None,
) -> list[str]:
    """手元の文脈キャッシュ／既取得文脈行から Bot 自身以外の直近発言本文を最大 limit 件（既定10件）収集する（§116）。

    追加の Discord API 呼び出し（channel.history）は発行せず、データが手元に無い場合は空リストを返す（fail-open）。
    """
    if limit <= 0:
        return []

    results: list[str] = []
    try:
        eff_max_age = (
            max(float(max_age_sec), 0.0)
            if max_age_sec is not None
            else max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0)
        )
        cid = channel_id(message) if message is not None else None
        if cid is not None and isinstance(cache, dict):
            items = recent_context_items(
                cache,
                cid,
                before_message=message,
                max_age_sec=eff_max_age,
            )
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                if _is_assistant_or_system_cache_item(item, bot_user_id=bot_user_id):
                    continue
                text = _extract_cache_item_body_text(item)
                if not text:
                    continue
                results.append(text)
                if len(results) >= limit:
                    return results

        if results or not context_lines:
            return results

        bot_names_lower = {"assistant", "system"}
        if bot_name and str(bot_name).strip():
            bot_names_lower.add(str(bot_name).strip().lower())
        for raw_line in reversed(list(context_lines)):
            line_str = str(raw_line or "").strip()
            if not line_str:
                continue
            first_line = _CONTEXT_TIME_PREFIX_RE.sub("", line_str.splitlines()[0].strip())
            if first_line.startswith(_SYSTEM_MEMO_PREFIXES) or ":" not in first_line:
                continue
            spk_part, body_part = first_line.split(":", 1)
            spk_clean = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", spk_part).strip()
            if not spk_clean or spk_clean.lower() in bot_names_lower:
                continue
            body_clean = _BOT_REACTION_SUFFIX_RE.sub("", body_part).strip()
            if body_clean:
                results.append(body_clean)
                if len(results) >= limit:
                    break
    except Exception as e:
        log.debug("recent human message collection skipped (fail-open): %r", e)
        return []

    return results


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


def _find_human_sentence_overlap(
    candidate: str | None,
    human_texts: Sequence[str] | None,
) -> str | None:
    """他参加者の直近発言と文単位で重複する候補文を返す（§116）。無ければ None。"""
    if not candidate or not human_texts:
        return None
    return find_repeated_reply_sentence(
        human_texts,
        candidate,
        min_chars=_HUMAN_OVERLAP_MIN_CHARS,
        threshold=_HUMAN_OVERLAP_THRESHOLD,
    )


def _count_reply_sentences(text: str | None) -> int:
    """§103 と同じ文分割区切りで候補本文の実質文数を返す（§116）。"""
    stripped = str(text or "").strip()
    if not stripped:
        return 0
    return sum(
        1
        for sent in _split_sentences_preserve_endings(stripped)
        if _normalize_sentence_for_repeat(sent)
    )


def _check_overlong_essay_reply(
    candidate: str | None,
    *,
    max_sentences: int | None = None,
    min_chars: int | None = None,
) -> str | None:
    """候補本文が長文エッセイ（既定8文以上かつ500文字以上）なら理由文字列を返す（§116）。"""
    stripped = str(candidate or "").strip()
    if not stripped:
        return None
    eff_max_sentences = (
        max(1, int(max_sentences))
        if max_sentences is not None
        else max(1, _gate_cfg_int("REPLY_OVERLONG_GUARD_MAX_SENTENCES", _DEFAULT_OVERLONG_MAX_SENTENCES))
    )
    eff_min_chars = (
        max(1, int(min_chars))
        if min_chars is not None
        else max(1, _gate_cfg_int("REPLY_OVERLONG_GUARD_MIN_CHARS", _DEFAULT_OVERLONG_MIN_CHARS))
    )
    char_count = len(stripped)
    if char_count < eff_min_chars:
        return None
    sent_count = _count_reply_sentences(stripped)
    if sent_count >= eff_max_sentences:
        return f"overlong_essay(sentences={sent_count},chars={char_count})"
    return None


def _build_human_overlap_deliberation_note(violation_reason: str = "") -> str:
    """他参加者の発言との文単位重複検知時の再生成用再考注記を組み立てる（§116）。"""
    detail = f"（{violation_reason}）" if violation_reason else ""
    return (
        f"\n\n[再考注記: 直前の返信候補は、チャンネル内の他の参加者が既に言った内容と"
        f"文単位で重複していたため破棄されました{detail}。"
        "他の参加者の発言を言い換えて繰り返すのではなく、別の切り口や新しい反応で短く自然に返信してください。]"
    )


def _build_overlong_essay_deliberation_note(violation_reason: str = "") -> str:
    """長文エッセイ検知時の再生成用再考注記を組み立てる（§116）。"""
    detail = f"（{violation_reason}）" if violation_reason else ""
    return (
        f"\n\n[再考注記: 直前の返信候補は文数・文字数が長すぎる総括エッセイになっていたため破棄されました{detail}。"
        "要点を絞って2〜4文程度の自然な会話調で簡潔に返信してください。]"
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


async def _record_gate_incident(
    *,
    owner: Any,
    path: str,
    action: str,
    flags: Sequence[str],
    draft: str,
    user_text: str,
    channel_id: int | str | None,
    guild_id: int | None = None,
) -> None:
    """送信ゲートの事故系結果を横断台帳 bot_activities (kind=gate_incident) へ記録する（§147, fail-open）。"""
    try:
        if not _gate_cfg_bool("GATE_INCIDENT_LOG_ENABLED", True):
            return
        if not _gate_cfg_bool("ACTIVITY_LOG_ENABLED", True):
            return
        summary = str(draft or "").strip()[:400]
        if not summary:
            return
        store = getattr(owner, "memory_store", None) if owner is not None else None
        add_act_fn = getattr(store, "add_bot_activity", None)
        if not callable(add_act_fn):
            return
        flags_list = [str(f) for f in (flags or ())]
        detail_payload = {
            "path": str(path or ""),
            "action": str(action or ""),
            "flags": flags_list,
            "user_text": str(user_text or "")[:100],
        }
        detail = json.dumps(detail_payload, ensure_ascii=False)[:800]
        res = add_act_fn(
            persona=cfg_persona_namespace(custom_cfg=cfg),
            front="discord",
            kind="gate_incident",
            summary=summary,
            detail=detail,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        if inspect.isawaitable(res):
            await res
        log.info(
            "[SEND_GATE] incident recorded: path=%s action=%s flags=%s",
            detail_payload["path"],
            detail_payload["action"],
            ",".join(flags_list),
        )
    except Exception as e:
        log.debug("failed to record gate incident (%s/%s): %s", path, action, e)


async def _record_gate_pass(
    *,
    owner: Any,
    path: str,
    risk_flags: Sequence[str],
    draft: str,
    user_text: str,
    channel_id: int | str | None,
    guild_id: int | None = None,
) -> None:
    """意味検査が実行され通過した候補を対照群として bot_activities (kind=gate_pass) へ記録する（§150, fail-open）。"""
    try:
        if not _gate_cfg_bool("GATE_PASS_LOG_ENABLED", True):
            return
        if not _gate_cfg_bool("ACTIVITY_LOG_ENABLED", True):
            return
        summary = str(draft or "").strip()[:400]
        if not summary:
            return
        store = getattr(owner, "memory_store", None) if owner is not None else None
        add_act_fn = getattr(store, "add_bot_activity", None)
        if not callable(add_act_fn):
            return
        risk_flags_list = [str(f) for f in (risk_flags or ())]
        detail_payload = {
            "path": str(path or ""),
            "action": "passed",
            "risk_flags": risk_flags_list,
            "user_text": str(user_text or "")[:100],
        }
        detail = json.dumps(detail_payload, ensure_ascii=False)[:800]
        res = add_act_fn(
            persona=cfg_persona_namespace(custom_cfg=cfg),
            front="discord",
            kind="gate_pass",
            summary=summary,
            detail=detail,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        if inspect.isawaitable(res):
            await res
        log.info(
            "[SEND_GATE] inspected pass recorded: path=%s risk_flags=%s",
            detail_payload["path"],
            ",".join(risk_flags_list),
        )
    except Exception as e:
        log.debug("failed to record gate pass (%s): %s", path, e)


async def _record_candidate_compare(
    *,
    owner: Any,
    scope: str,
    chosen: int,
    candidates: Sequence[CandidateEval],
    summary_text: str,
    channel_id: int | str | None,
    guild_id: int | None = None,
) -> None:
    """候補比較選定の結果を bot_activities (kind=candidate_compare) へ記録する（§165, fail-open）。"""
    try:
        if not _gate_cfg_bool("ACTIVITY_LOG_ENABLED", True):
            return
        summary = str(summary_text or "").strip()[:400]
        if not summary:
            return
        store = getattr(owner, "memory_store", None) if owner is not None else None
        add_act_fn = getattr(store, "add_bot_activity", None)
        if not callable(add_act_fn):
            return
        detail = build_candidate_compare_detail(
            scope=scope,
            chosen=chosen,
            candidates=candidates,
        )
        res = add_act_fn(
            persona=cfg_persona_namespace(custom_cfg=cfg),
            front="discord",
            kind="candidate_compare",
            summary=summary,
            detail=detail,
            guild_id=guild_id,
            channel_id=channel_id,
            source_ref=CANDIDATE_COMPARE_SOURCE_REF,
        )
        if inspect.isawaitable(res):
            await res
        log.info(
            "[CANDIDATE_COMPARE] scope=%s chosen=%d total=%d",
            scope,
            int(chosen),
            len(candidates),
        )
    except Exception as e:
        log.debug("failed to record candidate_compare (%s): %s", scope, e)


_LESSONS_MODULE_PATH = "harness.agent.prompt_parser.prompt_manager.lessons"
_LESSONS_INSPECTION_SCOPE = "discord_reply"
_DEFAULT_INSPECTION_LESSONS_CACHE_TTL_SEC = 600.0
_INSPECTION_LESSONS_CACHE: dict[str, tuple[float, str]] = {}


def clear_inspection_lessons_cache() -> None:
    """意味検査向け教訓 TTL キャッシュをクリアする（§164）。"""
    _INSPECTION_LESSONS_CACHE.clear()


def _resolve_lessons_fns() -> tuple[Callable[..., Any] | None, Callable[..., Any] | None]:
    """共有モジュール（§162: lessons.py）の関数を解決する（不可用時は fail-open で None）。"""
    try:
        mod = importlib.import_module(_LESSONS_MODULE_PATH)
    except Exception:
        return None, None
    fetch_fn = getattr(mod, "fetch_active_lessons", None)
    build_fn = getattr(mod, "build_lessons_block", None)
    if not callable(fetch_fn) or not callable(build_fn):
        return None, None
    return fetch_fn, build_fn


async def _call_fetch_active_lessons(
    fetch_fn: Callable[..., Any],
    store: Any,
    *,
    persona: str,
    scope: str,
) -> Any:
    """fetch_active_lessons を位置/キーワード・sync/async の差を吸収して呼び出す。"""
    call_kwargs: dict[str, Any] = {"persona": persona, "scope": scope}
    pass_store_positional = True
    try:
        params = inspect.signature(fetch_fn).parameters
        if "store" in params and params["store"].kind == inspect.Parameter.KEYWORD_ONLY:
            call_kwargs["store"] = store
            pass_store_positional = False
        elif "memory_store" in params and params["memory_store"].kind == inspect.Parameter.KEYWORD_ONLY:
            call_kwargs["memory_store"] = store
            pass_store_positional = False
    except (ValueError, TypeError):
        pass
    res = fetch_fn(store, **call_kwargs) if pass_store_positional else fetch_fn(**call_kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


def _call_build_lessons_block(build_fn: Callable[..., Any], lessons: Any) -> Any:
    """build_lessons_block を位置/キーワードの差を吸収して呼び出す。"""
    try:
        params = inspect.signature(build_fn).parameters
        if "lessons" in params and params["lessons"].kind == inspect.Parameter.KEYWORD_ONLY:
            return build_fn(lessons=lessons)
    except (ValueError, TypeError):
        pass
    return build_fn(lessons)


async def _get_inspection_lessons_block(owner: Any) -> str:
    """意味検査プロンプト向けの教訓ブロックを TTL キャッシュ付きで取得する（§164, fail-open）。"""
    try:
        if not _gate_cfg_bool("LESSONS_IN_INSPECTION_ENABLED", True):
            return ""
        store = getattr(owner, "memory_store", None) if owner is not None else None
        if store is None:
            return ""
        persona = cfg_persona_namespace(custom_cfg=cfg)
        ttl_sec = max(
            0.0,
            _gate_cfg_float(
                "LESSONS_IN_INSPECTION_CACHE_TTL_SEC",
                _DEFAULT_INSPECTION_LESSONS_CACHE_TTL_SEC,
            ),
        )
        now = time.monotonic()
        cached = _INSPECTION_LESSONS_CACHE.get(persona)
        if cached is not None and (now - cached[0]) < ttl_sec:
            return cached[1]
        fetch_fn, build_fn = _resolve_lessons_fns()
        if fetch_fn is None or build_fn is None:
            return ""
        lessons = await _call_fetch_active_lessons(
            fetch_fn,
            store,
            persona=persona,
            scope=_LESSONS_INSPECTION_SCOPE,
        )
        if not lessons:
            block = ""
        else:
            raw_block = _call_build_lessons_block(build_fn, lessons)
            if inspect.isawaitable(raw_block):
                raw_block = await raw_block
            block = str(raw_block or "").strip()
        _INSPECTION_LESSONS_CACHE[persona] = (now, block)
        return block
    except Exception as e:
        log.debug("[SEND_GATE] inspection lessons fetch skipped (fail-open): %r", e)
        return ""


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
    recent_human_texts: Sequence[str] | None = None,
) -> tuple[str, str]:
    """送信層の単一検査ゲート（§79, §81, §87, §104, §116）。

    すべてのモデル生成テキスト送信経路がここを通過し、
    決定的ガード（キャラクター逸脱・他参加者発言重複・長文エッセイ）と意味検査（§79）を実施して
    (最終本文, action) を返す。
    action: "skipped" | "gated_out" | "passed" | "regenerated" |
            "adopted_violation" | "text_fallback" | "reaction_fallback" |
            "error_fail_open"
    """
    if not _gate_cfg_bool("SEMANTIC_GUARD_ENABLED", True) or not str(reply or "").strip():
        _log_send_gate(path=path, inspected=False, needs_check=False, flags=(), action="skipped")
        return reply, "skipped"

    gate_perf_started_at = time.perf_counter()
    inspected_any = False
    needs_check_any = False
    last_flags: list[str] = []
    last_risk_keys: list[str] = []

    resolved_incident_channel_id = (
        channel_id
        if channel_id is not None
        else getattr(getattr(message, "channel", None), "id", None)
        or getattr(runtime, "cid", None)
    )
    resolved_incident_guild_id = (
        getattr(getattr(message, "guild", None), "id", None)
        or getattr(runtime, "guild_id", None)
    )

    async def _record_incident(action_name: str) -> None:
        await _record_gate_incident(
            owner=owner,
            path=path,
            action=action_name,
            flags=last_flags,
            draft=reply,
            user_text=user_text,
            channel_id=resolved_incident_channel_id,
            guild_id=resolved_incident_guild_id,
        )

    async def _record_pass() -> None:
        await _record_gate_pass(
            owner=owner,
            path=path,
            risk_flags=last_risk_keys,
            draft=reply,
            user_text=user_text,
            channel_id=resolved_incident_channel_id,
            guild_id=resolved_incident_guild_id,
        )

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

        ctx_lines_list = _append_trigger_image_caption_line(
            ctx_lines_list,
            message=message,
            runtime=runtime,
            user_text=user_text,
            speaker=resolved_speaker,
        )

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

        resolved_human_texts: list[str]
        if recent_human_texts is not None:
            resolved_human_texts = [
                str(t).strip() for t in recent_human_texts if t and str(t).strip()
            ]
        elif message is not None and _gate_cfg_bool("REPLY_HUMAN_OVERLAP_GUARD_ENABLED", True):
            owner_bot = getattr(owner, "bot", None) if owner is not None else None
            owner_bot_user = getattr(owner_bot, "user", None) if owner_bot is not None else None
            resolved_bot_uid = (
                getattr(runtime, "bot_user_id", None) if runtime is not None else None
            ) or (getattr(owner_bot_user, "id", None) if owner_bot_user is not None else None)
            owner_cache = getattr(owner, "channel_context_cache", None) if owner is not None else None
            try:
                resolved_human_texts = await _find_recent_human_message_texts(
                    message,
                    bot_user_id=resolved_bot_uid,
                    cache=owner_cache if isinstance(owner_cache, dict) else None,
                    limit=_RECENT_HUMAN_TEXTS_DEFAULT_LIMIT,
                )
            except Exception as human_fetch_err:
                log.warning(
                    "[SEND_GATE] recent human message fetch failed (fail-open, path=%s): %r",
                    path,
                    human_fetch_err,
                )
                resolved_human_texts = []
        else:
            resolved_human_texts = []

        def _check_human_sentence_overlap(candidate_text: str) -> str | None:
            if not resolved_human_texts or not _gate_cfg_bool("REPLY_HUMAN_OVERLAP_GUARD_ENABLED", True):
                return None
            try:
                return _find_human_sentence_overlap(candidate_text, resolved_human_texts)
            except Exception as overlap_err:
                log.warning(
                    "[SEND_GATE] human sentence overlap check failed (fail-open, path=%s): %r",
                    path,
                    overlap_err,
                )
                return None

        def _check_overlong_essay(candidate_text: str) -> str | None:
            if not _gate_cfg_bool("REPLY_OVERLONG_GUARD_ENABLED", True):
                return None
            try:
                return _check_overlong_essay_reply(candidate_text)
            except Exception as overlong_err:
                log.warning(
                    "[SEND_GATE] overlong essay check failed (fail-open, path=%s): %r",
                    path,
                    overlong_err,
                )
                return None

        async def _inspect_llm_call(prompt_text: str) -> Any:
            model, model_reason = resolve_inspection_model()
            log.info("[INSPECTION_MODEL] model=%s reason=%s", model, model_reason)
            return await call_ollama_json(
                prompt_text,
                system_prompt="あなたは会話の整合性を検証する意味検査AIです。",
                schema=SEMANTIC_INSPECTION_SCHEMA,
                model=model,
                temperature=0.0,
                think=False,
                timeout_sec=max(_gate_cfg_float("SEMANTIC_GUARD_TIMEOUT_SEC", 15.0), 1.0),
            )

        async def _evaluate_candidate(
            candidate_text: str,
        ) -> tuple[bool, bool, bool, list[str], str, dict[str, bool]]:
            last_risk_keys[:] = []
            if _check_character_deviation(candidate_text):
                return True, False, False, ["character_deviation"], "character_deviation", {}
            overlap_sentence = _check_human_sentence_overlap(candidate_text)
            if overlap_sentence:
                return (
                    True,
                    False,
                    False,
                    ["human_sentence_overlap"],
                    f"human_sentence_overlap: {overlap_sentence}",
                    {},
                )
            overlong_reason = _check_overlong_essay(candidate_text)
            if overlong_reason:
                return (
                    True,
                    False,
                    False,
                    ["overlong_essay"],
                    overlong_reason,
                    {},
                )
            risk = eff_assess(
                reply_text=candidate_text,
                intent_info=intent_info,
                denied_claims=resolved_claims,
                previous_reply_text=bot_previous_text,
                context_lines=ctx_lines_list,
                current_speaker=resolved_speaker,
                bot_name=resolved_bot_name,
                user_text=user_text or None,
            )
            extracted_risks: list[str] = []
            if isinstance(risk, dict):
                raw_reasons = risk.get("reasons")
                if isinstance(raw_reasons, (list, tuple)):
                    for item in raw_reasons:
                        s = str(item or "").strip()
                        if s and s not in extracted_risks:
                            extracted_risks.append(s)
                for k, v in risk.items():
                    if k in ("needs_check", "reasons", "substring_hit"):
                        continue
                    if v is True:
                        ks = str(k).strip()
                        if ks and ks not in extracted_risks:
                            extracted_risks.append(ks)
            last_risk_keys[:] = extracted_risks
            if risk.get("substring_hit"):
                return True, True, False, ["substring_hit"], f"substring_hit: {risk['substring_hit']}", {}
            if not risk.get("needs_check"):
                return False, False, False, [], "", {}
            has_actor_risk = bool(
                risk.get("actor_attribution_risk")
                or ("actor_attribution_risk" in (risk.get("reasons") or []))
            )
            has_addressee_risk = bool(
                "addressee_claim_risk" in (risk.get("reasons") or [])
            )
            has_target_claim_risk = bool(
                risk.get("target_claim_mismatch_risk")
                or ("target_claim_mismatch_risk" in (risk.get("reasons") or []))
            )
            has_literal_gloss_risk = bool(
                risk.get("literal_gloss_risk")
                or ("literal_gloss_risk" in (risk.get("reasons") or []))
            )
            has_image_claim_risk = bool(
                risk.get("ungrounded_image_claim")
                or ("ungrounded_image_claim" in (risk.get("reasons") or []))
            )
            inspect_context_lines = (
                ctx_lines_list
                if (
                    has_actor_risk
                    or has_addressee_risk
                    or has_target_claim_risk
                    or has_literal_gloss_risk
                    or has_image_claim_risk
                    or (path == "break" and ctx_lines_list)
                )
                else None
            )
            lessons_block = await _get_inspection_lessons_block(owner)
            if lessons_block:
                insp = await eff_inspect(
                    llm_caller_fn=_inspect_llm_call,
                    user_text=user_text,
                    bot_previous_text=bot_previous_text or "",
                    generated_reply=candidate_text,
                    denied_claims=resolved_claims,
                    context_lines=inspect_context_lines,
                    recent_events=resolved_recent_events if has_actor_risk else None,
                    bot_name=resolved_bot_name,
                    lessons_block=lessons_block,
                )
            else:
                insp = await eff_inspect(
                    llm_caller_fn=_inspect_llm_call,
                    user_text=user_text,
                    bot_previous_text=bot_previous_text or "",
                    generated_reply=candidate_text,
                    denied_claims=resolved_claims,
                    context_lines=inspect_context_lines,
                    recent_events=resolved_recent_events if has_actor_risk else None,
                    bot_name=resolved_bot_name,
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
        first_risk_keys = list(last_risk_keys)
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
                await _record_incident("gated_out")
                return reply, "gated_out"
            _log_send_gate(
                path=path,
                inspected=True,
                needs_check=True,
                flags=(),
                action="passed",
            )
            selected_reply = reply
            if inspected:
                await _record_pass()
                if (
                    regenerate_fn is not None
                    and _gate_cfg_bool(
                        "REPLY_CANDIDATE_COMPARE_ENABLED",
                        DEFAULT_CANDIDATE_COMPARE_ENABLED,
                    )
                ):
                    target_count = _gate_cfg_int("REPLY_CANDIDATE_COUNT", DEFAULT_CANDIDATE_COUNT)
                    max_elapsed_sec = _gate_cfg_float(
                        "REPLY_CANDIDATE_COMPARE_MAX_ELAPSED_SEC",
                        DEFAULT_CANDIDATE_COMPARE_MAX_ELAPSED_SEC,
                    )
                    if (
                        target_count >= 2
                        and max_elapsed_sec > 0.0
                        and _get_turn_elapsed_sec(gate_perf_started_at) < max_elapsed_sec
                        and not _is_turn_budget_exceeded()
                    ):
                        async def _eval_for_compare(
                            cand_text: str,
                        ) -> tuple[bool, list[str], int]:
                            alt_v, _, _, alt_f, _, _ = await _evaluate_candidate(cand_text)
                            return alt_v, alt_f, len(last_risk_keys)

                        async def _gen_for_compare(alt_note: str) -> str:
                            return await _invoke_regenerate_fn(regenerate_fn, alt_note)

                        async def _rec_for_compare(
                            chosen_idx: int,
                            cands: Sequence[CandidateEval],
                            summary_text: str,
                        ) -> None:
                            await _record_candidate_compare(
                                owner=owner,
                                scope="reply",
                                chosen=chosen_idx,
                                candidates=cands,
                                summary_text=summary_text,
                                channel_id=resolved_incident_channel_id,
                                guild_id=resolved_incident_guild_id,
                            )

                        selected_reply = await run_reply_candidate_compare(
                            first_reply=reply,
                            first_risk_count=len(first_risk_keys),
                            user_text=user_text,
                            bot_previous_text=bot_previous_text,
                            path=path,
                            target_count=target_count,
                            is_over_time_fn=lambda: (
                                _get_turn_elapsed_sec(gate_perf_started_at) >= max_elapsed_sec
                                or _is_turn_budget_exceeded()
                            ),
                            generate_fn=_gen_for_compare,
                            evaluate_fn=_eval_for_compare,
                            record_fn=_rec_for_compare,
                            logger=log,
                        )
            return selected_reply, "passed"

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
                elif "human_sentence_overlap" in last_flags:
                    delib_correction = _build_human_overlap_deliberation_note(reason)
                elif "overlong_essay" in last_flags:
                    delib_correction = _build_overlong_essay_deliberation_note(reason)
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
                        await _record_incident("regenerated")
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
                await _record_incident("adopted_violation")
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
                await _record_incident("text_fallback")
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
                await _record_incident("reaction_fallback")
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
            await _record_incident("adopted_violation")
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
            await _record_incident("reaction_fallback")
            return "", "reaction_fallback"

        _log_send_gate(
            path=path,
            inspected=inspected_any,
            needs_check=needs_check_any,
            flags=last_flags,
            action="adopted_violation",
            budget_exceeded=False,
        )
        await _record_incident("adopted_violation")
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
        try:
            from harness.agent.prompt_parser.prompt_manager.conversation_activity import mark_channel_activity

            mark_channel_activity(int(getattr(target_message.channel, "id", 0) or 0))
        except Exception:
            log.debug("conversation activity mark failed (bot reply)", exc_info=True)
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
        record_bot_sent_message(
            getattr(getattr(target_message, "channel", None), "id", None),
            sent,
            kind="reply",
            overwrite_same_id=True,
            text=payload,
        )
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
        recent_human_texts: Sequence[str] | None = None,
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
            recent_human_texts=recent_human_texts,
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
        recent_human_texts: Sequence[str] | None = None,
    ) -> discord.Message | None:
        try:
            from harness.agent.prompt_parser.prompt_manager.conversation_activity import mark_channel_activity

            mark_channel_activity(int(getattr(message.channel, "id", 0) or 0))
        except Exception:
            log.debug("conversation activity mark failed (bot reply)", exc_info=True)
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
            or recent_human_texts is not None
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
                recent_human_texts=recent_human_texts,
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
                        chunk_sent = await channel.send(chunk)
                        record_bot_sent_message(
                            getattr(channel, "id", None),
                            chunk_sent,
                            kind="reply",
                            overwrite_same_id=True,
                            text=chunk,
                        )
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
                            fallback_sent = await channel.send(combined)
                            record_bot_sent_message(
                                getattr(channel, "id", None),
                                fallback_sent,
                                kind="reply",
                                overwrite_same_id=True,
                                text=combined,
                            )
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

