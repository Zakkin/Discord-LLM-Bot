import re
from typing import Any, Optional
from datetime import datetime
import discord

from lib.date_utils import JST
from .config_helpers import cfg
from .discord_helpers import (
    author_id,
    channel_id,
    content,
    display_name,
    get_japanese_weekday,
    resolve_reference_message,
)
from .fact_check import extract_urls
from ..ollama_chat_.ollama_chat_texts import TIME_QUERY_KEYWORDS, build_weather_comment_prompt



async def _build_weather_comment(weather_summary: str) -> str:
    """Weather comment helper."""
    from .ollama_helpers import call_ollama
    reply = await call_ollama(build_weather_comment_prompt(weather_summary), think=False)
    return _trim_reply(reply, max_len=120)


def _trim_reply(text: str, max_len: int = 1900) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"

def _strip_user_echo_prefix(user_text: str, reply_text: str) -> str:
    from .ollama_helpers import _normalize_compare_text
    user = (user_text or "").strip()
    reply = (reply_text or "").strip()
    if not user or not reply:
        return reply

    norm_user = _normalize_compare_text(user)
    norm_reply = _normalize_compare_text(reply)

    # 1. 完全一致（または正規化後の一致）で始まるかチェック
    if norm_reply.startswith(norm_user):
        user_lines = [l.strip() for l in user.splitlines() if l.strip()]
        reply_lines = reply.splitlines()
        
        match_line_count = 0
        for i, u_line in enumerate(user_lines):
            if i < len(reply_lines) and _normalize_compare_text(reply_lines[i]) == _normalize_compare_text(u_line):
                match_line_count += 1
            else:
                break
        
        if match_line_count > 0:
            remaining_lines = reply_lines[match_line_count:]
            while remaining_lines and not remaining_lines[0].strip():
                remaining_lines.pop(0)
            return "\n".join(remaining_lines).strip()

    # 2. ユーザーテキストの末尾部分が返信の冒頭にあるかチェック（履歴のエコー対策）
    # Qwen 3.6などはプロンプトの最後の数行を繰り返すことがある
    user_lines = [l.strip() for l in user.splitlines() if l.strip()]
    if user_lines:
        # A. 行単位でのマッチ（最大3行）
        for lookback in range(1, min(4, len(user_lines) + 1)):
            tail_fragment_lines = user_lines[-lookback:]
            tail_fragment_norm = _normalize_compare_text("\n".join(tail_fragment_lines))
            
            if norm_reply.startswith(tail_fragment_norm):
                # マッチした行数分を返信から削る
                reply_lines = reply.splitlines()
                current_idx = 0
                match_count = 0
                # 空行を飛ばしつつ、tail_fragment_lines に含まれる行を順番に探す
                for frag_line in tail_fragment_lines:
                    frag_norm = _normalize_compare_text(frag_line)
                    while current_idx < len(reply_lines):
                        line_norm = _normalize_compare_text(reply_lines[current_idx])
                        if not line_norm:
                            current_idx += 1
                            continue
                        if line_norm == frag_norm:
                            current_idx += 1
                            match_count += 1
                            break
                        current_idx += 1
                
                if match_count == len(tail_fragment_lines):
                    remaining = reply_lines[current_idx:]
                    while remaining and not remaining[0].strip():
                        remaining.pop(0)
                    return "\n".join(remaining).strip()

        # B. 最近の数行について部分的な一致（接尾辞の一致）をチェック
        # Qwen 3.6などは履歴の末尾部分を拾ってエコーすることがある
        for line in reversed(user_lines[-5:]):
            line_norm = _normalize_compare_text(line)
            if len(line_norm) < 10:
                continue
                
            # 最小10文字、最大はその行の長さ分
            # (長い方から探す)
            for length in range(len(line_norm), 9, -1):
                suffix = line_norm[-length:]
                if norm_reply.startswith(suffix):
                    # マッチした部分が返信の冒頭にある場合、その行を削る
                    reply_lines = reply.splitlines()
                    for idx, r_line in enumerate(reply_lines):
                        if not r_line.strip():
                            continue
                        if _normalize_compare_text(r_line).startswith(suffix):
                            remaining = reply_lines[idx+1:]
                            while remaining and not remaining[0].strip():
                                remaining.pop(0)
                            return "\n".join(remaining).strip()
                    break

    return reply

def _build_unclear_intent_reply() -> str:
    return cfg("OLLAMA_UNCLEAR_INTENT_FALLBACK", "意味が取りづらい。何をしてほしいのか、もう少しはっきり言ってくれ。")

def _merge_user_text_with_media_analysis(user_text: str, media_analysis_text: str) -> str:
    base = (user_text or "").strip()
    extra = (media_analysis_text or "").strip()
    if base and extra:
        return f"{base}\n\nメディア補足:\n{extra}"
    return extra or base

