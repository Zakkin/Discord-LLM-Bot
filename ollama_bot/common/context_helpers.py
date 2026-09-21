import asyncio
import re
import logging
from typing import Any, Optional
from collections import deque
import discord

from .config_helpers import cfg, cfg_int
from .discord_helpers import author_id, channel_id, content, recent_context_items, to_context_line, message_created_at_ts
from .ollama_helpers import (
    call_ollama, 
    looks_like_abnormal_assistant_reply, 
    looks_like_reasoning_leak, 
    looks_like_unusable_assistant_reply,
    sanitize_generated_reply, 
    truncate_text
)
from .history_helpers import fetch_recent_context_lines
from .singing_helpers import compact_singing_context_lines
from ..ollama_chat_.ollama_chat_texts import CONTEXT_CHECKER_SYSTEM_PROMPT

log = logging.getLogger("ollama_bot.context_helpers")


def _context_max_age_sec() -> float:
    return max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0)


def _last_assistant_max_age_sec() -> float:
    # 24 hours default for remembering the last thing the bot said, 
    # to avoid context breaks when users reply late to a question.
    return max(float(cfg("LAST_ASSISTANT_MAX_AGE_SEC", 86400.0) or 86400.0), _context_max_age_sec())

_CONTEXT_FLOW_FOLLOW_HINTS = (
    r"同意", r"継続", r"続き", r"引き継", r"関連", r"文脈あり", r"会話の流れ", r"そのまま",
)
_CONTEXT_FLOW_BREAK_HINTS = (
    r"別話題", r"別件", r"新しい話題", r"新規", r"独立", r"単独", r"文脈なし", r"話題転換", r"切り替",
)

def _classifier_model_name() -> str:
    return cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))

def _append_unique_context_line(lines: list[str], line: str | None) -> None:
    if line and (not lines or lines[-1] != line):
        lines.append(line)

