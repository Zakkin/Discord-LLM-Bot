"""OllamaChat系Mixinで共有するDiscord、LLM、記憶、プロンプト補助処理を集約する。"""
import asyncio
import contextlib
import difflib
import json
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from lib.date_utils import JST


import discord
from discord import app_commands
from discord.ext import commands

from ..common.chat_prompt import (
    _analyze_pre_reply_context,
    _build_user_prompt,
)
from ..common import context_helpers as _context_helpers_mod
from ..common import reply_helpers as _reply_helpers_mod
from ..common import umigame_helpers as _umigame_helpers_mod
from ..common.config_helpers import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_int_set,
    cfg_managed_channel_ids,
    cfg_primary_channel_id,
    model_supports_thinking,
)
from ..common.discord_helpers import (
    append_context_message,
    author_id,
    channel_id,
    content,
    display_name,
    has_anon_buttons,
    prune_stale_context_cache,
    recent_context_items,
    remove_context_message,
    resolve_reference_message,
    to_context_line,
)
from ..common.emotion_helpers import (
    EMOTION_KEYS,
    EMOTION_LABEL_TO_FACE,
    EMOTION_LABEL_TO_JA,
    build_emotion_bio_text,
    build_emotion_status_text,
    build_memory_emotion_tags,
    build_nickname_with_face,
    build_reply_emotion_guidance,
    choose_emotion_reaction_emojis,
    clamp_score,
    emotion_bio_enabled,
    emotion_blend_ratio,
    emotion_decay,
    emotion_scoring_enabled,
    fallback_reason_text,
    looks_like_bad_reason_text,
    merge_emotion_scores,
    normalize_reason_text,
    should_force_deliberation_from_emotions,
)
from ..common.history_helpers import (
    fetch_recent_context_lines,
    find_recent_bot_and_user_pair,
    safe_message_content,
)
from ..common.memory_logic import (
    extract_and_store_memory,
    get_user_habit_profile,
    get_user_habit_profile_lines,
    get_relevant_memories,
    maybe_store_training_candidate,
    maybe_update_channel_summary,
    update_user_habit_profile,
)
from ..common.user_interest_logic import (
    extract_user_interests_from_context,
    update_user_interests,
    format_user_interests_for_prompt,
)
from ..common import habit_policy
from ..common.memory_store import MemoryStore
from ..common.message_media import (
    build_message_media_context,
    extract_base64_images,
    message_has_supported_media_or_links,
)
from ..common.http_session import close_shared_http_session
from ..common.fact_check import (
    build_factcheck_target_context,
    classify_reply_investigate_mode,
    extract_target_text_from_message,
    extract_urls,
    search_web,
)
from ..common.web_research import detect_reply_research_rules, detect_deepdive_followup, research_dispatch
from ..common.ollama_helpers import (
    OllamaChatResult,
    call_ollama,
    call_ollama_chat_raw,
    call_ollama_json,
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_multi_turn_output,
    looks_like_non_japanese_reply,
    looks_like_parrot_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_truncated_reply,
    looks_like_unusable_assistant_reply,
    sanitize_generated_reply,
    truncate_lines,
    truncate_text,
    _strip_think_blocks,
    _normalize_compare_text,
)
from ..common.weather_helpers import (
    build_weather_followup_reply,
    extract_recent_weather_fact_from_context,
    extract_weather_place,
    extract_weather_place_from_context,
    fetch_weather_summary,
    is_explicit_weather_request,
    looks_like_weather_query,
)
from ..common.reply_helpers import (
    _build_role_flip_fallback_reply,
    _build_unclear_intent_reply,
    _build_weather_comment,
    _format_reply,
    _merge_user_text_with_media_analysis,
    _should_use_unclear_intent_fallback,
    _strip_user_echo_prefix,
    _trim_reply,
    build_current_time_reply,
    looks_like_time_query,
)
from ..common.umigame_helpers import (
    UMIGAME_GM_FALLBACK_JSON_SCHEMA,
    UMIGAME_GM_JSON_SCHEMA,
    UMIGAME_JSON_SCHEMA,
    _build_umigame_clear_append_message,
    _build_umigame_generation_user_prompt,
    _build_umigame_giveup_message,
    _build_umigame_gm_fallback_system_prompt,
    _build_umigame_gm_user_prompt,
    _format_umigame_gm_reply,
    _infer_umigame_judgement_without_llm,
    _looks_like_umigame_clear,
    _looks_like_umigame_guess,
    _looks_like_umigame_question_duplicate,
    _normalize_umigame_judgement,
    _strip_umigame_memory_labels,
    _umigame_start_message,
)
from ..common.singing_helpers import (
    compact_singing_context_lines,
    is_singing_assistant_line,
)
from .ollama_chat_texts import (
    CONTEXT_CHECKER_SYSTEM_PROMPT,
    EMOTION_REASONER_SYSTEM_PROMPT,
    EMOTION_SCORER_SYSTEM_PROMPT,
    FEEDBACK_KEYWORDS,
    FINAL_REPLY_EXTRACTOR_SYSTEM_PROMPT,
    REACTION_CLASSIFIER_SYSTEM_PROMPT,
    REPLY_DELIBERATION_SYSTEM_PROMPT,
    REPLY_RESEARCH_DECIDER_SYSTEM_PROMPT,
    ROLE_FLIP_BAD_PATTERNS,
    THINK_CLASSIFIER_SYSTEM_PROMPT,
    TIME_QUERY_KEYWORDS,
    CHAT_REPLY_OUTPUT_SUFFIX,
    append_chat_reply_output_suffix,
    build_break_prompt_parts,
    build_chat_reply_system_prompt,
    build_fact_check_system_prompt,
    build_final_reply_extractor_prompt,
    build_output_only_retry_prompt,
    build_parrot_retry_prompt,
    build_reply_deliberation_prompt,
    build_reply_investigate_system_prompt,
    build_reply_research_decider_prompt,
    build_reply_research_decider_system_prompt,
    build_research_grounded_reply_prompt,
    build_role_flip_retry_prompt,
    build_similar_retry_prompt,
    build_topic_system_prompt,
    build_weather_comment_prompt,
)
from .emotion import OllamaChatEmotionMixin
from .ollama_chat_types import EmotionState, MediaBundle, MessageRuntime