def _format_reply(text: str, *, prefix: str = "") -> str:
    from .ollama_helpers import (
        extract_first_user_facing_reply,
        sanitize_generated_reply,
        looks_like_unusable_assistant_reply,
        looks_like_prompt_leak,
        looks_like_reasoning_leak,
        looks_like_multi_turn_output,
        looks_like_non_japanese_reply,
        looks_like_abnormal_assistant_reply,
    )
    raw_text = str(text or "")
    leak_problem = (
        bool(raw_text.strip())
        and (
            looks_like_prompt_leak(raw_text)
            or looks_like_reasoning_leak(raw_text)
            or looks_like_multi_turn_output(raw_text)
            or looks_like_non_japanese_reply(raw_text)
        )
    )
    unusable = bool(raw_text.strip()) and looks_like_unusable_assistant_reply(raw_text)
    abnormal = bool(raw_text.strip()) and looks_like_abnormal_assistant_reply(raw_text)
    cleaned = extract_first_user_facing_reply(text)
    if cleaned and (looks_like_unusable_assistant_reply(cleaned) or looks_like_abnormal_assistant_reply(cleaned)):
        cleaned = ""
    if not cleaned and not abnormal:
        cleaned = sanitize_generated_reply(text)
        if cleaned and (looks_like_unusable_assistant_reply(cleaned) or looks_like_abnormal_assistant_reply(cleaned)):
            cleaned = ""
    if not cleaned and leak_problem:
        cleaned = extract_first_user_facing_reply(
            cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。")
        )
    if not cleaned:
        cleaned = cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "今のはうまく返せなかった。もう一回言ってくれ。")
        if looks_like_unusable_assistant_reply(cleaned):
            cleaned = "今のはうまく返せなかった。もう一回言ってくれ。"
    return f"{prefix}{_trim_reply(cleaned).rstrip()}\n"

def _looks_like_interpretable_monologue(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if len(t) >= 8 and re.search(r"[ぁ-んァ-ヶ一-龠]", t) and not t.endswith(("?", "？")):
        return True
    return bool(re.search(
        r"(眠い|ねむ|疲れ|つかれ|しんど|だる|暇|おはよ|こんにちは|こんばんは|ただいま|おやすみ|うれし|嬉し|かなし|悲し|寂し|さみし|腹減|おなかす|寝る|寝たい|起きた|帰る|行く)",
        t,
    ))

def _should_use_unclear_intent_fallback(user_text: str, intent_info: dict[str, Any]) -> bool:
    if not bool(intent_info.get("should_clarify")):
        return False
    t = (user_text or "").strip()
    if not t:
        return True
    if extract_urls(t):
        return False
    if not bool(intent_info.get("target_is_ai")) and _looks_like_interpretable_monologue(t):
        return False
    compact = re.sub(r"[\s…。！？!?ー〜~・.]+", "", t)
    if len(compact) >= 6 and re.search(r"[ぁ-んァ-ヶ一-龠]", compact):
        return False
    return True

def _build_role_flip_fallback_reply() -> str:
    return cfg("OLLAMA_ROLE_FLIP_FALLBACK", "は？ 俺がやる必要あるかよ。嫌だけど。")

def looks_like_time_query(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and any(k in t for k in TIME_QUERY_KEYWORDS)

def _looks_like_date_query(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(
        re.search(r"(今|いま|現在|今年|今日).{0,8}(何年|年|日付|何日|何月何日)", t)
        or re.search(r"(何年|日付|何月何日|何日).{0,8}(今|いま|現在|今年|今日)", t)
    )


def build_current_time_reply(user_text: str | None = None) -> str:
    now_jst = datetime.now(JST)
    hh = str(now_jst.hour)
    mm = str(now_jst.minute)
    if _looks_like_date_query(user_text or ""):
        template = cfg(
            "OLLAMA_CURRENT_DATETIME_REPLY_TEMPLATE",
            "今は日本時間で{year}年{month}月{day}日({weekday}曜日)、{hour}時{minute}分だ。",
        )
        try:
            return str(template).format(
                year=now_jst.year,
                month=now_jst.month,
                day=now_jst.day,
                weekday=get_japanese_weekday(now_jst),
                hour=hh,
                minute=mm,
            )
        except Exception:
            return f"今は日本時間で{now_jst.year}年{now_jst.month}月{now_jst.day}日({get_japanese_weekday(now_jst)}曜日)、{hh}時{mm}分だ。"
    template = cfg("OLLAMA_CURRENT_TIME_REPLY_TEMPLATE", "今は日本時間で{hour}時{minute}分だ。")
    try:
        return str(template).format(hour=hh, minute=mm)
    except Exception:
        return f"今は日本時間で{hh}時{mm}分だ。"

async def _build_reply_context_user_text(
    message: discord.Message,
    user_text: str,
    *,
    bot_user_id: Optional[int] = None,
) -> str:
    base = (user_text or "").strip()
    ref_msg = await resolve_reference_message(message)
    if not ref_msg:
        return base

    ref_author_name = "あなた" if bot_user_id is not None and author_id(ref_msg) == bot_user_id else display_name(ref_msg)
    ref_text = re.sub(r"\s+", " ", content(ref_msg)).strip()
    from .ollama_helpers import truncate_text
    ref_text = truncate_text(ref_text, max(int(cfg("OLLAMA_REPLY_REFERENCE_MAX_CHARS", 80) or 80), 20))

    prefix = f"（{ref_author_name}への返信）"
    if ref_text:
        prefix = f"（{ref_author_name}の「{ref_text}」への返信）"
    return f"{prefix}\n{base}".strip() if base else prefix
