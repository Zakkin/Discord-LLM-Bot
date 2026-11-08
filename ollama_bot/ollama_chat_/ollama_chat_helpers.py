"""OllamaChat系Mixinで共有するDiscord、LLM、記憶、プロンプト補助処理を集約する。"""
import asyncio
import contextlib
import difflib
import json
import logging
import os
import random
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from lib.date_utils import JST


import discord
from discord import app_commands
from discord.ext import commands

from harness.agent.prompt_parser.prompt_manager import (
    _analyze_pre_reply_context,
    _build_user_prompt,
)
from harness.agent.prompt_parser.prompt_manager import context_helpers as _context_helpers_mod
from harness.agent.prompt_parser.parser import reply_helpers as _reply_helpers_mod
from harness.agent.tool_registry import umigame_helpers as _umigame_helpers_mod
from harness.agent.observation.feedback_observer import FeedbackObserver

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
    clamp_score,
    emotion_bio_enabled,
    emotion_scoring_enabled,
    fallback_reason_text,
    looks_like_bad_reason_text,
    merge_emotion_scores,
    normalize_reason_text,
    should_force_deliberation_from_emotions,
)
from harness.agent.prompt_parser.prompt_manager.history_helpers import (
    fetch_recent_context_lines,
    find_recent_bot_and_user_pair,
    safe_message_content,
)
from harness.agent.memory_manager.logic import (
    extract_and_store_memory,
    get_user_habit_profile,
    get_user_habit_profile_lines,
    get_relevant_memories,
    maybe_store_training_candidate,
    maybe_update_channel_summary,
    update_user_habit_profile,
)
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import (
    extract_user_interests_from_context,
    update_user_interests,
    format_user_interests_for_prompt,
)
from harness.agent.prompt_parser.parser import habit_policy
from harness.agent.memory_manager import MemoryStore
from ..common.message_media import (
    build_message_media_context,
    extract_base64_images,
    message_has_image,
    message_has_supported_media_or_links,
)
from ..common.http_session import close_shared_http_session
from lib.text_utils import extract_urls
from harness.agent.tool_registry.mcp.fact_check import (
    build_factcheck_target_context,
    classify_reply_investigate_mode,
    extract_target_text_from_message,
    search_web,
)
from harness.agent.tool_registry.web_research import detect_reply_research_rules, detect_deepdive_followup, research_dispatch
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
from harness.agent.tool_registry.weather_helpers import (
    build_weather_followup_reply,
    extract_recent_weather_fact_from_context,
    extract_weather_place,
    extract_weather_place_from_context,
    fetch_weather_summary,
    is_explicit_weather_request,
    looks_like_weather_query,
)
from harness.agent.prompt_parser.parser.reply_helpers import (
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
from harness.agent.tool_registry.umigame_helpers import (
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
from harness.agent.tool_registry.singing_helpers import (
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
    mod = sys.modules.get(__name__)
    setattr(_reply_helpers_mod, "cfg", getattr(mod, "cfg", cfg))
    setattr(_reply_helpers_mod, "author_id", getattr(mod, "author_id", author_id))
    setattr(_reply_helpers_mod, "content", getattr(mod, "content", content))
    setattr(_reply_helpers_mod, "display_name", getattr(mod, "display_name", display_name))
    setattr(_reply_helpers_mod, "resolve_reference_message", getattr(mod, "resolve_reference_message", resolve_reference_message))
    setattr(_reply_helpers_mod, "truncate_text", getattr(mod, "truncate_text", truncate_text))
    setattr(_reply_helpers_mod, "call_ollama", getattr(mod, "call_ollama", call_ollama))
    setattr(_reply_helpers_mod, "sanitize_generated_reply", getattr(mod, "sanitize_generated_reply", sanitize_generated_reply))
    setattr(_reply_helpers_mod, "_normalize_compare_text", getattr(mod, "_normalize_compare_text", _normalize_compare_text))
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
    from lib.config_utils import cfg_persona_namespace
    return cfg_persona_namespace(custom_cfg=cfg)


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


_JAPANESE_STOPWORDS = {
    "これ", "それ", "あれ", "どれ", "ここ", "そこ", "あそこ", "どこ",
    "こう", "そう", "ああ", "どう", "こと", "もの", "とき", "わけ",
    "はず", "つもり", "ところ", "ほう", "など", "ばかり", "だけ", "ほど",
    "くらい", "ぐらい", "から", "まで", "より", "って", "という", "といった",
    "ような", "ように", "よう", "みたい", "らしい", "です", "ます", "でした",
    "ました", "ません", "だろ", "かも", "ある", "いる", "する", "なる",
    "おる", "みる", "いく", "くる", "せる", "させる", "れる", "られる",
    "ない", "なかった", "たい", "たく", "たかった", "じゃん", "んだ",
    "んだよ", "んだな", "んだね", "んだよね", "んだよな", "ほんと", "まじ",
    "マジ", "いや", "うん", "はい", "いいえ", "ちょっと", "すごく", "とても",
    "かなり", "もっと", "ずっと", "いつも", "また", "まだ", "もう", "すでに",
    "たぶん", "おそらく", "ぜんぶ", "みんな", "さん", "ちゃん", "くん",
}

_GODAN_CONSONANT_MAP = {
    "ら": "r", "り": "r", "る": "r", "れ": "r", "ろ": "r", "っ": "r",
    "か": "k", "き": "k", "く": "k", "け": "k", "こ": "k", "い": "k",
    "さ": "s", "し": "s", "す": "s", "せ": "s", "そ": "s",
    "た": "t", "ち": "t", "つ": "t", "て": "t", "と": "t",
    "な": "n", "に": "n", "ぬ": "n", "ね": "n", "の": "n", "ん": "n",
    "ば": "b", "び": "b", "ぶ": "b", "べ": "b", "ぼ": "b",
    "ま": "m", "み": "m", "む": "m", "め": "m", "も": "m",
    "が": "g", "ぎ": "g", "ぐ": "g", "げ": "g", "ご": "g",
}

_FOLLOW_ENDING_PATTERNS = (
    "んだよな", "んだよなー", "んだよな〜",
    "じゃん", "じゃんか", "じゃんね", "じゃんよ",
    "だし", "だしな", "だしね", "だしさ",
    "よな", "よなー", "よな〜",
    "んだな", "んだね", "んだよね", "んだよ", "んだわ",
    "よね", "よねー", "よね〜",
    "もんな", "もんね", "もん",
    "けどな", "けどね",
    "って話", "ってこと",
)


def _extract_content_tokens_and_stems(text: str) -> set[str]:
    tokens: set[str] = set()
    if not text:
        return tokens

    # カタカナ語（2文字以上）
    for m in re.findall(r"[ァ-ヶー]{2,}", text):
        if m not in _JAPANESE_STOPWORDS:
            tokens.add(m)

    # 漢字熟語（2文字以上）
    for m in re.findall(r"[一-龥]{2,}", text):
        if m not in _JAPANESE_STOPWORDS:
            tokens.add(m)

    # 英数字単語（2文字以上）
    for m in re.findall(r"[A-Za-z0-9_]{2,}", text):
        tokens.add(m.lower())

    # ひらがな特徴語（3文字以上・ストップワード除外）
    for m in re.findall(r"[ぁ-ん]{3,}", text):
        if m not in _JAPANESE_STOPWORDS:
            tokens.add(m)

    # 漢字＋ひらがな活用語・語幹（例: 太る/太った/太ってる/太らない -> 太:r）
    for m in re.finditer(r"([一-龥]+)([ぁ-ん]{1,4})", text):
        kanji_part = m.group(1)
        hira_part = m.group(2)
        full_word = kanji_part + hira_part
        if len(full_word) >= 2:
            tokens.add(full_word[:2])
            tokens.add(full_word[:3])
            tokens.add(full_word)
        first_hira = hira_part[0]
        if first_hira in _GODAN_CONSONANT_MAP:
            tokens.add(f"{kanji_part}:{_GODAN_CONSONANT_MAP[first_hira]}")

    return {t for t in tokens if len(t) >= 2}


async def _check_context_flow(
    message: discord.Message,
    context_lines: list[str],
    latest_user_text: str | None = None,
    last_assistant_text: str = "",
) -> str:
    latest_text = (latest_user_text or content(message) or "").strip()
    recent_lines = context_lines[-5:] if context_lines else []

    # 1. 2文字以上の自立語・特徴語（共通語幹や同一固有名詞）の共通性チェック
    if latest_text and recent_lines:
        latest_tokens = _extract_content_tokens_and_stems(latest_text)
        if latest_tokens:
            history_text = " ".join(recent_lines)
            history_tokens = _extract_content_tokens_and_stems(history_text)
            common = latest_tokens & history_tokens
            if common:
                reason = f"common_stem_or_keyword({','.join(sorted(common)[:3])})"
                log.info("Context flow decision: result=%s raw_llm=%r reason=%s", "FOLLOW", None, reason)
                return "FOLLOW"

    # 2. 直前履歴に同一発言者の発話があり最新発言が「〜んだよな」「〜じゃん」「〜だし」「〜よな」等の終助詞・補足表現で終わる場合
    if latest_text and recent_lines:
        author = getattr(message, "author", None)
        author_names = {
            name.strip()
            for name in [
                str(getattr(author, "display_name", "") or ""),
                str(getattr(author, "name", "") or ""),
            ]
            if name.strip()
        }
        has_same_author = False
        if author_names:
            for line in recent_lines:
                speaker_part = line.split(":", 1)[0].split(" (>>", 1)[0].strip()
                if speaker_part in author_names:
                    has_same_author = True
                    break
        if has_same_author:
            clean_end = re.sub(r"[\s!?.…~〜wW笑\u3000。！？]*$", "", latest_text)
            for ending in _FOLLOW_ENDING_PATTERNS:
                if clean_end.endswith(ending):
                    reason = f"same_author_ending_particle({ending})"
                    log.info("Context flow decision: result=%s raw_llm=%r reason=%s", "FOLLOW", None, reason)
                    return "FOLLOW"

    module = _sync_context_helper_dependencies()
    result = await module._check_context_flow(
        message,
        context_lines,
        latest_user_text=latest_user_text,
        last_assistant_text=last_assistant_text,
    )
    log.info("Context flow decision: result=%s raw_llm=%r reason=%s", result, result, "llm_evaluation")
    return result

_feedback_observer = FeedbackObserver()

def looks_like_feedback_text(text: str) -> bool:
    return _feedback_observer.looks_like_feedback_text(text)

def _infer_feedback_type(text: str) -> str:
    return _feedback_observer.infer_feedback_type(text)


def _is_target_channel_id(cid: Optional[int]) -> ChannelKind:
    if cid == cfg_primary_channel_id(0):
        return "primary"
    if cid is not None and cid in cfg_int_set("OTHER_CHANNEL_IDS"):
        return "other"
    return "none"

def _get_channel_kind(message: discord.Message) -> ChannelKind:
    return _is_target_channel_id(channel_id(message))

def _get_other_channel_message_counts(cog: Optional[Any]) -> dict[int, int]:
    if cog is None:
        return {}
    counts = getattr(cog, "_other_channel_message_counts", None)
    if counts is None or not isinstance(counts, dict):
        counts = {}
        try:
            setattr(cog, "_other_channel_message_counts", counts)
        except Exception:
            pass
    return counts

def _get_other_channel_reply_upper_limit() -> int:
    """サブチャンネル返信カウントの上限値（最大インターバル）を解決して返す。"""
    max_interval = cfg_int("OTHER_CHANNEL_MAX_INTERVAL_MESSAGES", -1)
    if max_interval > 0:
        return max_interval
    return max(
        cfg_int("OTHER_CHANNEL_RANDOM_MAX", 10),
        cfg_int("OTHER_CHANNEL_COOLDOWN_MESSAGES", 0),
        1,
    )


def _get_other_channel_force_next_reply_cids(cog: Optional[Any]) -> set[int]:
    """次回レスで確定返信を行うチャンネルIDのセットを取得・初期化する。"""
    if cog is None:
        return set()
    force_cids = getattr(cog, "_other_channel_force_next_reply_cids", None)
    if force_cids is None or not isinstance(force_cids, set):
        force_cids = set()
        try:
            setattr(cog, "_other_channel_force_next_reply_cids", force_cids)
        except Exception:
            pass
    return force_cids


def _set_other_channel_message_count_to_max(cog: Optional[Any], cid: int) -> int:
    """指定チャンネルの未返信メッセージカウントを上限値に設定し、次回確定返信フラグを立てる。"""
    if cog is None or cid <= 0:
        return 0
    upper_limit = _get_other_channel_reply_upper_limit()
    counts = _get_other_channel_message_counts(cog)
    counts[cid] = upper_limit
    _get_other_channel_force_next_reply_cids(cog).add(cid)
    return upper_limit


def _record_other_channel_message(cog: Optional[Any], cid: Optional[int]) -> int:
    """サブチャンネルでのユーザー発言を記録し、現在の未返信メッセージ数を返す。"""
    if cog is None or cid is None:
        return 0
    counts = _get_other_channel_message_counts(cog)
    counts[cid] = counts.get(cid, 0) + 1
    return counts[cid]

def _reset_other_channel_message_count(cog: Optional[Any], cid: Optional[int]) -> None:
    """サブチャンネルでBotが返信した際、未返信メッセージ数を0にリセットしてクールダウンを開始する。"""
    if cog is None or cid is None:
        return
    counts = _get_other_channel_message_counts(cog)
    counts[cid] = 0
    _get_other_channel_force_next_reply_cids(cog).discard(cid)

def _random_one_in_range(min_name: str, max_name: str, dmin: int, dmax: int) -> bool:
    lo = cfg_int(min_name, dmin)
    hi = cfg_int(max_name, dmax)
    lo = max(lo, 1)
    hi = max(hi, lo)
    n = random.randint(lo, hi)
    hit = (random.randint(1, n) == 1)
    log.info(
        "TRACE random_dice_roll: min_name=%s (%d) max_name=%s (%d) chosen_n=%d hit=%s",
        min_name, lo, max_name, hi, n, hit
    )
    return hit

def _should_respond_in_other_channel(
    cid: Optional[int] = None,
    cog: Optional[Any] = None,
) -> bool:
    if cfg_bool("OTHER_CHANNEL_ALWAYS_RESPOND", False):
        return True

    if cid is not None and cid > 0 and cog is not None:
        if cid in _get_other_channel_force_next_reply_cids(cog):
            log.info(
                "TRACE other_channel_decision: cid=%s force_next_reply=True (forced response)",
                cid,
            )
            return True
        counts = _get_other_channel_message_counts(cog)
        current = counts.get(cid, 0)
        max_interval = cfg_int("OTHER_CHANNEL_MAX_INTERVAL_MESSAGES", -1)
        if max_interval > 0 and current >= max_interval:
            log.info(
                "TRACE other_channel_decision: cid=%s reached max_interval count=%d >= max=%d (forced response)",
                cid, current, max_interval,
            )
            return True

    # クールダウンが無効化されている場合、または cog/cid が渡されていない場合は従来の独立確率サイコロ
    if not cfg_bool("OTHER_CHANNEL_COOLDOWN_ENABLED", True) or cog is None or cid is None:
        return _random_one_in_range("OTHER_CHANNEL_RANDOM_MIN", "OTHER_CHANNEL_RANDOM_MAX", 5, 10)

    # クールダウンおよび最大インターバルの設定値取得（明示的に指定された場合のみ有効）
    cooldown = cfg_int("OTHER_CHANNEL_COOLDOWN_MESSAGES", 0)
    cooldown = max(0, cooldown)

    max_interval = cfg_int("OTHER_CHANNEL_MAX_INTERVAL_MESSAGES", -1)

    counts = _get_other_channel_message_counts(cog)
    current = counts.get(cid, 0)

    # 1. 明示的なクールダウン中（最低メッセージ数に満たない場合）: 返信を抑止
    if cooldown > 0 and current < cooldown:
        log.info(
            "TRACE other_channel_decision: cid=%s cooldown active count=%d < cooldown=%d (suppressed)",
            cid, current, cooldown
        )
        return False

    # 2. 明示的な最大間隔到達: 確定で返信
    if max_interval > 0 and current >= max_interval:
        log.info(
            "TRACE other_channel_decision: cid=%s max interval reached count=%d >= max=%d (forced response)",
            cid, current, max_interval
        )
        return True

    # 3. 通常時（クールダウン期間外かつ最大確定未満）:
    # 項目73に準拠し、1通ごとに独立確率抽選（サイコロ方式）を行う
    res = _random_one_in_range("OTHER_CHANNEL_RANDOM_MIN", "OTHER_CHANNEL_RANDOM_MAX", 5, 10)
    log.info(
        "TRACE other_channel_decision: cid=%s dice roll count=%d cooldown=%d max=%d result=%s",
        cid, current, cooldown, max_interval, res
    )
    return res


_LOW_INFO_STRIP_RE = re.compile(r"[\s\(\)（）\^〜~○＼／\\/!！?？。、・°ഃ˃˂;；:：`´︵\u2060\-_=+*#@'\"]+")


def _is_low_information_other_reply(message: Any) -> bool:
    """他ユーザー宛ての返信で、本文が数字・記号・顔文字のみ（意味の取れる語がない）なら True。"""
    ref = getattr(message, "reference", None)
    if ref is None or getattr(ref, "message_id", None) is None:
        return False
    resolved = getattr(ref, "resolved", None)
    ref_author = getattr(resolved, "author", None)
    if ref_author is not None and bool(getattr(ref_author, "bot", False)):
        return False  # Bot宛ての返信は対象外
    text = str(getattr(message, "content", "") or "")
    if getattr(message, "attachments", None):
        return False
    core = _LOW_INFO_STRIP_RE.sub("", text)
    return bool(core) and core.isdigit() or core == ""


def _cached_other_channel_response_allowed(
    message: discord.Message,
    cog: Optional[Any] = None,
) -> bool:
    cached = getattr(message, "_ollama_other_channel_response_allowed", None)
    if isinstance(cached, bool):
        return cached

    mid = getattr(message, "id", None)
    if cog is not None and isinstance(mid, int):
        allowed_cache = getattr(cog, "_other_channel_allowed_cache", None)
        if isinstance(allowed_cache, dict) and mid in allowed_cache:
            return allowed_cache[mid]

    cid = channel_id(message)
    if cfg_bool("OTHER_CHANNEL_SKIP_LOW_INFO_REPLY", True) and _is_low_information_other_reply(message):
        log.info(
            "TRACE other_channel_decision: cid=%s low-information reply to other user (skipped, dice not consumed)",
            cid,
        )
        allowed = False
    else:
        allowed = _should_respond_in_other_channel(cid=cid, cog=cog)
    try:
        setattr(message, "_ollama_other_channel_response_allowed", allowed)
    except Exception:
        pass

    if cog is not None and isinstance(mid, int):
        allowed_cache = getattr(cog, "_other_channel_allowed_cache", None)
        if not isinstance(allowed_cache, dict):
            allowed_cache = {}
            try:
                setattr(cog, "_other_channel_allowed_cache", allowed_cache)
            except Exception:
                pass
        if isinstance(allowed_cache, dict):
            allowed_cache[mid] = allowed
            while len(allowed_cache) > 512:
                try:
                    allowed_cache.pop(next(iter(allowed_cache)), None)
                except StopIteration:
                    break

    return allowed


from harness.agent.orchestrator.proactive import (
    SpontaneousSource,
    _build_topic_prompt,
    choose_spontaneous_source,
    fetch_recent_x_timeline_items as _fetch_recent_x_timeline_items_impl,
    format_x_timeline_items_for_prompt,
    rotate_x_timeline_items_for_spontaneous,
)


async def fetch_recent_x_timeline_items(limit: int = 10) -> list[dict[str, str]]:
    return await _fetch_recent_x_timeline_items_impl(limit=limit, custom_dispatch=research_dispatch)


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

from harness.agent.prompt_parser.parser import (
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

    # Bot自身の発言はすべて無視する（自己ループ防止）
    if bot_user_id is not None and a_id == bot_user_id:
        return True, kind

    # 匿名メッセージ（/tokumei 等のボタン付き）以外のBot・Webhook発言は無視する
    is_anon_like = has_anon_buttons(message)
    if not is_anon_like:
        if getattr(message.author, "bot", False):
            return True, kind

        if getattr(message, "webhook_id", None) is not None:
            return True, kind

    # 他のBot宛てのメッセージ（他Botへのメンション、他Botへの返信、他Bot名での呼びかけ）は無視する
    from ..common.bot_identity import is_message_addressed_to_other_bot
    bot_obj = getattr(cog, "bot", None) if cog is not None else None
    if is_message_addressed_to_other_bot(message, bot_user_id=bot_user_id, bot=bot_obj):
        return True, kind

    # 2段階Visionパイプラインの導入に伴い、画像含有メッセージ（添付または画像URL）を返信対象として許可。
    # テキストが空であっても、画像またはサポート対象メディアが含まれていれば処理を継続する。
    if not content(message) and not message_has_image(message) and not message_has_supported_media_or_links(message):
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


def _reply_model_timeout_fallback() -> str:
    fallback = str(cfg("OLLAMA_MODEL_TIMEOUT_FALLBACK", "") or "").strip()
    if fallback:
        return fallback
    fallback = str(
        cfg(
            "OLLAMA_EMPTY_REPLY_FALLBACK",
            "今ちょっと応答が重い。もう一回言ってくれ。",
        )
        or ""
    ).strip()
    return fallback or "今ちょっと応答が重い。もう一回言ってくれ。"


def should_send_timeout_fallback(
    *,
    is_primary_channel: bool = False,
    is_direct: bool = False,
    force_response: bool = False,
) -> bool:
    """
    推論モデルのタイムアウト時にフォールバック応答を送信すべきかを判定する。
    直接話しかけられた場合（is_direct=True または force_response=True）や、
    メインチャンネル（is_primary_channel=True）では会話断絶を防ぐため必ずTrueを返す。
    サブチャンネルの自発雑談では TIMEOUT_FALLBACK_ALL_CHANNELS が有効でない限りスキップ（False）する。
    """
    if cfg_bool("TIMEOUT_FALLBACK_ALL_CHANNELS", False):
        return True
    return bool(is_primary_channel or is_direct or force_response)