log = logging.getLogger("ollama_bot.ollama_chat")

ChannelKind = Literal["primary", "other", "none"]


FINAL_REPLY_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
    },
    "required": ["reply"],
    "additionalProperties": False,
}


def extract_action_style_from_intent_info(
    intent_info: dict[str, Any],
    deliberation_info: dict[str, Any],
) -> str:
    return habit_policy.extract_action_style_from_intent(
        intent_info,
        deliberation_info,
        cfg=cfg,
    )


def _sync_reply_helper_dependencies():
    setattr(_reply_helpers_mod, "cfg", cfg)
    setattr(_reply_helpers_mod, "author_id", author_id)
    setattr(_reply_helpers_mod, "content", content)
    setattr(_reply_helpers_mod, "display_name", display_name)
    setattr(_reply_helpers_mod, "resolve_reference_message", resolve_reference_message)
    setattr(_reply_helpers_mod, "truncate_text", truncate_text)
    setattr(_reply_helpers_mod, "call_ollama", call_ollama)
    setattr(_reply_helpers_mod, "sanitize_generated_reply", sanitize_generated_reply)
    setattr(_reply_helpers_mod, "_normalize_compare_text", _normalize_compare_text)
    return _reply_helpers_mod


def _sync_context_helper_dependencies():
    setattr(_context_helpers_mod, "cfg", cfg)
    setattr(_context_helpers_mod, "cfg_int", cfg_int)
    setattr(_context_helpers_mod, "author_id", author_id)
    setattr(_context_helpers_mod, "channel_id", channel_id)
    setattr(_context_helpers_mod, "content", content)
    setattr(_context_helpers_mod, "to_context_line", to_context_line)
    setattr(_context_helpers_mod, "call_ollama", call_ollama)
    setattr(_context_helpers_mod, "fetch_recent_context_lines", fetch_recent_context_lines)
    setattr(_context_helpers_mod, "looks_like_abnormal_assistant_reply", looks_like_abnormal_assistant_reply)
    setattr(_context_helpers_mod, "looks_like_reasoning_leak", looks_like_reasoning_leak)
    setattr(_context_helpers_mod, "sanitize_generated_reply", sanitize_generated_reply)
    setattr(_context_helpers_mod, "truncate_text", truncate_text)
    setattr(_context_helpers_mod, "log", log)
    return _context_helpers_mod


def _sync_umigame_helper_dependencies():
    setattr(_umigame_helpers_mod, "cfg", cfg)
    setattr(_umigame_helpers_mod, "call_ollama_json", call_ollama_json)
    setattr(_umigame_helpers_mod, "sanitize_generated_reply", sanitize_generated_reply)
    setattr(_umigame_helpers_mod, "truncate_text", truncate_text)
    return _umigame_helpers_mod