async def _build_summary_context_lines(
    message: discord.Message,
    sent: discord.Message,
    *,
    base_context_lines: list[str],
    bot_user_id: Optional[int],
    fetch_limit: int | None = None,
    trigger: int | None = None,
) -> list[str]:
    resolved_trigger = trigger
    if resolved_trigger is None:
        resolved_trigger = max(int(cfg_int("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20), 1)

    resolved_fetch_limit = fetch_limit
    if resolved_fetch_limit is None:
        window = max(int(cfg_int("CONTEXT_WINDOW_MESSAGES", 7) or 7), 1)
        resolved_fetch_limit = max(resolved_trigger, window)

    lines = list(base_context_lines[-resolved_fetch_limit:])
    if len(lines) < resolved_trigger:
        lines = await fetch_recent_context_lines(
            message,
            limit=resolved_fetch_limit,
            bot_user_id=bot_user_id,
            max_age_sec=_context_max_age_sec(),
        )

    _append_unique_context_line(lines, to_context_line(message, bot_user_id=bot_user_id))
    _append_unique_context_line(lines, to_context_line(sent, bot_user_id=bot_user_id))
    return lines[-(resolved_fetch_limit + 2):] if len(lines) > (resolved_fetch_limit + 2) else lines

def _last_assistant_text_is_error_placeholder(text: str | None) -> bool:
    t = (text or "").strip()
    return t.startswith("AI応答でエラーが発生しました")

async def _find_last_assistant_message_text(
    message: discord.Message,
    *,
    bot_user_id: Optional[int],
    cache: dict[int, deque[dict[str, Any]]],
) -> str:
    cid = channel_id(message)
    if cid is not None:
        for item in reversed(recent_context_items(
            cache,
            cid,
            before_message=message,
            max_age_sec=_last_assistant_max_age_sec(),
        )):
            if item.get("author_id") == bot_user_id:
                line = item.get("line") or ""
                text = line.split(": ", 1)[1].strip() if ": " in line else line.strip()
                if _last_assistant_text_is_error_placeholder(text):
                    return ""
                if looks_like_abnormal_assistant_reply(text):
                    return ""
                return truncate_text(text, int(cfg("OLLAMA_PROMPT_LAST_ASSISTANT_MAX_CHARS", 220) or 220))

    if bot_user_id is None:
        return ""

    try:
        async for m in message.channel.history(limit=12, before=message, oldest_first=False):
            current_ts = message_created_at_ts(message)
            item_ts = message_created_at_ts(m)
            max_age_sec = _last_assistant_max_age_sec()
            if current_ts is not None and item_ts is not None and max_age_sec > 0 and (current_ts - item_ts) > max_age_sec:
                continue
            if author_id(m) == bot_user_id:
                text = content(m)
                if _last_assistant_text_is_error_placeholder(text):
                    return ""
                if looks_like_abnormal_assistant_reply(text):
                    return ""
                return truncate_text(text, int(cfg("OLLAMA_PROMPT_LAST_ASSISTANT_MAX_CHARS", 220) or 220))
    except Exception as e:
        log.debug("last assistant fetch failed: %s", e)
    return ""

def _looks_like_clear_short_message(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"https?://|www\.", t):
        return True
    if len(t) <= 24:
        if re.search(r"[?？!！。…]", t):
            return True
        if re.search(r"^(?:了解|わかった|はい|うん|なるほど|草|w+|乙|ありがとう|どういうこと|何それ|なんで|テスト|ぽこあ)$", t):
            return True
    return False

def _looks_like_context_dependent_message(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    compact = re.sub(r"\s+", "", t)
    if len(compact) > 24:
        return False
    return bool(re.search(
        r"^(それ|これ|あれ|さっき|今の|その話|この話|あの話|続き|なんで|どういうこと|どゆこと|それで|で[、,]?)",
        compact,
    ))

def _looks_like_standalone_new_topic(text: str, *, last_assistant_text: str = "") -> bool:
    t = (text or "").strip()
    if not t:
        return False
    compact = re.sub(r"\s+", "", t)
    if len(compact) >= 10 and re.search(r"(X|Twitter|ツイッター).{0,8}タイムライン", t, flags=re.IGNORECASE):
        return True
    if _looks_like_context_dependent_message(t):
        return False
    if re.search(r"https?://|www\.", t):
        return True
    if len(compact) >= 10 and t.endswith(("?", "？")):
        prev_is_question = bool(last_assistant_text and re.search(r"[?？]", last_assistant_text))
        if not prev_is_question:
            return True
    if len(compact) >= 14 and re.search(
        r"(教えて|教えろ|どうする|どう思う|してくれ|してほしい|見せて|要約して|調べて|解説して|おすすめ|比較|理由|って何|とは)",
        t,
    ):
        return True
    return False

_SHORT_REACTION_PATTERN = re.compile(
    r"^(?:凌駕したか|確かに|それな|負けた|草|草生える|なるほど|そっか|そうか|正論|わかる|分かる|同意|ほんと|本当|うそ|嘘|まじか|マジか|すごい|凄い|やばい|ヤバい|かわいい|可愛い|それ|同感|完敗|勝てん|無理|草|笑|お前|おい|え|えっ|は|は？|へえ|ほお|ほう)[!！?？wW\s^〜~]*$",
    flags=re.IGNORECASE,
)

def _strip_reply_prefix(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^（.+?の「.+?」への返信）\s*", "", t)
    t = re.sub(r"^（.+?への返信）\s*", "", t)
    return t.strip()

def _looks_like_reply_reference(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(re.search(r"^（.+?への返信）|^（.+?の「.+?」への返信）", t))

def _should_skip_context_check(latest_text: str, context_lines: list[str]) -> bool:
    t = _strip_reply_prefix(latest_text)
    if not t:
        return True
    if _looks_like_clear_short_message(t):
        return True
    if _SHORT_REACTION_PATTERN.match(t):
        return True
    if len(t) <= 14:
        return True
    if len(context_lines) <= 1:
        return True
    return False

_CAUSAL_OR_FOLLOWUP_CONJUNCTION_PATTERN = re.compile(
    r"^(?:じゃあ|では|なら|だったら|それなら|じゃ[、,\s]|んで[、,\s]|なので|だから|ですから|それで|ゆえに|だからこそ|というか|っていうか|つまり|要するに|要は|となると|だとすると)[、,\s]*",
    flags=re.IGNORECASE,
)

def _looks_like_reply_to_assistant(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return "（あなたの「" in t and "」への返信）" in t

def _looks_like_causal_or_followup_message(text: str) -> bool:
    body = _strip_reply_prefix(text)
    if not body:
        return False
    return bool(_CAUSAL_OR_FOLLOWUP_CONJUNCTION_PATTERN.search(body))

def _looks_like_followup_question(text: str) -> bool:
    body = _strip_reply_prefix(text)
    if not body:
        return False
    compact = re.sub(r"\s+", "", body)
    if len(compact) > 30:
        return False
    if re.search(r"^(?:じゃあ|では|なら|だったら|それなら|じゃ)", compact):
        if re.search(r"(?:[?？]|は|はどう|はどうなの|って[?？]|ってどう)[!！?？\s]*$", compact):
            return True
    return False

def _looks_like_reply_to_bot_topic(latest_text: str, context_lines: list[str]) -> bool:
    t = (latest_text or "").strip()
    if not t:
        return False
    has_recent_assistant = any(
        (line or "").strip().startswith("Assistant:") for line in (context_lines[-4:] if context_lines else [])
    )
    if not has_recent_assistant:
        return False
    compact = re.sub(r"\s+", "", t)
    if len(compact) > 30:
        return False
    if re.search(r"^まだ", t) and re.search(r"(ない|ねえ|ません|わけない|じゃない|してない|してない|できない)", t):
        return True
    if re.search(r"(わけない|するわけ|するわけない|してるわけ|してるわけない)([。！!]|$)", t):
        return True
    return False

def _infer_context_flow_without_llm(
    latest_text: str,
    context_lines: list[str],
    last_assistant_text: str = "",
) -> str | None:
    t = (latest_text or "").strip()
    if not t:
        return "BREAK"
    body = _strip_reply_prefix(t)
    prev_is_question = bool(last_assistant_text and re.search(r"[?？]", last_assistant_text))
    if prev_is_question and _looks_like_clear_short_message(body):
        return "FOLLOW"
    if _SHORT_REACTION_PATTERN.match(body):
        return "FOLLOW"
    if _looks_like_followup_question(t):
        return "FOLLOW"
    if _looks_like_standalone_new_topic(body, last_assistant_text=last_assistant_text):
        return "BREAK"
    if _looks_like_reply_reference(t):
        return "FOLLOW"
    if _looks_like_causal_or_followup_message(t):
        return "FOLLOW"
    if len(context_lines) <= 1:
        if prev_is_question:
            return None
        return "BREAK"
    if _looks_like_reply_to_bot_topic(t, context_lines):
        return "FOLLOW"
    if _should_skip_context_check(t, context_lines):
        return "FOLLOW"
    return None

def _strip_context_flow_choice_prefix(text: str) -> str:
    normalized = str(text or "").strip()
    normalized = re.sub(r"^[\s\[(（【]*\d+[\s\])）】.．:：-]*", "", normalized)
    normalized = re.sub(r"^[\s\-*・]+", "", normalized)
    return normalized.strip()

def _match_context_flow_hint(text: str) -> str | None:
    normalized = _strip_context_flow_choice_prefix(text)
    if not normalized:
        return None
    follow_hint = any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in _CONTEXT_FLOW_FOLLOW_HINTS)
    break_hint = any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in _CONTEXT_FLOW_BREAK_HINTS)
    if follow_hint and not break_hint:
        return "FOLLOW"
    if break_hint and not follow_hint:
        return "BREAK"
    return None

def _extract_context_flow_label(result_text: str) -> str | None:
    cleaned = sanitize_generated_reply(result_text)
    candidates = [cleaned, *cleaned.splitlines()]
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", _strip_context_flow_choice_prefix(candidate)).upper()
        if not normalized:
            continue
        has_follow = bool(re.search(r"\bFOLLOW\b", normalized))
        has_break = bool(re.search(r"\bBREAK\b", normalized))
        if has_follow and not has_break:
            return "FOLLOW"
        if has_break and not has_follow:
            return "BREAK"
        hint_label = _match_context_flow_hint(normalized)
        if hint_label is not None:
            return hint_label
    return None

def _looks_like_noisy_context_flow_output(result_text: str) -> bool:
    raw = str(result_text or "")
    cleaned = sanitize_generated_reply(raw)
    lowered = cleaned.lower().strip()
    if not lowered:
        return True
    if re.match(r"^\s*(?:assistant|ai)\s*[:：]", raw, flags=re.IGNORECASE):
        return True
    if _match_context_flow_hint(cleaned) is not None:
        return False
    if looks_like_unusable_assistant_reply(cleaned):
        return True
    if looks_like_reasoning_leak(raw):
        return True
    if "thinking process" in lowered:
        return True
    if re.fullmatch(r"[\d.\s%]+", lowered):
        return True
    return False

def _context_has_recent_assistant_message(context_lines: list[str]) -> bool:
    return any(
        (line or "").strip().startswith("Assistant:") for line in (context_lines[-5:] if context_lines else [])
    )

async def _check_context_flow(
    message: discord.Message,
    context_lines: list[str],
    latest_user_text: str | None = None,
    last_assistant_text: str = "",
) -> str:
    cleaned_context_lines = compact_singing_context_lines(context_lines)
    latest_text = (latest_user_text or content(message) or "（本文なし）").strip()
    heuristic = _infer_context_flow_without_llm(latest_text, cleaned_context_lines, last_assistant_text=last_assistant_text)
    if heuristic is not None:
        return heuristic

    effective_context = list(cleaned_context_lines)
    if not any((line or "").startswith("Assistant:") for line in effective_context[-5:]) and last_assistant_text:
        effective_context = [f"Assistant: {last_assistant_text}"] + effective_context

    prompt = "会話履歴:\n" + "\n".join(effective_context[-5:]) + f"\n\n最新の発言: {latest_text}\n判定:"
    try:
        result = await call_ollama(
            prompt,
            system_prompt=CONTEXT_CHECKER_SYSTEM_PROMPT,
            think=False,
            model=_classifier_model_name(),
            timeout_sec=float(cfg("OLLAMA_CONTEXT_CHECK_TIMEOUT_SEC", 2.0) or 2.0),
            retries=0,
            temperature=0.0,
            num_predict=max(int(cfg("OLLAMA_CONTEXT_CHECK_NUM_PREDICT", 16) or 16), 8),
            log_retryable_errors=False,
            log_http_errors=False,
        )
    except Exception as e:
        if getattr(e, "status", None) in (429, 503) or isinstance(e, asyncio.TimeoutError):
            log.debug("context flow check skipped due to busy/timeout: %s", e)
        else:
            log.warning("context flow check failed: %s", e)
        return "FOLLOW" if _context_has_recent_assistant_message(cleaned_context_lines) else "BREAK"

    label = _extract_context_flow_label(result)
    if label is not None:
        return label

    if _looks_like_noisy_context_flow_output(result):
        fallback = _infer_context_flow_without_llm(
            latest_text,
            cleaned_context_lines,
            last_assistant_text=last_assistant_text,
        )
        log.debug(
            "context flow returned noisy output; falling back to %s: %r",
            fallback or "BREAK",
            result,
        )
        return fallback or "BREAK"
    else:
        log.warning("context flow returned unexpected label: %r", result)

    return _infer_context_flow_without_llm(latest_text, cleaned_context_lines) or "BREAK"