def _umigame_memory_persona() -> str:
    persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "").strip()
    return persona or "default"


def _build_umigame_persona_block() -> str:
    system_prompt = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
    style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
    extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

    blocks: list[str] = []
    if system_prompt:
        blocks.append(f"【キャラクター設定】\n{system_prompt}")
    if style_guard:
        blocks.append(f"【口調ガード】\n{style_guard}")
    if extra_rule:
        blocks.append(f"【追加ルール】\n{extra_rule}")
    return "\n\n".join(blocks).strip()


def _build_umigame_generation_system_prompt() -> str:
    module = _sync_umigame_helper_dependencies()
    module._build_umigame_persona_block = _build_umigame_persona_block
    return module._build_umigame_generation_system_prompt()


def _build_umigame_gm_system_prompt(question: str, answer: str) -> str:
    module = _sync_umigame_helper_dependencies()
    module._build_umigame_persona_block = _build_umigame_persona_block
    return module._build_umigame_gm_system_prompt(question, answer)


def _build_umigame_gm_generation_options(*, fallback: bool = False) -> dict[str, float | int]:
    module = _sync_umigame_helper_dependencies()
    return module._build_umigame_gm_generation_options(fallback=fallback)


def _is_umigame_active_channel(channel_id_value: int | None, umigame_states: dict[int, Any]) -> bool:
    module = _sync_umigame_helper_dependencies()
    return module._is_umigame_active_channel(channel_id_value, umigame_states)


def _build_umigame_tool_block_text(action_name: str) -> str:
    module = _sync_umigame_helper_dependencies()
    return module._build_umigame_tool_block_text(action_name)


def _looks_like_umigame_meta_comment(text: str) -> bool:
    module = _sync_umigame_helper_dependencies()
    return module._looks_like_umigame_meta_comment(text)


def _looks_like_umigame_open_question(text: str) -> bool:
    module = _sync_umigame_helper_dependencies()
    return module._looks_like_umigame_open_question(text)


def _looks_like_umigame_quiz_or_wordplay(text: str) -> bool:
    module = _sync_umigame_helper_dependencies()
    return module._looks_like_umigame_quiz_or_wordplay(text)


async def _build_reply_context_user_text(
    message: discord.Message,
    user_text: str,
    *,
    bot_user_id: Optional[int] = None,
) -> str:
    module = _sync_reply_helper_dependencies()
    return await module._build_reply_context_user_text(message, user_text, bot_user_id=bot_user_id)


async def _build_summary_context_lines(
    message: discord.Message,
    sent: discord.Message,
    *,
    base_context_lines: list[str],
    bot_user_id: Optional[int],
    fetch_limit: int | None = None,
    trigger: int | None = None,
) -> list[str]:
    module = _sync_context_helper_dependencies()
    return await module._build_summary_context_lines(
        message,
        sent,
        base_context_lines=base_context_lines,
        bot_user_id=bot_user_id,
        fetch_limit=fetch_limit,
        trigger=trigger,
    )


async def _find_last_assistant_message_text(
    message: discord.Message,
    *,
    bot_user_id: Optional[int],
    cache: dict[int, deque[dict[str, Any]]],
) -> str:
    module = _sync_context_helper_dependencies()
    return await module._find_last_assistant_message_text(message, bot_user_id=bot_user_id, cache=cache)


def _extract_context_flow_label(result_text: str) -> str | None:
    module = _sync_context_helper_dependencies()
    return module._extract_context_flow_label(result_text)


async def _check_context_flow(
    message: discord.Message,
    context_lines: list[str],
    latest_user_text: str | None = None,
    last_assistant_text: str = "",
) -> str:
    module = _sync_context_helper_dependencies()
    return await module._check_context_flow(
        message,
        context_lines,
        latest_user_text=latest_user_text,
        last_assistant_text=last_assistant_text,
    )

def looks_like_feedback_text(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and any(k in t for k in FEEDBACK_KEYWORDS)

def _infer_feedback_type(text: str) -> str:
    t = (text or "").strip()
    if "不自然" in t or "日本語" in t or "もっと自然" in t:
        return "unnatural_japanese"
    if "やめて" in t or "その言い方" in t or "その表現" in t:
        return "style_problem"
    if "長い" in t or "くどい" in t:
        return "too_long"
    if "繰り返" in t or "オウム返し" in t:
        return "parrot"
    return "general_feedback"

def _is_target_channel_id(cid: Optional[int]) -> ChannelKind:
    if cid == cfg_primary_channel_id(0):
        return "primary"
    if cid in set(cfg("OTHER_CHANNEL_IDS", []) or []):
        return "other"
    return "none"

def _get_channel_kind(message: discord.Message) -> ChannelKind:
    return _is_target_channel_id(channel_id(message))

def _get_other_channel_target_threshold() -> int:
    lo = cfg_int("OTHER_CHANNEL_RANDOM_MIN", 5)
    hi = cfg_int("OTHER_CHANNEL_RANDOM_MAX", 10)
    lo = max(lo, 1)
    hi = max(hi, lo)
    return random.randint(lo, hi)

def _reset_other_channel_unreplied_count(cog: Optional[Any], cid: Optional[int]) -> None:
    if cog is None or cid is None:
        return
    unreplied_counts = getattr(cog, "_other_channel_unreplied_counts", None)
    target_counts = getattr(cog, "_other_channel_target_counts", None)
    if isinstance(unreplied_counts, dict):
        unreplied_counts[cid] = 0
    if isinstance(target_counts, dict):
        target_counts[cid] = _get_other_channel_target_threshold()

def _check_and_advance_other_channel_response(
    cog: Optional[Any],
    message: discord.Message,
) -> bool:
    if cfg_bool("OTHER_CHANNEL_ALWAYS_RESPOND", False):
        return True

    cid = channel_id(message)
    if cid is None or cog is None:
        return _should_respond_in_other_channel()

    unreplied_counts = getattr(cog, "_other_channel_unreplied_counts", None)
    target_counts = getattr(cog, "_other_channel_target_counts", None)
    if not isinstance(unreplied_counts, dict) or not isinstance(target_counts, dict):
        return _should_respond_in_other_channel()

    target = target_counts.get(cid, 0)
    if target <= 0:
        target = _get_other_channel_target_threshold()
        target_counts[cid] = target

    current = unreplied_counts.get(cid, 0) + 1
    unreplied_counts[cid] = current

    log.info(
        "other channel response count: channel=%s current=%d target=%d",
        cid,
        current,
        target,
    )

    if current >= target:
        unreplied_counts[cid] = 0
        target_counts[cid] = _get_other_channel_target_threshold()
        return True

    return False

def _random_one_in_range(min_name: str, max_name: str, dmin: int, dmax: int) -> bool:
    lo = cfg_int(min_name, dmin)
    hi = cfg_int(max_name, dmax)
    lo = max(lo, 1)
    hi = max(hi, lo)
    n = random.randint(lo, hi)
    return random.randint(1, n) == 1

def _should_respond_in_other_channel() -> bool:
    if cfg_bool("OTHER_CHANNEL_ALWAYS_RESPOND", False):
        return True
    return _random_one_in_range("OTHER_CHANNEL_RANDOM_MIN", "OTHER_CHANNEL_RANDOM_MAX", 50, 100)

def _cached_other_channel_response_allowed(
    message: discord.Message,
    cog: Optional[Any] = None,
) -> bool:
    cached = getattr(message, "_ollama_other_channel_response_allowed", None)
    if isinstance(cached, bool):
        return cached

    if cog is not None:
        allowed = _check_and_advance_other_channel_response(cog, message)
    else:
        allowed = _should_respond_in_other_channel()

    try:
        setattr(message, "_ollama_other_channel_response_allowed", allowed)
    except Exception:
        pass
    return allowed

def _should_reaction_mode() -> bool:
    return _random_one_in_range("REACTION_RANDOM_MIN", "REACTION_RANDOM_MAX", 5, 10)

def _build_topic_prompt() -> str:
    base = cfg("OLLAMA_TOPIC_USER_PROMPT", "雑談チャンネルに投げる、自然で短い話題を1つ作ってください。")
    guard = (
        "\n\n※最重要ルール:\n"
        "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを"
        "『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。\n"
        "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。"
    )
    return f"{base}{guard}"


SpontaneousSource = Literal["memory", "x", "img", "user_interest"]

def choose_spontaneous_source() -> SpontaneousSource:
    x_ratio = max(0.0, min(cfg_float("SPONTANEOUS_X_RATIO", 0.35), 1.0))
    user_interest_ratio = max(0.0, min(cfg_float("SPONTANEOUS_USER_INTEREST_RATIO", 0.25), 1.0))
    img_ratio = max(0.0, min(cfg_float("SPONTANEOUS_IMG_RATIO", 0.15), 1.0))
    val = random.random()
    if val < img_ratio:
        return "img"
    elif val < (img_ratio + x_ratio):
        return "x"
    elif val < (img_ratio + x_ratio + user_interest_ratio):
        return "user_interest"
    else:
        return "memory"


async def fetch_recent_x_timeline_items(limit: int = 10) -> list[dict[str, str]]:
    resolved_limit = max(int(limit or 10), 1)
    result = await research_dispatch(
        f"今のXのタイムラインを{resolved_limit}件見て",
        mode="x_timeline",
        max_results=resolved_limit,
    )
    error = str(result.get("error") or "").strip()
    if error:
        raise RuntimeError(error)

    items: list[dict[str, str]] = []
    for source in list(result.get("sources") or []):
        if not isinstance(source, dict):
            continue
        text = str(source.get("snippet") or source.get("content_excerpt") or source.get("content_text") or "").strip()
        if not text:
            continue
        items.append({
            "author": str(source.get("author") or "").strip(),
            "handle": str(source.get("handle") or "").strip(),
            "text": text,
            "url": str(source.get("url") or "").strip(),
        })
        if len(items) >= resolved_limit:
            break
    if not items:
        raise RuntimeError(str(result.get("summary") or "Xタイムラインの投稿を抽出できませんでした。").strip())
    return items


def format_x_timeline_items_for_prompt(items: list[dict[str, str]], *, limit: int = 10) -> str:
    lines: list[str] = []
    seen_texts: set[str] = set()
    for item in list(items or [])[:max(int(limit or 10), 1)]:
        text = truncate_text(str(item.get("text") or "").strip().replace("\n", " "), 280)
        if not text or text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        lines.append(f"{len(lines) + 1}. 本文: {text}")
    return "\n".join(lines)


def rotate_x_timeline_items_for_spontaneous(
    owner: Any,
    items: list[dict[str, str]],
    *,
    limit: int = 10,
) -> list[dict[str, str]]:
    normalized_items = [dict(item) for item in list(items or []) if str(item.get("text") or "").strip()]
    max_items = max(int(limit or 10), 1)
    if not normalized_items:
        return []

    cache_size = max(cfg_int("SPONTANEOUS_X_RECENT_TEXT_CACHE_SIZE", 60), max_items)
    recent = getattr(owner, "_recent_spontaneous_x_texts", None)
    if not isinstance(recent, deque) or recent.maxlen != cache_size:
        recent = deque(list(recent or [])[-cache_size:], maxlen=cache_size)
        with contextlib.suppress(Exception):
            setattr(owner, "_recent_spontaneous_x_texts", recent)

    def _item_key(item: dict[str, str]) -> str:
        return _normalize_compare_text(str(item.get("text") or ""))

    recent_keys = {str(key) for key in recent if str(key)}
    unseen = [item for item in normalized_items if _item_key(item) and _item_key(item) not in recent_keys]

    if unseen:
        ordered = unseen + [item for item in normalized_items if item not in unseen]
    else:
        cursor = int(getattr(owner, "_spontaneous_x_timeline_cursor", 0) or 0)
        offset = cursor % len(normalized_items)
        ordered = normalized_items[offset:] + normalized_items[:offset]

    selected = ordered[:max_items]
    for item in selected:
        key = _item_key(item)
        if key:
            recent.append(key)
    with contextlib.suppress(Exception):
        setattr(owner, "_spontaneous_x_timeline_cursor", int(getattr(owner, "_spontaneous_x_timeline_cursor", 0) or 0) + 1)
    return selected


def _relationship_score_for_habit(relationship: Any | None) -> float | None:
    if relationship is None:
        return None
    try:
        affinity = float(getattr(relationship, "affinity", 0.0) or 0.0)
        trust = float(getattr(relationship, "trust", 0.0) or 0.0)
        limit = max(float(cfg("RELATIONSHIP_ABS_MAX", 5.0) or 5.0), 0.5)
        normalized = ((affinity + trust) / 2.0) / limit
        return max(0.0, min(1.0, (normalized + 1.0) / 2.0))
    except Exception:
        return None

def _looks_like_negative_habit_followup(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if looks_like_feedback_text(t):
        return True
    negative_markers = (
        "違う", "ちがう", "違います", "いや", "いや違う", "そうじゃない",
        "意味わから", "わからん", "何言って", "は？", "は?",
        "なんでそうなる", "怒ってる", "こわ",
    )
    return any(marker in t for marker in negative_markers)

def _looks_like_reactionish_followup(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    return bool(
        re.search(r"[!！😂🤣😊😭🥲👍✨wｗ笑草]+", t)
        or len(t) <= 8
        or any(marker in t for marker in ("なるほど", "たしかに", "ありがとう", "助かる", "いいね"))
    )

def _user_valence_improved_for_habit(
    previous_scores: dict[str, float] | None,
    current_scores: dict[str, float] | None,
) -> bool | None:
    prev = previous_scores or {}
    curr = current_scores or {}
    if not prev or not curr:
        return None

    prev_negative = max(
        float(prev.get("anger", 0.0) or 0.0),
        float(prev.get("sadness", 0.0) or 0.0),
        float(prev.get("fear", 0.0) or 0.0),
        float(prev.get("disgust", 0.0) or 0.0),
    )
    curr_negative = max(
        float(curr.get("anger", 0.0) or 0.0),
        float(curr.get("sadness", 0.0) or 0.0),
        float(curr.get("fear", 0.0) or 0.0),
        float(curr.get("disgust", 0.0) or 0.0),
    )
    prev_positive = max(
        float(prev.get("joy", 0.0) or 0.0),
        float(prev.get("anticipation", 0.0) or 0.0),
    )
    curr_positive = max(
        float(curr.get("joy", 0.0) or 0.0),
        float(curr.get("anticipation", 0.0) or 0.0),
    )
    return (curr_negative <= prev_negative) or (curr_positive >= prev_positive)

from ..common.reply_safety import (
    looks_like_similar_to_previous_reply as _looks_too_similar_to_previous_reply,
)

async def _is_force_response_trigger(message: discord.Message, bot_user_id: Optional[int]) -> bool:
    if bot_user_id is None:
        return False
    try:
        if any(getattr(u, "id", None) == bot_user_id for u in (message.mentions or [])):
            return True
    except Exception:
        pass
    ref_msg = await resolve_reference_message(message)
    return bool(ref_msg and author_id(ref_msg) == bot_user_id)

def _should_ignore(
    message: discord.Message,
    bot_user_id: Optional[int],
    *,
    force_response: bool = False,
    cog: Optional[Any] = None,
) -> tuple[bool, ChannelKind]:
    if message.guild is None:
        return True, "none"

    a_id = author_id(message)
    ignored_user_ids = cfg_int_set("IGNORE_USER_IDS")
    if a_id in ignored_user_ids:
        return True, "none"

    kind = _get_channel_kind(message)
    if kind == "none":
        return True, kind

    # 匿名メッセージ（/tokumei 等）は AI の反応対象外とする
    if has_anon_buttons(message):
        return True, kind

    # Bot自身、他のBot、Webhookの発言はすべて無視する
    if bot_user_id is not None and a_id == bot_user_id:
        return True, kind

    if getattr(message.author, "bot", False):
        return True, kind

    if getattr(message, "webhook_id", None) is not None:
        return True, kind

    if not content(message) and not message_has_supported_media_or_links(message):
        return True, kind

    if kind == "other" and not force_response and not _cached_other_channel_response_allowed(message, cog=cog):
        return True, kind

    return False, kind

def _is_rate_limited(hit_cache: dict[int, deque[float]], message: discord.Message) -> bool:
    cid = channel_id(message)
    if cid is None:
        return False

    window_sec = float(cfg("RATE_LIMIT_WINDOW_SEC", 10) or 10)
    max_messages = int(cfg("RATE_LIMIT_MAX_MESSAGES", 3) or 3)
    if window_sec <= 0 or max_messages <= 0:
        return False

    now = time.monotonic()
    q = hit_cache[cid]
    while q and (now - q[0]) > window_sec:
        q.popleft()
    if len(q) >= max_messages:
        q.append(now)
        return True
    q.append(now)
    return False

def _looks_like_role_flip_reply(intent_info: dict[str, Any], reply: str) -> bool:
    reply_text = (reply or "").strip()
    return bool(intent_info.get("target_is_ai")) and bool(reply_text) and any(p in reply_text for p in ROLE_FLIP_BAD_PATTERNS)

def _classifier_model_name() -> str:
    return cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))

def _managed_channel_ids() -> set[int]:
    return cfg_managed_channel_ids()
