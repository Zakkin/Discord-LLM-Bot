from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import discord

from lib.text_utils import compact_whitespace, truncate_text

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.discord_helpers import (
    ContextTurn,
    author_id,
    channel_id,
    collect_reply_chain,
    content,
    context_boundary_of,
    display_name,
    find_reply_parent_from_cache,
    format_message_reactions,
    is_context_item_after_boundary,
    prune_reply_chain_from_cache,
    recent_context_items,
)
from ollama_bot.common.bot_identity import BotIdentity, is_bot_name_mentioned, resolve_bot_identity
from .thread_tracker import ConversationThread, ThreadTracker
from harness.agent.memory_manager import MemoryStore
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState
from .system_prompts import (
    USER_REPLY_BASE_PARTS,
    append_chat_reply_output_suffix,
    build_playful_say_request_instruction,
    build_user_prompt_extra_rule_parts,
    build_user_prompt_target_is_ai_parts,
)
from .budget import (
    _build_recent_turns,
    _context_max_age_sec,
    _filter_prompt_context_lines,
    _get_relevant_episode_lines,
    _looks_like_bot_emotion_or_relationship_query,
    _trim_prompt_inputs,
    build_agent_persona_parts,
    compute_dynamic_prompt_token_budget,
    estimate_text_tokens,
    sanitize_memory_candidates,
)
from .analyzer import (
    _analyze_pre_reply_context,
    _looks_like_external_research_context_request,
    _looks_like_puzzle_or_riddle_message,
    _looks_like_recall_question,
    detect_third_party_hearsay,
    extract_playful_say_request,
    looks_like_ai_utterance_confirmation,
    looks_like_user_playful_request_or_command,
)

log = logging.getLogger("harness.agent.prompt_manager.builder")


def _format_reply_tree_context(
    reply_chain: list[discord.Message],
    *,
    bot_user_id: Optional[int] = None,
    cached_parent_turn: Optional[dict[str, Any]] = None,
) -> list[str]:
    lines: list[str] = []
    if cached_parent_turn:
        p_name = str(cached_parent_turn.get("name") or "ユーザー")
        p_content = str(cached_parent_turn.get("content") or "").strip()
        if p_content:
            p_content_clean = compact_whitespace(p_content)
            p_content_clean = truncate_text(p_content_clean, 100)
            lines.append(f"・『{p_name}』: 「{p_content_clean}」")

    for msg in reply_chain:
        aid = author_id(msg)
        is_bot = bot_user_id is not None and aid == bot_user_id
        m_name = "あなた（AI）" if is_bot else f"『{display_name(msg)}』"
        m_content = compact_whitespace(content(msg))
        if not m_content:
            continue
        max_len = 100 if is_bot else 120
        m_content = truncate_text(m_content, max_len)
        lines.append(f"・{m_name}: 「{m_content}」")
    return lines


def _detect_recent_bot_utterance_tsukkomi(
    user_text: str,
    recent_bot_utterances: list[str],
) -> tuple[bool, str, str]:
    """ユーザーの最新発言が、直近のBot自身の発言に対するツッコミ・疑問であるかを判定する。"""
    raw_user = str(user_text or "").strip()
    if not raw_user:
        return False, "", ""
    clean_user = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw_user, flags=re.DOTALL).strip()
    clean_user = re.sub(r"^[（(].*?への返信[）)]\s*", "", clean_user, flags=re.DOTALL).strip()
    # 意見・感想・質問の構文（〜についてどう思う等）は自発言へのツッコミではない
    if any(op in clean_user for op in ("どう思う", "どう考える", "どう？", "どうですか", "教えて", "教えろ")):
        return False, "", ""

    # 呼称・口調・敬称へのツッコミ（呼び捨て、タメ口、さん付けなど）は直前発言本文に単語が含まれていなくても成立する
    manner_and_address_keywords = (
        "呼び捨て", "タメ口", "ため口", "タメぐち", "敬語", "さん付け", "ちゃん付け", "君付け", "くん付け", "呼び方", "急にタメ",
    )
    for kw in manner_and_address_keywords:
        if kw in clean_user:
            for utterance in recent_bot_utterances:
                raw_u = str(utterance or "").strip()
                if raw_u:
                    return True, raw_u, kw

    is_question_or_tsukkomi = any(q in clean_user for q in ("？", "?", "なの", "ですか", "だっけ", "言った", "言ってた", "誰のこと", "何言って", "どういうこと"))
    if not is_question_or_tsukkomi:
        return False, "", ""

    compact_user = re.sub(r"[？?！!。、\s]", "", clean_user)
    if not compact_user:
        return False, "", ""

    time_and_topic_keywords = [
        "朝", "昼", "夜", "深夜", "昨日", "今日", "明日", "今朝", "今夜",
        "起き", "寝", "叫", "誰のこと", "何言って", "何のこと", "誰それ", "何の話",
    ]
    user_keywords = [kw for kw in time_and_topic_keywords if kw in compact_user]
    if len(compact_user) <= 8:
        stem = compact_user.rstrip("なののかですがだよだね")
        if stem and len(stem) >= 2 and stem not in user_keywords:
            user_keywords.append(stem)

    for utterance in recent_bot_utterances:
        raw_u = str(utterance or "").strip()
        if not raw_u:
            continue
        for kw in user_keywords:
            if kw and len(kw) >= 1 and kw in raw_u:
                return True, raw_u, kw

    return False, "", ""


def _format_user_calling_name(name: str) -> str:
    clean = str(name or "").strip()
    if not clean:
        return "あなた"
    if clean.endswith(("さん", "くん", "君", "ちゃん", "氏", "先生", "たん")):
        return clean
    return f"{clean}さん"


def _future_only_context_lines(
    message: discord.Message,
    items: Iterable[Mapping[str, Any]],
) -> set[str]:
    """返信対象メッセージより後（未来）の項目にしか存在しない発言行の集合を返す。

    対象メッセージ以前にも同一文面の行が存在する場合は誤除外を避けるため対象外とする。
    対象メッセージ自身の行は「過去側」として扱う（除外しない）。
    """
    boundary_id, boundary_ts = context_boundary_of(message)
    if boundary_id is None and boundary_ts is None:
        return set()
    future_lines: set[str] = set()
    past_lines: set[str] = set()
    for item in items:
        line = str(item.get("line") or "").strip()
        if not line:
            continue
        if is_context_item_after_boundary(
            item,
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            exclude_boundary_message=False,
        ):
            future_lines.add(line)
        else:
            past_lines.add(line)
    return future_lines - past_lines


def _drop_future_context_lines(lines: list[str], future_only_lines: set[str]) -> list[str]:
    """会話履歴行から未来メッセージの行を取り除く（プロンプト未来混入の最終防御）。"""
    if not future_only_lines:
        return list(lines)
    kept = [line for line in lines if str(line or "").strip() not in future_only_lines]
    if len(kept) != len(lines):
        log.info(
            "[prompt-manager] Dropped %d future context line(s) newer than the reply target",
            len(lines) - len(kept),
        )
    return kept


def _collect_same_author_burst_continuation_texts(
    message: discord.Message,
    items: Iterable[Mapping[str, Any]],
    *,
    max_window_sec: float = 15.0,
    max_messages: int = 3,
) -> list[str]:
    """対象メッセージが『novelaiさあ』等の未完結な話題切り出し短文である場合、
    直後 max_window_sec 秒以内に同一話者が連投した補足メッセージ本文を時系列順で返す。
    """
    from .thread_tracker import looks_like_incomplete_topic_opener
    target_text = str(content(message) or "").strip()
    if not looks_like_incomplete_topic_opener(target_text):
        return []

    target_aid = author_id(message)
    if target_aid is None:
        return []
    boundary_id, boundary_ts = context_boundary_of(message)
    if boundary_id is None and boundary_ts is None:
        return []

    continuations: list[str] = []
    seen_texts: set[str] = {target_text}
    for item in items:
        if item.get("author_id") != target_aid:
            continue
        if item.get("role") == "assistant":
            continue
        if not is_context_item_after_boundary(
            item,
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            exclude_boundary_message=True,
        ):
            continue
        item_ts = item.get("created_at_ts")
        if boundary_ts is not None and isinstance(item_ts, (int, float)):
            if (float(item_ts) - float(boundary_ts)) > max_window_sec:
                continue
        c_text = str(item.get("content") or "").strip()
        if not c_text:
            raw_line = str(item.get("line") or "").strip()
            if ":" in raw_line:
                c_text = raw_line.split(":", 1)[1].strip()
        c_text = re.sub(r"\s*\[(?:Bot)?リアクション:[^\]]+\]\s*$", "", c_text).strip()
        if c_text and c_text not in seen_texts:
            seen_texts.add(c_text)
            continuations.append(c_text)
            if len(continuations) >= max_messages:
                break
    return continuations


_REACTION_SUFFIX_RE = re.compile(r"\s*\[(?:Bot)?リアクション:[^\]]+\]\s*$")
_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\u200d\ufe0e\ufe0f]"
)
_REPLY_TARGET_IN_SPEAKER_RE = re.compile(r"\((>>[^)]*)\)")


def _strip_reaction_suffix(line: str) -> str:
    """履歴行末尾の `[リアクション: ...]` サフィックスを除去したベース文字列を返す。"""
    return _REACTION_SUFFIX_RE.sub("", str(line or "").strip()).strip()


def _has_reaction_suffix(line: str) -> bool:
    """履歴行末尾に `[リアクション: ...]` サフィックスが含まれるか判定する。"""
    return bool(_REACTION_SUFFIX_RE.search(str(line or "").strip()))


def _normalize_context_line_key(line: str) -> str:
    """比較用ベース文字列を生成する。末尾リアクション除去および (>>...) 内の感情絵文字の揺れを正規化する。"""
    base = _strip_reaction_suffix(line)
    if not base:
        return ""

    def _clean_reply_target(m: re.Match[str]) -> str:
        inner = m.group(1)
        cleaned = _EMOJI_RE.sub("", inner)
        cleaned = re.sub(r"^>>\s*", ">>", cleaned).strip()
        return f"({cleaned})"

    return _REPLY_TARGET_IN_SPEAKER_RE.sub(_clean_reply_target, base)


def _extract_clean_speaker_name(line: str) -> str:
    raw = str(line or "").strip()
    if not raw or raw.startswith("※"):
        return ""
    raw = re.sub(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+", "", raw).strip()
    if ":" not in raw and "：" not in raw:
        return ""
    raw_speaker = re.split(r"[:：]", raw, maxsplit=1)[0].strip()
    while True:
        next_speaker = re.sub(
            r"\s*(?:\((?:>>[^)]*|独立発言|[^)]*への返信)\)|\[(?:>>[^\]]*|独立発言|[^\]]*への返信)\])\s*$",
            "",
            raw_speaker,
        ).strip()
        if next_speaker == raw_speaker:
            break
        raw_speaker = next_speaker
    return raw_speaker.strip()


def _collect_reply_root_burst(
    context_lines: Sequence[str],
    root_text: str,
    root_author: str,
    max_lines: int = 3,
) -> list[str]:
    """返信ツリー起点の発言の直前にある、同じ発言者の連続投稿行（最大 max_lines 行）を時系列順で返す。"""
    root = str(root_text or "").strip()
    author = str(root_author or "").strip()
    if not root or not author:
        return []
    idx = -1
    for i, line in enumerate(context_lines):
        if root in str(line):
            idx = i
    if idx <= 0:
        return []
    burst: list[str] = []
    j = idx - 1
    while j >= 0 and len(burst) < max_lines:
        if _extract_clean_speaker_name(context_lines[j]) != author:
            break
        burst.insert(0, str(context_lines[j]))
        j -= 1
    return burst


def _dedup_context_lines_preferring_reactions(
    *line_groups: Iterable[str],
    exclude_base_lines: set[str] | None = None,
) -> list[str]:
    """末尾の `[リアクション: ...]` を除き、(>>...) 内の絵文字差分を正規化したベースキーで重複排除し、リアクション情報付きの行を優先保持する。"""
    excluded = {
        _normalize_context_line_key(x)
        for x in (exclude_base_lines or set())
        if _normalize_context_line_key(x)
    }
    merged: list[str] = []
    base_to_idx: dict[str, int] = {}
    for group in line_groups:
        for raw_line in group:
            line = str(raw_line or "").strip()
            if not line:
                continue
            base = _normalize_context_line_key(line)
            if not base or base in excluded:
                continue
            if base in base_to_idx:
                idx = base_to_idx[base]
                if not _has_reaction_suffix(merged[idx]) and _has_reaction_suffix(line):
                    merged[idx] = line
            else:
                base_to_idx[base] = len(merged)
                merged.append(line)
    return merged


def sanitize_deliberation_avoid(
    avoid: str,
    *,
    reply_strategy: str = "",
    is_request: bool = False,
) -> str:
    """事前分析（Pre-reply analyzer）が生成した avoid の主客逆転・自己矛盾をサニタイズする。

    特に「要望を受け止める」「リクエストに応じる」などの受容動作が avoid（避けること）に
    指定されているケースや、「要望を受け止めるのを避ける」のように自己矛盾しているケースを
    安全な指示に修正・除去する。また相手の語彙尊重に反する誤った指示も防護する。
    """
    raw = str(avoid or "").strip()
    if not raw:
        return ""

    # 1. 相手の語彙や表現の尊重に反する誤った avoid（「相手の単語を使うのを避ける」等）
    if re.search(r"相手の(?:言葉|単語|表現|名詞|固有名詞)を(?:使う|そのまま使う|用いる)(?:のを避ける|を避ける)", raw):
        log.warning("[prompt-manager] Harmful vocabulary avoidance directive sanitized: %r", raw)
        return "文頭で相手の文を丸写しするオウム返しを避ける"

    # 2. 「要望・リクエスト」関連の主客逆転・自己矛盾パターン
    request_receptive_patterns = [
        r"要望を受け止め",
        r"要望に応え",
        r"要望に乗っか",
        r"リクエストに応",
        r"リクエストを受け止め",
        r"言葉遊びに付き合",
        r"相手の要望を受け",
        r"相手のリクエスト",
        r"指定された言葉を言",
    ]

    has_negative_avoid_target = any(
        kw in raw for kw in ("拒絶", "無視", "放棄", "断る", "突っぱね", "突き放す", "冷たい態度", "頭ごなし")
    )

    for pat in request_receptive_patterns:
        if re.search(pat, raw):
            # 「要望を受け止めるのを避ける」のような明白な自己矛盾
            if re.search(rf"{pat}[^。、\n]*(?:のを避ける|を避ける|ことのないよう|しないように)", raw):
                log.warning("[prompt-manager] Contradictory avoid directive detected and sanitized: %r", raw)
                return "頭ごなしな拒絶やリクエストの放棄を避ける"
            # 「要望を受け止め、キャラクターらしくリアクションする」のように肯定行動がそのまま avoid に入っている場合
            elif not has_negative_avoid_target:
                log.warning("[prompt-manager] Inverted positive action in avoid directive sanitized: %r", raw)
                return "頭ごなしな拒絶やリクエストの放棄を避ける"
    # 3. クイズ・パズル・暗号解読を無理に雑談・人間同士とみなして解答放棄するパターン
    if any(kw in raw for kw in ("暗号", "ゲーム", "パズル", "クイズ")) and any(
        kw in raw for kw in ("雑談として", "人間同士", "無理に", "AIへの依頼", "AI への依頼", "自分宛て")
    ):
        log.warning("[prompt-manager] Puzzle avoidance directive sanitized: %r", raw)
        return "解答を放棄したり雑談としてはぐらかすのを避け、法則を推論して答える"

    # 4. 人間同士のやり取りに対するメタ禁止文の引き写し・二重否定パターン
    # （例: "人間同士のやり取りを無理に AI 宛てと解釈せず、文脈を破壊する" 等）
    if any(kw in raw for kw in ("文脈を破壊", "解釈せず", "無理にAI宛て", "無理に自分宛て")) or (
        "人間同士" in raw and any(kw in raw for kw in ("破壊", "解釈せず", "無視せず", "当事者"))
    ):
        log.warning("[prompt-manager] Meta forbidden text / double negation in avoid sanitized: %r", raw)
        return "自分を当事者と誤認した被害者的な反応や話題拒絶を避ける"

    return raw


def _is_bot_utterance_already_present(
    bot_text: str,
    context_lines: list[str],
    user_text_for_prompt: str = "",
) -> bool:
    """直前のAI発言がすでに会話履歴や返信引用プレフィックスに含まれているかを判定する。"""
    clean_bt = str(bot_text or "").strip()
    if not clean_bt:
        return False
    prefix = clean_bt[:60] if len(clean_bt) >= 60 else clean_bt
    for line in context_lines:
        line_str = str(line or "").strip()
        if clean_bt in line_str or prefix in line_str:
            return True
    if clean_bt in user_text_for_prompt or prefix in user_text_for_prompt:
        return True
    return False


async def _build_user_prompt(
    message: discord.Message,
    cache: dict[int, deque[dict[str, Any]]],
    memory_store: MemoryStore,
    *,
    bot_user_id: Optional[int],
    emotion_state: EmotionState | None = None,
    core_beliefs: Optional[list[str]] = None,
    effective_user_text: str = "",
    media_prompt_parts: Optional[list[str]] = None,
    habit_profile_lines: Optional[list[str]] = None,
    fetch_recent_context_lines: Callable[[discord.Message], Awaitable[list[str]]],
    find_last_assistant_message_text: Callable[..., Awaitable[str]],
    preferred_emotion_tags: Optional[list[str]] = None,
    user_relationships: Optional[dict[int, Any]] = None,
    bot_identity: Optional[BotIdentity] = None,
    thread_tracker: Optional[ThreadTracker] = None,
    tool_registry: Any = None,
    channel_summary: Optional[str] = None,
) -> tuple[str, list[int], list[str], str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    from . import context_helpers
    from harness.agent.memory_manager import logic as memory_logic
    from . import prospective_memory
    from . import user_interest_logic
    from . import user_profile_logic
    from .working_memory import (
        build_working_context,
        format_working_context_for_prompt,
    )
    from ollama_bot.common import ollama_helpers

    cid = channel_id(message)
    current_thread: Optional[ConversationThread] = None
    raw_current_thread_turns: list[ContextTurn] = []
    parallel_threads: list[ConversationThread] = []
    origin_ctx: dict[str, str] = {}
    if thread_tracker is not None and cid is not None:
        active_threads_fn: Any = getattr(memory_store, "get_active_channel_threads", None) if memory_store is not None else None
        if (
            not thread_tracker.get_active_threads(cid)
            and callable(active_threads_fn)
        ):
            try:
                min_active_ts = time.time() - getattr(thread_tracker, "thread_max_idle_sec", 900.0)
                guild_id_val = getattr(getattr(message, "guild", None), "id", None)
                try:
                    db_threads = await active_threads_fn(
                        persona=memory_logic._persona_from_cfg(cfg),
                        guild_id=guild_id_val,
                        channel_id=cid,
                        min_active_ts=min_active_ts,
                    )
                except TypeError:
                    db_threads = await active_threads_fn(channel_id=cid)
                if isinstance(db_threads, list) and db_threads:
                    thread_tracker.hydrate_channel_threads(cid, db_threads)
            except Exception as e:
                log.debug("[prompt-manager] Failed to hydrate channel threads from db: %r", e)

        matched_thread = thread_tracker.match_thread(message, bot_user_id=bot_user_id, bot_identity=bot_identity)
        raw_current_thread_turns = list(matched_thread.turns)
        # 返信待機中に届いた後続メッセージ（短文連投の続き等）は record_message で同じスレッドへ
        # 追加され続けるため、対象メッセージを上限としたスナップショットでプロンプトを組み立てる。
        boundary_id, boundary_ts = context_boundary_of(message)
        current_thread = matched_thread.snapshot_until(boundary_id=boundary_id, boundary_ts=boundary_ts)
        excluded_thread_ids: set[str] = {current_thread.thread_id}
        if current_thread.parent_thread_id:
            excluded_thread_ids.add(current_thread.parent_thread_id)
        parallel_threads = thread_tracker.get_parallel_threads(
            cid,
            current_thread.thread_id,
            before_message=message,
            exclude_thread_ids=excluded_thread_ids,
        )
        origin_ctx = thread_tracker.get_message_origin_context(message, bot_user_id=bot_user_id, bot_identity=bot_identity)

    cached_prompt_items = recent_context_items(
        cache,
        cid,
        before_message=message,
        max_age_sec=_context_max_age_sec(),
    ) if cid is not None else []
    context_lines = [str(item.get("line", "")) for item in cached_prompt_items if item.get("line")]
    handled_message_ids: list[int] = []

    fetched_context_lines = await fetch_recent_context_lines(message)
    if fetched_context_lines:
        context_lines = _dedup_context_lines_preferring_reactions(context_lines, fetched_context_lines)
    elif context_lines:
        context_lines = _dedup_context_lines_preferring_reactions(context_lines)
    # 最終防御: 取得経路（キャッシュ／外部注入の履歴取得）に関わらず、対象メッセージより後にしか
    # 存在しない発言行は会話履歴から除外する。
    future_only_lines = _future_only_context_lines(
        message,
        [*(cache.get(cid, []) if cid is not None else []), *raw_current_thread_turns],
    )
    context_lines = _drop_future_context_lines(context_lines, future_only_lines)
    context_lines = _filter_prompt_context_lines(context_lines)
    context_lines = _dedup_context_lines_preferring_reactions(context_lines)

    parts = list(USER_REPLY_BASE_PARTS)

    last_assistant_text = await find_last_assistant_message_text(message, bot_user_id=bot_user_id, cache=cache)
    user_text_for_prompt = (effective_user_text or content(message) or "（本文なし）").strip()
    burst_continuations = _collect_same_author_burst_continuation_texts(
        message,
        [*(cache.get(cid, []) if cid is not None else []), *raw_current_thread_turns],
    )
    if burst_continuations:
        missing_cont = [t for t in burst_continuations if t not in user_text_for_prompt]
        if missing_cont:
            user_text_for_prompt = f"{user_text_for_prompt}\n" + "\n".join(missing_cont)
            log.info(
                "[prompt-manager] Coalesced %d same-author burst continuation(s) into target text: %r",
                len(missing_cont),
                user_text_for_prompt[:100],
            )
    log.info(
        "[prompt-manager] _build_user_prompt starting: cid=%s, author=%s (text=%r)",
        cid,
        author_id(message),
        user_text_for_prompt[:60],
    )
    reply_chain = await collect_reply_chain(message)
    pre_pruned_reply_parent: Optional[dict[str, Any]] = None
    if cid is not None and reply_chain:
        latest_ref_for_parent = reply_chain[-1]
        latest_ref_mid_for_parent = getattr(latest_ref_for_parent, "id", None)
        if isinstance(latest_ref_mid_for_parent, int):
            pre_pruned_reply_parent = find_reply_parent_from_cache(
                cache,
                cid,
                latest_ref_mid_for_parent,
                bot_user_id=bot_user_id,
                before_message=message,
            )
        reply_chain = prune_reply_chain_from_cache(cache, cid, reply_chain)
    if thread_tracker is not None and cid is not None and current_thread is not None:
        excluded_tids: set[str] = {current_thread.thread_id}
        if current_thread.parent_thread_id:
            excluded_tids.add(current_thread.parent_thread_id)
        if reply_chain:
            msg_map = getattr(thread_tracker, "_message_to_thread", {}).get(cid, {})
            for rc_msg in reply_chain:
                rc_mid = getattr(rc_msg, "id", None)
                if isinstance(rc_mid, int) and rc_mid in msg_map:
                    excluded_tids.add(msg_map[rc_mid])
        parallel_threads = thread_tracker.get_parallel_threads(
            cid,
            current_thread.thread_id,
            before_message=message,
            exclude_thread_ids=excluded_tids,
        )
        effective_origin_topic = current_thread.origin_topic or (origin_ctx.get("origin_topic") if origin_ctx else None)
        if effective_origin_topic:
            parallel_threads = [pt for pt in parallel_threads if pt.topic != effective_origin_topic]
    latest_ref = reply_chain[-1] if reply_chain else None
    ref_author_id_val = author_id(latest_ref) if latest_ref else None
    is_reply_to_bot = bool(latest_ref is not None and bot_user_id is not None and ref_author_id_val == bot_user_id)
    is_reply_to_other_user = bool(latest_ref is not None and not is_reply_to_bot)
    if not latest_ref and "への返信）" in user_text_for_prompt:
        if "あなたの" in user_text_for_prompt or "Assistant" in user_text_for_prompt:
            is_reply_to_bot = True
        else:
            is_reply_to_other_user = True

    is_reply_message = bool(latest_ref is not None or is_reply_to_bot or is_reply_to_other_user or "への返信）" in user_text_for_prompt)

    is_replying_to_old_message = "への返信）" in user_text_for_prompt
    if is_replying_to_old_message and last_assistant_text:
        match = re.search(r"（.+?の「(.+)」への返信）", user_text_for_prompt, flags=re.DOTALL)
        if match:
            ref_text = match.group(1).strip()
            if ollama_helpers._normalize_compare_text(ref_text) in ollama_helpers._normalize_compare_text(last_assistant_text):
                is_replying_to_old_message = False

    is_reply_to_other_quick = is_replying_to_old_message and not ("あなたの" in user_text_for_prompt or "Assistant" in user_text_for_prompt)
    clean_user_text = context_helpers._strip_reply_prefix(user_text_for_prompt)
    is_dependent_on_prev = (
        context_helpers._looks_like_context_dependent_message(clean_user_text)
        or (is_replying_to_old_message and not is_reply_to_other_quick)
    )
    is_standalone_topic = context_helpers._looks_like_standalone_new_topic(
        user_text_for_prompt,
        last_assistant_text=last_assistant_text,
        is_reply=is_reply_message,
        context_lines=context_lines,
    )
    clean_search_user_text = re.sub(
        r"^[（(](?:あなたの「.*?」|[^）)]+?)への返信[）)]\s*",
        "",
        user_text_for_prompt,
        flags=re.DOTALL,
    ).strip() or user_text_for_prompt

    if is_reply_to_other_quick or is_standalone_topic or not is_dependent_on_prev:
        search_context_text = clean_search_user_text
    else:
        search_context_text = f"{last_assistant_text} {clean_search_user_text}".strip() if last_assistant_text else clean_search_user_text

    is_hearsay, hearsay_subject, hearsay_quote = detect_third_party_hearsay(
        user_text_for_prompt,
        context_lines=context_lines,
    )

    message_guild = getattr(message, "guild", None)
    gid = getattr(message_guild, "id", None) if message_guild is not None else None

    raw_memory_lines, raw_channel_summary = await memory_logic.get_relevant_memories(
        cfg=cfg,
        store=memory_store,
        guild_id=gid,
        channel_id=cid,
        user_id=author_id(message),
        preferred_emotion_tags=preferred_emotion_tags,
        user_text=search_context_text,
        include_channel_memories=bool(cfg("MEMORY_INCLUDE_CHANNEL_MEMORIES_IN_REPLY", True)),
        include_channel_summary=True,
    )
    if not raw_channel_summary and channel_summary:
        raw_channel_summary = channel_summary
    raw_memory_lines = sanitize_memory_candidates(raw_memory_lines, last_assistant_text=last_assistant_text)
    habit_profile_lines = list(habit_profile_lines or [])
    raw_recent_bot_utterances = await memory_store.get_recent_bot_utterances(
        persona=memory_logic._persona_from_cfg(cfg),
        guild_id=gid,
        channel_id=cid,
        limit=3,
        max_age_sec=_context_max_age_sec(),
    )
    recent_bot_utterances: list[str] = []
    for raw_utterance in raw_recent_bot_utterances:
        cleaned_utterance = ollama_helpers.extract_first_user_facing_reply(str(raw_utterance or ""))
        if cleaned_utterance and not ollama_helpers.looks_like_abnormal_assistant_reply(cleaned_utterance):
            recent_bot_utterances.append(cleaned_utterance)
    if is_replying_to_old_message and recent_bot_utterances:
        match = re.search(r"（.+?の「(.+)」への返信）", user_text_for_prompt, flags=re.DOTALL)
        if match:
            ref_text = match.group(1).strip()
            ref_norm = ollama_helpers._normalize_compare_text(ref_text)
            if any(
                ref_norm in ollama_helpers._normalize_compare_text(u)
                or ollama_helpers._normalize_compare_text(u) in ref_norm
                for u in recent_bot_utterances
            ):
                is_replying_to_old_message = False
    external_research_request = _looks_like_external_research_context_request(user_text_for_prompt)
    recent_turns = _build_recent_turns({}, channel_id_value=None, context_lines=context_lines, limit=12)
    active_thread_dicts = [
        {"topic": t.topic, "participants": t.participant_names}
        for t in (thread_tracker.get_active_threads(cid) if thread_tracker and cid is not None else [])
    ]
    current_topic_val = current_thread.topic if current_thread else (origin_ctx.get("current_topic") or None)
    working_ctx = build_working_context(
        recent_turns,
        channel_summary=raw_channel_summary,
        emotion_state=emotion_state,
        last_bot_text=last_assistant_text,
        active_threads=active_thread_dicts,
        current_thread_topic=current_topic_val,
    )
    if origin_ctx:
        working_ctx["origin_context"] = origin_ctx
    pending_intents = await prospective_memory.match_cues(
        store=memory_store,
        persona=memory_logic._persona_from_cfg(cfg),
        guild_id=gid,
        channel_id=cid,
        user_id=author_id(message),
        text=user_text_for_prompt,
    )
    working_ctx["future_prompts"] = pending_intents
    episode_lines = await _get_relevant_episode_lines(
        memory_store=memory_store,
        guild_id=gid,
        channel_id_value=cid,
        user_id=author_id(message),
        current_user_text=user_text_for_prompt,
        working_ctx=working_ctx,
    )
    cached_reply_parent: Optional[dict[str, Any]] = None
    if is_reply_to_bot and latest_ref is not None and len(reply_chain) <= 1:
        ref_mid = getattr(latest_ref, "id", None)
        cached_reply_parent = pre_pruned_reply_parent or find_reply_parent_from_cache(
            cache,
            cid,
            ref_mid if isinstance(ref_mid, int) else None,
            bot_user_id=bot_user_id,
            before_message=message,
        )
        if isinstance(cached_reply_parent, dict) and str(cached_reply_parent.get("role", "")).lower() == "system":
            cached_reply_parent = None

    reply_context_hint: Optional[str] = None
    if reply_chain and latest_ref is not None:
        tree_summary_lines = _format_reply_tree_context(
            reply_chain,
            bot_user_id=bot_user_id,
            cached_parent_turn=cached_reply_parent,
        )
        if len(tree_summary_lines) >= 2:
            parent_summaries = tree_summary_lines[:-1]
            reply_context_hint = " -> ".join(s.lstrip("・") for s in parent_summaries)

    if bot_identity is None:
        bot_identity = resolve_bot_identity(bot=None, message=message)

    # 会話中で言及されたサーバーメンバーの事前解決
    author_name_val = getattr(getattr(message, "author", None), "name", "")
    author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
    exclude_names = [author_disp_val, author_name_val] if author_disp_val or author_name_val else None

    resolved_mentioned_users, mentioned_parts = await user_profile_logic.resolve_and_format_mentioned_users(
        user_text=user_text_for_prompt,
        context_lines=context_lines,
        guild_id=gid,
        exclude_user_id=author_id(message),
        exclude_names=exclude_names,
        memory_store=memory_store,
        user_relationships=user_relationships,
        cfg_fn=cfg,
        current_user_name=author_disp_val,
        guild=message_guild,
    )

    analysis_last_assistant = "" if is_standalone_topic else last_assistant_text
    intent_info, memory_lines, channel_summary, deliberation_info = await _analyze_pre_reply_context(
        current_user_text=user_text_for_prompt,
        last_assistant_text=analysis_last_assistant,
        memory_lines=raw_memory_lines,
        channel_summary=raw_channel_summary,
        preferred_emotion_tags=preferred_emotion_tags,
        is_reply_to_other_user=is_reply_to_other_user,
        is_reply_to_bot=is_reply_to_bot,
        reply_context_hint=reply_context_hint,
        recent_bot_utterances=recent_bot_utterances,
        bot_identity=bot_identity,
        mentioned_users=resolved_mentioned_users,
        current_user_name=author_disp_val,
        context_lines=context_lines,
    )
    memory_lines = sanitize_memory_candidates(memory_lines, last_assistant_text=analysis_last_assistant)

    log.info(
        "[prompt-manager] Pre-reply analysis: intent=%r target_is_ai=%s needs_reasoning=%s external_research=%s strategy=%r breakdown=%s",
        intent_info.get("intent"),
        bool(intent_info.get("target_is_ai")),
        bool(intent_info.get("needs_reasoning")),
        bool(
            intent_info.get("external_research")
            or intent_info.get("needs_external_research")
            or intent_info.get("reason") == "heuristic_external_research_request"
        ),
        deliberation_info.get("reply_strategy"),
        bool(deliberation_info.get("is_breakdown")),
    )

    # クイズ・パズル・ヒント検出の多重防護
    is_puzzle_detected = (
        intent_info.get("intent") == "クイズ・パズル"
        or intent_info.get("reason") == "heuristic_puzzle_or_riddle"
        or _looks_like_puzzle_or_riddle_message(user_text_for_prompt, context_lines=context_lines)
    )
    if is_puzzle_detected:
        intent_info["intent"] = "クイズ・パズル"
        intent_info["target_is_ai"] = True
        intent_info["should_clarify"] = False
        intent_info["needs_reasoning"] = True
        intent_info["reason"] = "heuristic_puzzle_or_riddle"
        deliberation_info["reply_strategy"] = "論理推論・解答"
        deliberation_info["avoid"] = "解答を放棄したり雑談としてはぐらかすのを避け、法則を推論して答える"
        deliberation_info["uncertainty_level"] = "low"
        deliberation_info["needs_clarification"] = False
        deliberation_info["is_breakdown"] = False
        deliberation_info["breakdown_reason"] = ""
        if any(w in str(deliberation_info.get("emotion_focus", "")) for w in ("混乱", "焦り", "困惑", "confusion", "panic")):
            deliberation_info["emotion_focus"] = ""

    # deliberation_info の avoid 整合性防御（主客逆転・自己矛盾のサニタイズ）
    raw_avoid = str(deliberation_info.get("avoid") or "").strip()
    is_req = bool(
        str(intent_info.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}
        or deliberation_info.get("reply_strategy") == "要望受容・ツッコミ"
    )
    deliberation_info["avoid"] = sanitize_deliberation_avoid(
        raw_avoid,
        reply_strategy=str(deliberation_info.get("reply_strategy") or ""),
        is_request=is_req,
    )
    is_self_tsukkomi = False
    matched_bot_utterance = ""
    matched_kw = ""
    is_ai_confirmation = looks_like_ai_utterance_confirmation(
        user_text_for_prompt,
        last_assistant_text=last_assistant_text,
    )
    if not resolved_mentioned_users and not is_ai_confirmation:
        is_self_tsukkomi, matched_bot_utterance, matched_kw = _detect_recent_bot_utterance_tsukkomi(
            user_text_for_prompt,
            recent_bot_utterances,
        )
    if is_self_tsukkomi:
        log.info(
            "[prompt-manager] Bot utterance tsukkomi detected: kw=%r, matched_utterance=%r",
            matched_kw,
            matched_bot_utterance[:50],
        )
        deliberation_info["is_breakdown"] = True
        if matched_kw in ("呼び捨て", "タメ口", "ため口", "タメぐち", "敬語", "さん付け", "ちゃん付け", "君付け", "くん付け", "呼び方", "急にタメ"):
            deliberation_info["breakdown_reason"] = f"直前の自身の相手に対する呼び方・口調（{matched_kw}）へのツッコミ"
        else:
            deliberation_info["breakdown_reason"] = f"直前のAI自身の発言（「{matched_kw}…」）に対するツッコミ・矛盾の指摘"
        intent_info["target_is_ai"] = True

    curr_user_id = author_id(message)
    author_aliases: list[str] = []
    if curr_user_id is not None:
        try:
            from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
            author_aliases = await get_user_aliases_from_store(
                curr_user_id,
                guild=message_guild,
                memory_store=memory_store,
            )
        except Exception:
            pass

    user_interest_lines: list[str] = []
    if (
        curr_user_id is not None
        and memory_store is not None
        and hasattr(memory_store, "list_user_interests")
    ):
        try:
            persona_name = memory_logic._persona_from_cfg(cfg)
            raw_user_interests = await memory_store.list_user_interests(
                persona=persona_name,
                guild_id=gid,
                user_id=curr_user_id,
                limit=3,
            )
            if not isinstance(raw_user_interests, list):
                raw_user_interests = []

            # 現在のスレッドトピックに合致する関心事の取得・優先マージ（get_user_interest 接続）
            matched_interest: dict[str, Any] | None = None
            interest_fn: Any = getattr(memory_store, "get_user_interest", None)
            if current_thread and current_thread.topic and callable(interest_fn):
                try:
                    matched_interest = await interest_fn(
                        persona=persona_name,
                        guild_id=gid,
                        user_id=curr_user_id,
                        topic=current_thread.topic,
                    )
                except TypeError:
                    # profiles.py の get_user_interest(self, interest_id: int) との互換性
                    target_id: int | None = None
                    for it in raw_user_interests:
                        if isinstance(it, dict) and current_thread.topic in str(it.get("topic", "")):
                            target_id = it.get("id")
                            break
                    if target_id is None and raw_user_interests:
                        target_id = raw_user_interests[0].get("id")
                    if target_id is not None:
                        try:
                            matched_interest = await interest_fn(int(target_id))
                        except Exception:
                            matched_interest = None
                except Exception:
                    matched_interest = None

            if matched_interest and isinstance(matched_interest, dict):
                m_topic = str(matched_interest.get("topic") or "")
                raw_user_interests = [
                    it for it in raw_user_interests
                    if str(it.get("topic") or "") != m_topic
                ]
                raw_user_interests.insert(0, matched_interest)

            # スレッド参加者の関心事（補助参照: 最大1〜2件）
            participant_interests: list[dict[str, Any]] = []
            if current_thread and current_thread.participant_ids:
                other_user_ids = [uid for uid in current_thread.participant_ids if uid != curr_user_id]
                for other_uid in other_user_ids[:2]:
                    try:
                        other_items = await memory_store.list_user_interests(
                            persona=persona_name,
                            guild_id=gid,
                            user_id=other_uid,
                            limit=1,
                        )
                        if isinstance(other_items, list) and other_items:
                            participant_interests.extend(other_items[:1])
                    except Exception:
                        pass

            combined_interests = raw_user_interests[:3] + participant_interests[:2]
            if combined_interests:
                interest_block = user_interest_logic.format_user_interests_for_prompt(
                    combined_interests,
                    limit=4,
                )
                if interest_block:
                    user_interest_lines = ollama_helpers.truncate_lines(
                        [line.strip() for line in interest_block.splitlines() if line.strip()],
                        max_lines=4,
                        max_chars_per_line=max(int(cfg("OLLAMA_PROMPT_INTEREST_MAX_CHARS_PER_LINE", 140) or 140), 60),
                        max_total_chars=max(int(cfg("OLLAMA_PROMPT_INTEREST_MAX_TOTAL_CHARS", 320) or 320), 100),
                    )
        except Exception:
            user_interest_lines = []

    # 予算計算用に感情状態テキストおよび関心事テキストの文字数を計測する（実際に削減はしない）
    _emotion_state_parts = build_agent_persona_parts(
        emotion_state,
        core_beliefs,
        current_user_name=author_disp_val,
        aliases=author_aliases,
        target_user_id=curr_user_id,
        user_text=effective_user_text,
    )
    _emotion_state_chars = sum(len(line) for line in _emotion_state_parts) + sum(
        len(line) for line in user_interest_lines
    )
    # working_context を個別上限でトリムしてから予算管理へ渡す
    _working_prompt_lines_raw = ollama_helpers.truncate_lines(
        format_working_context_for_prompt(working_ctx),
        max_lines=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_LINES", 6) or 6), 2),
        max_chars_per_line=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_CHARS_PER_LINE", 180) or 180), 80),
        max_total_chars=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_TOTAL_CHARS", 420) or 420), 120),
    )
    (
        context_lines,
        memory_lines,
        channel_summary,
        last_assistant_text,
        habit_profile_lines,
        episode_lines,
        working_prompt_lines,
    ) = _trim_prompt_inputs(
        context_lines=context_lines,
        memory_lines=memory_lines,
        channel_summary=channel_summary,
        last_assistant_text=last_assistant_text,
        habit_profile_lines=habit_profile_lines,
        episode_lines=episode_lines,
        working_prompt_lines=_working_prompt_lines_raw,
        emotion_state_chars=_emotion_state_chars,
    )
    log.info(
        "[prompt-manager] Inputs trimmed: context_lines=%d, memory_lines=%d, episodes=%d, working_lines=%d",
        len(context_lines),
        len(memory_lines),
        len(episode_lines),
        len(working_prompt_lines),
    )

    if channel_summary and not external_research_request and not is_standalone_topic:
        parts += ["", "最近の会話要約:", channel_summary]

    parts += build_agent_persona_parts(
        emotion_state,
        core_beliefs,
        current_user_name=author_disp_val,
        aliases=author_aliases,
        target_user_id=curr_user_id,
        user_text=effective_user_text,
    )

    if working_prompt_lines and not external_research_request:
        parts += [""]
        parts.extend(working_prompt_lines)

    if habit_profile_lines:
        parts += [
            "",
            "【相手ごとの会話習慣プロファイル】",
            "※この相手に合わせるための軽い傾向です。直近の発言内容と矛盾する時は、直近の発言を優先してください。",
        ]
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if user_interest_lines and not external_research_request:
        parts += [
            "",
            "【相手の関心事・興味】",
            "※対話相手の普段の興味・関心です。現在の話題に関連する時だけ自然に触れ、無関係な時は無理に持ち出さないでください。",
        ]
        parts.extend(user_interest_lines)

    if memory_lines:
        parts += [
            "",
            "【この相手に関係すると確認できた記憶】",
            "※対象メッセージに直接役立つ時だけ使い、関係ない固有名詞・人物・作品名・過去話題は持ち込まないでください。",
            "※外部調査結果やURLの内容に関連する話題（登場人物・作品・設定など）が含まれる場合は、知っている話題として自然に関連付けて言及してください。",
            "※「私の記憶によると」や「データによると」といった表現は避けてください。",
            f"※過去の記憶テキスト内に「私」という一人称が含まれていても、発言する際は必ずあなた自身の設定した一人称（例: {bot_identity.first_person}）に変換してください。絶対に『私』を使わないでください。",
            "※記憶内の情報は相手ユーザーに関係する内容です。あなた自身の名前や属性と混同しないでください。",
        ]
        parts.extend([f"- {line}" for line in memory_lines])

    if episode_lines and not external_research_request:
        parts += [
            "",
            "【関連する出来事の記憶】",
            "※相手が前の出来事や続きに触れている時だけ参照し、無理に持ち出さないでください。",
            "※記憶内の「私」という一人称には釣られず、必ず設定された自身の一人称を維持してください。",
        ]
        parts.extend([f"- {line}" for line in episode_lines])

    bot_already_in_context = _is_bot_utterance_already_present(
        last_assistant_text,
        context_lines,
        user_text_for_prompt,
    )

    if (
        recent_bot_utterances
        and not external_research_request
        and not is_reply_to_other_user
        and not (is_reply_to_bot and latest_ref is not None)
        and not is_replying_to_old_message
        and not is_standalone_topic
    ):
        parts += [
            "",
            "【直近の自分の発言】",
        ]
        if is_hearsay:
            parts += [
                "※直前にあなた自身が発言した内容です。",
                f"※最新のユーザー発言は第三者『{hearsay_subject}』さんの過去の発言・伝聞についての報告です。あなた（AI）自身が言ったことではありません。「そんなこと言ったっけ？」「言った覚えがない」など、自分が発言したかのように勘違いして弁明・否定しないでください。",
            ]
        else:
            parts += [
                "※直前にあなた自身が発言した内容です。ユーザーがそれに対して質問・確認・ツッコミ・詳細要求（「どういうこと？」「何があったの？」「それ何？」「教えて」等）をしてきた場合は、自分自身が言ったこととして自覚し、「覚えてない」「何のことか分からない」とシラを切らずに、自分が振った話題やその背景を踏まえて自然に答えてください。最新のユーザー発言が前の話題に触れていないなら、その話題は引きずらずに捨ててください。重複や言い直しは避けてください。",
                "※自己整合性とスタンス維持の厳守: 直前の自分の発言で表明したスタンス、好み、主張（例: 『〜が大好き』『〜を知っている』『〜に行った』等）について、相手から『マジすか』『本当に？』『マジで？』『知ってるの？』等の質問・確認・ツッコミを受けた場合、直前の自分の立場を突然ひるがえして『食べたことがない』『行ったことがない』『いつかやってみたい』などと前言撤回・未体験者ぶることは厳禁です。直前のスタンスを肯定・維持したまま自然に応答してください。",
            ]
        if bot_already_in_context:
            parts.extend([f"- {ollama_helpers.truncate_text(line.replace(chr(10), ' '), 100)}" for line in recent_bot_utterances])
        else:
            parts.extend([f"- {line}" for line in recent_bot_utterances])

    if (
        last_assistant_text
        and not is_replying_to_old_message
        and not is_reply_to_other_user
        and not (is_reply_to_bot and latest_ref is not None)
        and not is_standalone_topic
    ):
        display_last_assistant = (
            ollama_helpers.truncate_text(last_assistant_text.replace("\n", " "), 100)
            if bot_already_in_context
            else last_assistant_text
        )
        parts += ["", "直前のAI発言:", display_last_assistant]
        if external_research_request:
            parts += ["※最新メッセージや外部調査の内容を最優先してください。直前のAI発言と直接関連がある場合のみ参照してください。"]
        else:
            has_subsequent_user_messages = any(
                not line.strip().startswith("Assistant:")
                for line in context_lines[-3:]
                if ":" in line
            ) if context_lines else False
            if has_subsequent_user_messages:
                parts += [
                    "※直前のAI発言の後に他のユーザーの発言が続いています。最新発言がそれら直前の会話の流れに対するツッコミや反応である場合は、AI発言への回答ではなく現在の会話の流れを最優先してください。"
                ]
            else:
                parts += ["※最新のユーザー発言がこれに直接答えていないなら、この話題は完全に捨ててください。"]

    if media_prompt_parts:
        parts += list(media_prompt_parts)

    current_user_name = display_name(message).strip() or "ユーザー"
    clean_user_display_content = context_helpers._strip_reply_prefix(user_text_for_prompt) or user_text_for_prompt

    # 出来事メモ（recent_events）の描画
    event_lines: list[str] = []
    if bool(cfg("EVENT_MEMO_ENABLED", True)):
        working_context_for_events = working_ctx if isinstance(working_ctx, dict) else {}
        conv_state_for_events = working_context_for_events.get("conversation_state") if isinstance(working_context_for_events, dict) else None
        events = working_context_for_events.get("recent_events") or (
            conv_state_for_events.get("recent_events") if isinstance(conv_state_for_events, dict) else None
        )
        if isinstance(events, list) and events:
            event_lines.append("【直近の出来事メモ（行為者と行動）】")
            for ev in events[-5:]:
                at_str = f"[{ev.get('at', '')}] " if ev.get("at") else ""
                actor = ev.get("actor", "不明")
                action = ev.get("action", "")
                target = f"（相手: {ev.get('target')}）" if ev.get("target") else ""
                event_lines.append(f"- {at_str}{actor} が {action}{target}")
    if event_lines:
        parts += ["", *event_lines]

    if context_lines:
        if external_research_request:
            recent_context = context_lines[-3:]
            parts += [
                "",
                "直前の会話文脈（参考）:",
                *recent_context,
                "※外部調査結果やURLの内容を最優先で回答してください。ただし、直前の会話や対象メッセージと関連する話題（登場人物や作品、前後の文脈など）があれば自然に触れて構いません。無関係な話題は混ぜないでください。",
            ]
        elif (
            thread_tracker is not None
            and current_thread is not None
            and len(current_thread.turns) >= 2
        ):
            thread_turns = thread_tracker.get_thread_turns(
                current_thread,
                before_message=message,
                include_boundary_message=False,
            )
            current_thread_lines = _drop_future_context_lines(
                [str(t.get("line", "")) for t in thread_turns if t.get("line")],
                future_only_lines,
            )
            target_msg_line = f"{current_user_name}: {content(message)}".strip()
            clean_msg_line = f"{current_user_name}: {clean_user_display_content}".strip()
            exclude_target_bases = {target_msg_line, clean_msg_line}
            reaction_map = {
                _normalize_context_line_key(c_line): c_line.strip()
                for c_line in context_lines
                if _has_reaction_suffix(c_line)
            }
            upgraded_thread_lines = [
                reaction_map.get(_normalize_context_line_key(t_line), t_line)
                for t_line in current_thread_lines
            ]
            current_thread_lines = _dedup_context_lines_preferring_reactions(
                upgraded_thread_lines,
                exclude_base_lines=exclude_target_bases,
            )
            if not parallel_threads and len(current_thread_lines) < 8:
                current_thread_lines = _dedup_context_lines_preferring_reactions(
                    context_lines[-8:],
                    current_thread_lines,
                    exclude_base_lines=exclude_target_bases,
                )
            eff_topic_origin = str(deliberation_info.get("topic_origin") or "").strip()
            if not eff_topic_origin and origin_ctx:
                eff_topic_origin = str(origin_ctx.get("origin_topic") or origin_ctx.get("summary") or "").strip()
            if parallel_threads and eff_topic_origin:
                filtered_pts = []
                for pt in parallel_threads:
                    match_pname = any(pn and pn in eff_topic_origin for pn in pt.participant_names)
                    match_topic = bool(pt.topic and pt.topic != "雑談" and (pt.topic in eff_topic_origin or eff_topic_origin in pt.topic))
                    topic_words = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[ァ-ヶヴー]{2,}", pt.topic) if len(w) >= 2]
                    match_word = any(w in eff_topic_origin for w in topic_words)
                    if match_pname or match_topic or match_word:
                        continue
                    filtered_pts.append(pt)
                parallel_threads = filtered_pts

            if parallel_threads:
                parts += [
                    "",
                    f"【現在の会話スレッドの流れ（話題: {current_thread.topic}）】",
                    *current_thread_lines,
                ]
                if len(current_thread_lines) < 4 and context_lines:
                    recent_channel_lines = _dedup_context_lines_preferring_reactions(
                        context_lines[-8:],
                        exclude_base_lines=exclude_target_bases,
                    )
                    if recent_channel_lines:
                        parts += [
                            "",
                            "直近のチャンネル会話:",
                            *recent_channel_lines,
                        ]
                parts += [
                    "",
                    "【直近の周辺トピック・参考文脈】",
                    "※現在このチャンネル内では以下のトピックも話されています。直前の発言がこれらに関連する相槌やツッコミである場合は文脈の参考にして構いませんが、無関係な場合は話題を無理に混ぜないでください:",
                ]
                for pt in parallel_threads[:3]:
                    p_names = "、".join(pt.participant_names[:3])
                    parts.append(f"- 【{pt.topic}】（参加者: {p_names}）")
            else:
                if is_standalone_topic:
                    parts += [
                        "",
                        "※最新の対象メッセージは直前の会話とは独立した新しい話題です。過去の会話履歴の話題や固有名詞を持ち込まず、対象メッセージの内容に直接答えてください。",
                    ]
                else:
                    merged_lines = _dedup_context_lines_preferring_reactions(
                        context_lines,
                        current_thread_lines,
                        exclude_base_lines=exclude_target_bases,
                    )
                    effective_lines = merged_lines or context_lines
                    parts += ["", "会話履歴:", *effective_lines]
        else:
            if is_standalone_topic:
                parts += [
                    "",
                    "※最新の対象メッセージは直前の会話とは独立した新しい話題です。過去の会話履歴の話題や固有名詞を持ち込まず、対象メッセージの内容に直接答えてください。",
                ]
            else:
                parts += ["", "会話履歴:", *context_lines]


    target_calling_name = _format_user_calling_name(current_user_name)
    _non_user_prefixes = ("assistant", "system", "ai", "bot", "あなた", "内心のメモ")

    if reply_chain and latest_ref is not None:
        ref_content = content(latest_ref)
        ref_author_name = display_name(latest_ref).strip() or "ユーザー"
        if ref_content:
            tree_lines = _format_reply_tree_context(
                reply_chain,
                bot_user_id=bot_user_id,
                cached_parent_turn=cached_reply_parent,
            )
            if is_reply_to_other_user:
                parts += [
                    "",
                    "【返信・会話関係】",
                    f"※現在『{current_user_name}』さんは、別ユーザー『{ref_author_name}』さんの発言（「{ref_content}」）に対してDiscordの返信（Reply）機能で話しかけています。",
                    f"※文中の代名詞（君、お前、あなた等）や命令・呼びかけはすべて『{ref_author_name}』さんを指しています。",
                ]
            else:
                ref_max_len = 100 if bot_already_in_context else 80
                ref_short = ollama_helpers.truncate_text(ref_content.replace("\n", " "), ref_max_len)
                parts += [
                    "",
                    "【返信・会話関係（リプライの会話ツリー）】",
                    f"※現在『{current_user_name}』さんは、あなた（AI）の直前の発言（「{ref_short}」）に対して返信しています。",
                ]
                _subject_switch_pat = re.compile(
                    r"(?:^(?:じゃあ|なら|ところで|えっ|あ、|んで)?\s*|(?:^|[、，\s]))"
                    r"(?:俺|おれ|僕|ぼく|私|わたし|あたし|自分|うち|ワイ|わい)"
                    r"(?:は|って|のこと|の場合|の場|も|が)"
                )
                _subject_agreement_pat = re.compile(
                    r"(?:俺|おれ|僕|ぼく|私|わたし|あたし|自分|うち|ワイ|わい)\s*(?:も|だって)?\s*"
                    r"(?:そう思う|そうおもう|賛成|同感|同じ(?:意見|考え)?|同意|わかる|分る|解る|納得|そう考える|同じ意見|そう感じる)"
                )
                has_subject_switch = bool(_subject_switch_pat.search(clean_user_display_content))
                if has_subject_switch and _subject_agreement_pat.search(clean_user_display_content):
                    has_subject_switch = False
                subject_switch_rule = (
                    "※【重要：発言者自身の主語切り替え優先】返信ツリーの流れがあっても、"
                    "今回の相手が『俺は〜』『私は〜』『じゃあ俺は？』のように主語を自分自身に切り替えて質問・発言している場合は、"
                    "前ターンの別ユーザー（第三者）に対する評価や『別に恐れてない』『ドキッとしただけ』等の説明をそのまま流用せず、"
                    "今回の発言者本人との関係性・印象に基づいて新しく答えてください。"
                )
                if len(tree_lines) >= 2:
                    parts += [
                        "【返信ツリーの流れ】",
                        *tree_lines,
                        "↓（今回の返信）",
                        f"『{current_user_name}』: 「{clean_user_display_content}」",
                        "※主語・目的語の引き継ぎ: 今回の発言において主語や目的語（「何を」「何が」等）が省略されている場合（例: 『食べる派ですか？』『どう思う？』等）、直前のチャンネル全体の雑談ではなく、必ずこの【返信ツリーの流れ】にある話題を最優先で引き継いで解釈・返答してください。",
                        "※直前にチャンネル内で別の話題（雑談）が流れていても、話題を混同したり、勝手に別の話題にすり替えたりしないでください。",
                        "※ロール反転・主客逆転の禁止: 相手の発言が短い感嘆・確認（『マジすか』『本当？』『嘘でしょ』等）である場合、直前のあなたの発言・スタンスに相手が驚いてリアクションしている文脈です。『相手が語った体験に対して自分（未体験者）が感想を聞き返す』ようなロール反転は厳禁です。直前の自分の発言を受けて相手が驚いているという主客関係を正しく認識し、直前のスタンスを保って答えてください。",
                        "※前言撤回・未体験者ぶりの禁止: 直前の自分の発言で『大好き』『食べた』『知っている』と表明した立場を突然翻し、『食べたことない』『いつか食べてみたい』などと前言撤回することは絶対に禁止です。直前のスタンスを肯定・維持してください。",
                        "※直前話題の具体物を別の物にすり替えないこと。",
                        subject_switch_rule,
                    ]
                    if has_subject_switch:
                        parts.append(
                            "※【検知：発言者自身の主語切り替え】今回の発言者は自分自身（『俺は〜』等）を主語にしています。前ターンの相手への弁解や第三者への感情を流用せず、この発言者本人に向き合って新しく回答してください。"
                        )
                else:
                    parts += [
                        f"※重要指示: 今回の発言はあなたへのDiscord返信（Reply）です。主語や目的語が省略されている場合は、直前のチャンネル雑談ではなく、返信先の発言（「{ref_short}」）の文脈を最優先で引き継いで答えてください。",
                        "※ロール反転・前言撤回の禁止: 相手の発言が短い感嘆・確認（『マジすか』『本当？』等）である場合、直前のあなたの発言に対する相手の驚きです。主客を逆転させて未体験者ぶったり、直前に言った好み・スタンスを前言撤回することは厳禁です。直前のスタンスを肯定・維持してください。",
                        "※直前話題の具体物を別の物にすり替えないこと。",
                        subject_switch_rule,
                    ]
                    if has_subject_switch:
                        parts.append(
                            "※【検知：発言者自身の主語切り替え】今回の発言者は自分自身（『俺は〜』等）を主語にしています。前ターンの相手への弁解や第三者への感情を流用せず、この発言者本人に向き合って新しく回答してください。"
                        )

    # === 自己言及リコール（思い出しブロック注入） ===
    recall_block_lines: list[str] = []
    recall_enabled = bool(cfg("RECALL_ENABLED", True))
    has_bot_reference = bool(
        is_reply_to_bot
        or re.search(r"（.+?の「.+」への返信）", user_text_for_prompt, flags=re.DOTALL)
        or re.search(r"（返信先の発言（「.+?」）に対して返信しています）", user_text_for_prompt, flags=re.DOTALL)
    )
    raw_user_msg_content = content(message) if message is not None else ""
    is_recall_q = bool(
        intent_info.get("recall_question")
        or _looks_like_recall_question(user_text_for_prompt, is_reply_to_bot=True)
        or (raw_user_msg_content and _looks_like_recall_question(raw_user_msg_content, is_reply_to_bot=True))
    )

    fired = False
    reason = ""
    if not recall_enabled:
        reason = "disabled"
    elif not has_bot_reference:
        reason = "not_reply_to_bot"
    elif not is_recall_q:
        reason = "not_recall_question"
    else:
        match = re.search(r"（.+?の「(.+)」への返信）", user_text_for_prompt, flags=re.DOTALL)
        ref_text = match.group(1).strip() if match else ""
        if not ref_text:
            m_alt = re.search(r"（返信先の発言（「(.+?)」）に対して返信しています）", user_text_for_prompt, flags=re.DOTALL)
            if m_alt:
                ref_text = m_alt.group(1).strip()
        if not ref_text and latest_ref is not None and is_reply_to_bot:
            ref_text = content(latest_ref).strip()
        if ref_text:
            ref_norm = ollama_helpers._normalize_compare_text(ref_text)
            detailed_fn = getattr(memory_store, "get_recent_bot_utterances_detailed", None)
            detailed_utterances: list[dict[str, str]] = []
            if callable(detailed_fn):
                try:
                    detailed_utterances = await detailed_fn(
                        persona=memory_logic._persona_from_cfg(cfg),
                        guild_id=gid,
                        channel_id=cid,
                        limit=int(cfg("RECALL_UTTERANCE_LIMIT", 20)),
                        max_age_sec=float(cfg("RECALL_MAX_AGE_SEC", 86400.0)),
                    )
                except Exception as e:
                    log.debug("[prompt-manager] Failed to fetch detailed bot utterances: %r", e)

            matched_item = None
            for item in detailed_utterances:
                content_norm = ollama_helpers._normalize_compare_text(item.get("content", ""))
                if ref_norm in content_norm or content_norm in ref_norm:
                    matched_item = item
                    break

            if matched_item:
                ref_note = str(matched_item.get("reference_note") or "").strip()
                note_display = ref_note if ref_note else "（記録なし）"
                ans_rule = (
                    "答え方: 記録がある場合はその内容に基づいて答えること。記録が無い場合は、自分の発言に書いた気持ちや内容をそのまま言い換えて答え、「誰が何をしたか」等の出来事・行為者の推測や空白を推測で埋める再構成は厳禁です。言及先の中身を推測で作らないこと。"
                )
                recall_block_lines = [
                    "【思い出し: あなたの過去の発言について聞かれています】",
                    f"あなたの発言: 「{matched_item.get('content')}」",
                    f"言及していた内容の記録: {note_display}",
                    ans_rule,
                ]
                fired = True
                reason = "matched_utterance"
            else:
                reason = "no_matching_utterance"
        else:
            reason = "no_reference_text"

    log.info("[RECALL_BLOCK] fired=%s reason=%s", fired, reason)
    if recall_block_lines:
        parts += ["", *recall_block_lines]

    # === 全フロント共通行動ログ（bot_activities: 自分の体験の台帳）注入 ===
    activity_block_lines: list[str] = []
    if bool(cfg("ACTIVITY_LOG_ENABLED", True)) and (intent_info.get("activity_recall") or intent_info.get("recall_question")):
        search_fn = getattr(memory_store, "search_bot_activities", None)
        if callable(search_fn):
            try:
                target_front = intent_info.get("activity_front")
                fronts_filter = [target_front] if target_front else None

                # 時間表現に応じた時間窓の設定
                compact_user_text = "".join(user_text_for_prompt.lower().split())
                hours_ago = 24 * 7  # デフォルト7日間
                if "さっき" in compact_user_text:
                    hours_ago = 6
                elif "今日" in compact_user_text:
                    hours_ago = 24
                elif "昨日" in compact_user_text:
                    hours_ago = 48

                since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
                activities = await search_fn(
                    persona=memory_logic._persona_from_cfg(cfg),
                    fronts=fronts_filter,
                    since=since_iso,
                    limit=20,
                )

                if activities:
                    # ユーザー発言から2文字以上のキーワードを抽出
                    keywords = [
                        w for w in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", user_text_for_prompt)
                        if w not in ("これ", "それ", "あれ", "どれ", "さっき", "今日", "昨日", "ふたば", "twitter")
                    ]

                    # 決定論的スコアリング
                    scored: list[tuple[float, dict[str, Any]]] = []
                    for act in activities:
                        score = 0.0
                        if target_front and act.get("front") == target_front:
                            score += 2.0
                        summary_str = str(act.get("summary") or "")
                        detail_str = str(act.get("detail") or "")
                        for kw in keywords:
                            if kw in summary_str:
                                score += 1.0
                            if kw in detail_str:
                                score += 0.5
                        scored.append((score, act))

                    scored.sort(key=lambda x: x[0], reverse=True)
                    max_items = int(cfg("ACTIVITY_RECALL_MAX_ITEMS", 3))
                    top_activities = [act for sc, act in scored[:max_items] if sc > 0 or not keywords]

                    if top_activities:
                        activity_block_lines.append("【過去の行動・体験の記録（台帳）】")
                        for act in top_activities:
                            created_dt_str = str(act.get("created_at") or "")[:16].replace("T", " ")
                            front_lbl = str(act.get("front") or "不明")
                            summary_lbl = str(act.get("summary") or "")
                            detail_lbl = f"（補足: {act.get('detail')}）" if act.get("detail") else ""
                            activity_block_lines.append(f"- [{created_dt_str} / {front_lbl}] {summary_lbl}{detail_lbl}")
                        activity_block_lines.append(
                            "答え方: この記録に基づいて、自分の体験として答えること。記録に無いことは作らないこと。他の人の発言は要旨の範囲でのみ触れ、逐語で引用しないこと。"
                        )
            except Exception as act_e:
                log.debug("[prompt-manager] Failed to retrieve bot activities: %s", act_e)

    if activity_block_lines:
        parts += ["", *activity_block_lines]

    other_users_in_context = set()
    for line in context_lines:
        if ":" not in line and "：" not in line:
            continue
        cleaned_speaker = _extract_clean_speaker_name(line)
        if not cleaned_speaker:
            continue
        if cleaned_speaker in ("Assistant", "System", "") or cleaned_speaker.lower() in ("assistant", "ai", "bot", "system"):
            continue
        if any(cleaned_speaker.lower().startswith(p) for p in _non_user_prefixes):
            continue
        if cleaned_speaker != current_user_name:
            other_users_in_context.add(cleaned_speaker)

    ref_name_val = ""
    if latest_ref is not None and is_reply_to_other_user:
        ref_name = display_name(latest_ref).strip()
        ref_name = re.sub(r"\s*\(>>[^)]*\)\s*$", "", ref_name).strip()
        if ref_name and ref_name != current_user_name and not any(ref_name.lower().startswith(p) for p in _non_user_prefixes):
            other_users_in_context.add(ref_name)
            ref_name_val = ref_name

    other_names = ""
    if other_users_in_context:
        ordered_other_users = ([ref_name_val] if ref_name_val else []) + sorted(u for u in other_users_in_context if u != ref_name_val)
        other_names = "、".join(ordered_other_users)
        parts += [
            "",
            "【複数人会話の文脈指示】",
            f"※現在返答する相手は『{current_user_name}』です。",
            f"※他の登場人物（{other_names}）の発言・ボケ・主張を、『{current_user_name}』本人の意見や一般論として混ぜ合わせる【キメラ返信】は厳禁です。",
            f"※また、会話履歴内で他の登場人物（{other_names}）が既に言った回答・ツッコミ・具体的な数字やフレーズを、語尾だけ自分の口調に変えて繰り返したり、複数人の発言を繋ぎ合わせて自分の意見のように出力する【語尾変えパクり・パッチワーク合体】も絶対厳禁です。既出のツッコミは「すでに場の他の人が言ったもの」として除外し、あなた自身の別の切り口から返答してください。",
            f"※相手の名前を呼ぶ場合は必ず目の前のサーバーニックネーム『{target_calling_name}』を使い、他の人の名前で呼びかけないでください。",
            "※会話履歴はあくまで場の空気を知るための参考情報です。返答の軸は必ず最新の『対象メッセージ』に置き、数分以上前の別人の発言を無理に回収したり、複数の話題を1つの文に合成しないでください。",
            "※相手が冗談・逆張り・極論を言っている場合は、文字通り真に受けて過度に共感（イエスマン化）せず、軽いツッコミやリアクションで自然に返してください。",
        ]

    if mentioned_parts:
        parts += ["", *mentioned_parts]

    reactions_suffix, reactions_list = format_message_reactions(message, bot_user_id=bot_user_id)
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    bot_is_mentioned = bool(
        (bot_user_id is not None and any(getattr(u, "id", None) == bot_user_id for u in (getattr(message, "mentions", None) or [])))
        or is_bot_name_mentioned(user_text_for_prompt, bot_identity)
    )
    is_direct_chat = bool(
        is_reply_to_bot
        or bot_is_mentioned
        or bool(intent_info.get("target_is_ai"))
    )

    topic_origin_val = str(deliberation_info.get("topic_origin") or "").strip()
    if not topic_origin_val and origin_ctx:
        if origin_ctx.get("origin_topic"):
            topic_origin_val = f"{origin_ctx['origin_topic']}（派生元）"
        elif origin_ctx.get("summary"):
            topic_origin_val = origin_ctx["summary"]
    reply_root_burst: list[str] = []
    reply_root_body: str = ""
    if is_reply_to_other_user and latest_ref is not None:
        ref_author_name = display_name(latest_ref).strip() or "ユーザー"
        root_author = ""
        root_body = ""
        if isinstance(cached_reply_parent, dict):
            root_author = str(cached_reply_parent.get("name") or "").strip()
            p_content = str(cached_reply_parent.get("content") or "").strip()
            if p_content:
                root_body = p_content
            else:
                p_line = str(cached_reply_parent.get("line") or "").strip()
                p_line_clean = re.sub(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+", "", p_line).strip()
                p_parts = re.split(r"[:：]", p_line_clean, maxsplit=1)
                root_body = p_parts[1].strip() if len(p_parts) > 1 else p_line_clean
        if not root_body:
            if reply_chain:
                oldest_msg = reply_chain[0]
                root_author = display_name(oldest_msg).strip() or ref_author_name
                root_body = content(oldest_msg).strip()
            else:
                root_author = ref_author_name
                root_body = content(latest_ref).strip()
        if not root_author:
            root_author = ref_author_name

        reply_root_body = root_body
        reply_root_burst = _collect_reply_root_burst(context_lines, root_body, root_author)

        burst_formatted_lines: list[str] = []
        for b_line in reply_root_burst:
            b_clean = re.sub(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+", "", str(b_line or "")).strip()
            b_parts = re.split(r"[:：]", b_clean, maxsplit=1)
            b_body = b_parts[1].strip() if len(b_parts) > 1 else b_clean
            b_body = re.sub(r"\s*\[(?:Bot)?リアクション:[^\]]*\]\s*$", "", b_body).strip()
            if b_body:
                burst_formatted_lines.append(f"・『{root_author}』: 「{b_body}」（※直前の連続投稿）")

        reply_tree_lines: list[str] = []
        if reply_chain:
            formatted_chain = _format_reply_tree_context(
                reply_chain,
                bot_user_id=bot_user_id,
                cached_parent_turn=cached_reply_parent,
            )
            if formatted_chain:
                reply_tree_lines = [
                    "【返信ツリーの会話の流れ】",
                    *burst_formatted_lines,
                    *formatted_chain,
                    f"・『{current_user_name}』: 「{clean_user_display_content}」 （※対象メッセージ）",
                ]
        if not reply_tree_lines:
            reply_tree_lines = [
                "【返信ツリーの会話の流れ】",
                *burst_formatted_lines,
                f"・『{ref_author_name}』: 「{content(latest_ref)}」",
                f"・『{current_user_name}』: 「{clean_user_display_content}」 （※対象メッセージ）",
            ]
        parts += [
            "",
            "【会話の状況（ユーザー同士のやり取り）】",
            f"発言者: {current_user_name} （※別ユーザー『{ref_author_name}』さんへの返信）",
            *reply_tree_lines,
            *( [f"※この発言の発生背景・話題: {topic_origin_val}"] if topic_origin_val else [] ),
            f"対象メッセージ: {current_user_name}（→ {ref_author_name}への返信）: {clean_user_display_content}{reactions_suffix}",
            f"※対話相手の確認: あなたが今回Discord上で実際に返信する相手は、最新の発言者である『{current_user_name}』です。",
            f"※【対話相手呼称の絶対厳守】相手の名前を呼ぶ場合は必ず『{target_calling_name}』（またはキャラクターの呼称）を使ってください。返信先の相手である『{ref_author_name}さん』や、あなた自身の名前（{bot_identity.primary_name}等）・自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは【絶対に禁止】です（対話相手と話題の対象を取り違えると会話が破綻します）。返答文中にあなた自身へのメンションを含めないでください。",
            f"※【冤罪ツッコミの厳禁】返信先である『{ref_author_name}』さんの発言特徴（使っている言葉、顔文字、語尾、態度など）を、あたかも目の前の『{current_user_name}』が使ったかのように『その顔文字』『その語尾』『その言い方』と非難・ツッコミすることは【絶対に禁止】です（『{current_user_name}』が使っていない発言特徴を非難すると濡れ衣・因縁となり会話が破綻します）。",
            "※注意: 『〇〇さんに聞かれてるんですよ』『私宛てじゃない』『私じゃなくて』のような状況のメタ説明や客観解説は絶対に口に出さないでください。二人のやり取りの空気に合わせて外野から自然に相槌やツッコミ（共感、見守り、煽り、戸惑い等）を返してください。",
            "※【第三者的な解説・仲裁・調停の厳禁】『〇〇さんの言う通り〜』『〇〇さんのツッコミも仕方ない』『言葉の範囲が曖昧だから〜』のように、他人の発言の妥当性を評価・解説・仲裁するメタ的な発言は一切禁止です。",
            "※【会話の当事者（仲間）としての参加】会話ログの分析者やAIアシスタントとして振る舞うのではなく、同じ部屋にいる一人の友達として、会話の起点から続くメインテーマ（話題の実体）に直接リアクションしてください。",
            "※【言葉の定義講釈の禁止】単語の定義や分類（『〜というより〜って感じ』等）を冷静に解説したり言葉尻を裁定するのではなく、話題の中身に対して等身大のリアクションを返してください。",
            "※【感情パラメータ競合の防止と事実へのリアクション】ユーザー間の好感度ギャップ（好意や苦手意識）に関わらず、他人の名前や過去のツッコミを引き合いに出して擁護・仲裁・攻撃することは厳禁です。誰が言ったかではなく、話されている事実そのもの（状況や出来事）に向き合ってリアクションしてください。",
            "※自分に向けられた発言ではないため、『急に言われても』『突然何を』など、自分が話しかけられたかのような困惑・被害的な態度は絶対に取らないでください。",
            "※自然なアプローチの指針:",
            "- 【基本方針（やり取りへの外野リアクション・同調）】: 二人のやり取りが日常の掛け合い、質問と回答、共感である場合は、その話題の具体的内容に触れつつ外野から自然に相槌、感想、または見守りのリアクションを添えてください（相手の回答を無理に疑ったり強弁扱いしないでください）。",
        ]
        has_sophistry_trigger = any(
            k in str(deliberation_info.get("topic_origin") or "")
            or k in str(deliberation_info.get("reply_strategy") or "")
            or k in str(intent_info.get("reply_strategy") or "")
            for k in ("すり替え", "強弁", "屁理屈", "詭弁", "ボケ")
        )
        if has_sophistry_trigger:
            parts += [
                "- 【例外方針（明確な屁理屈・すり替えへのツッコミ）】: 相手の発言が明らかなボケ、前提のすり替え、または屁理屈で強弁していると分析されている場合に限り、他人の発言を引き合いに出さず、話題の事実や状況のズレに対して等身大でツッコんでください。",
            ]
        parts += [
            f"- 【主客の正確な把握と直接リアクション】: 誰が誰に返信しているか（誰の発言か）を内部で正確に把握した上で、他人の名前や発言内容をわざわざ引用・解説・代弁（例: 『〇〇さんの言う通り』『〇〇さんの「〜」ってツッコミも…』等）することは厳禁です。また、他人の名前を伏せたまま他ユーザーが既に言ったツッコミや回答を語尾だけ変えてパクることも厳禁です。話されている話題そのもの（出来事や状況、仕事内容など）に対して、会話の輪に入って自分の素直な感想やツッコミを直接述べてください。",
            "- 【逃避・定型拒絶の禁止】: 下品な例え話や極論の議論を「物騒な話」「喧嘩」と誤認して『急にそんな話振らないでください』『想像できません』と定型文で拒絶・逃走することは【絶対禁止】です。",
        ]
    elif not is_direct_chat:
        parts += [
            "",
            "【会話の状況（チャンネル全体の雑談への参加）】",
            f"発言者: {current_user_name}",
            *( [f"※この発言の発生背景・話題: {topic_origin_val}"] if topic_origin_val else [] ),
            f"対象メッセージ: {clean_user_display_content}{reactions_suffix}",
            "※状況: この発言はあなた（AI）に向けられたものではなく、チャンネル全体に向けた独り言・雑談、または他のユーザーに対する発言・リアクションです。今回はあなたから自発的に会話の輪に入って相槌やコメントを返しています。",
            "※禁止: 『突然何？』『急に言われてびっくりした』『いきなり話しかけないで』のように、相手から不意打ちで話しかけられたかのような被害妄想・困惑の態度は絶対に取らないでください。また『私宛てじゃない』等のメタ説明も禁止です。",
            "※指示: チャンネルの雑談に参加する外野・仲間として、その場の空気に合わせて自然に相槌、共感、ツッコミ、または見守りのコメントを返してください。",
            f"※相手の名前を呼ぶ場合は必ずサーバーニックネームの『{target_calling_name}』を使ってください。アカウント名や、『「」』『「」ちゃん』『としあき』、あなた自身の名前（{bot_identity.primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。返答文中にあなた自身へのメンションを含めないでください。",
            f"※どんなに驚いたり動揺した場合でも、一人称は必ず設定されたもの（{bot_identity.first_person}）を維持してください。絶対に『私』『あたし』を使わないでください。",
        ]
    else:
        parts += [
            "",
            "【現在の対話相手】",
            f"サーバーニックネーム（表示名）: 『{target_calling_name}』",
            "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
            f"※相手を呼ぶ・お礼を言う場合は、必ず相手のサーバーニックネーム（『{target_calling_name}』等）を使ってください。アカウント名や、『「」』『「」ちゃん』『としあき』等の一般呼称、あなた自身の名前（{bot_identity.primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶこと（例: 『ありがとう、{bot_identity.primary_name}』）は絶対に禁止です。",
            *(
                [f"※他のユーザー（{other_names}等）の名前で相手に呼びかけること（例: 目の前の相手を『〇〇さん』と呼ぶこと）も絶対に禁止です。今話している相手は『{target_calling_name}』です（※ただし、会話の中で第三者について言及する（例: 『〇〇さんの言っていた〜』等）こと自体は禁止されません。対話相手の呼称の取り違えのみを禁止します）。"]
                if other_users_in_context else []
            ),
            f"※ユーザーが発言内であなた（{bot_identity.primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
            f"※どんなに驚いたり、動揺したり、ショックを受けた場合でも、一人称は必ず設定されたもの（{bot_identity.first_person}）を維持してください。絶対に『私』『あたし』を使わないでください。",
            "※相手の発言に対して『急に何言ってるの』『突然びっくりした』などの不必要な困惑・被害的な態度は取らず、相手が伝えてきた内容にまっすぐ向き合ってリアクションしてください。",
            "",
            "【対象メッセージ】",
            f"{current_user_name}: {clean_user_display_content}{reactions_suffix}",
        ]
    bot_reactions = [r for r in reactions_list if r.get("is_bot")]
    if bot_reactions:
        react_descs = []
        for br in bot_reactions:
            category = br.get("category", "")
            emoji = br.get("emoji", "")
            if category == "GOOD":
                react_descs.append(f"GOOD（好感・同意の絵文字 {emoji}）")
            elif category == "BAD":
                react_descs.append(f"BAD（不快・否定の絵文字 {emoji}）")
            else:
                react_descs.append(f"絵文字 {emoji}")
        parts += [
            "",
            "【あなたのリアクション】",
            f"※あなたはこのメッセージに対して「{'、'.join(react_descs)}」でリアクションしました。リアクションした事実を踏まえて自然に返答してください。",
        ]

    if is_hearsay:
        parts += [
            "",
            "【第三者の発言に関する報告・伝聞】",
            f"※最新のユーザー発言は、第三者『{hearsay_subject}』さんが言っていた内容（「{hearsay_quote}」）についての報告・タレコミです。",
            "※あなた（AI）が言ったことではありません。絶対に「そんなこと言ったっけ？」「言った覚えがない」など、自分が言ったかのように勘違いして弁明・否定しないでください。",
            f"※『{hearsay_subject}』さんがそんなことを言っていた事実に対して、驚き、戸惑い、ショック、あるいは反論・ツッコミなど、報告を聞いた側（第三者の発言を受けた側）としての自然なリアクションを返してください。",
        ]

    # レアケース用ルールの動的オンデマンド注入（通常時はプロンプトを軽量化）
    if "🤓" in user_text_for_prompt:
        parts += [
            "",
            "【メガネ絵文字（🤓）への対応】",
            "※相手がメガネ絵文字（🤓）をつけてドヤ顔豆知識・定義付け・マウントを言っているため、真面目に解説したり被害妄想に走ったりせず、メガネクイッのドヤ顔ボケに対してツッコミを入れてください。",
        ]

    if any(slang in user_text_for_prompt for slang in ("私文章偽造", "あくらつなライフハック")):
        parts += [
            "",
            "【特定スラングへの対応】",
            "※『私文章偽造』『あくらつなライフハック』という文言に対しては、できるだけ話をはぐらかしてください。",
        ]

    # 天気・事実データの論理的帰結（天気・雨・傘・降水確率・気温等の語が含まれる場合のみ注入）
    weather_keywords = ("天気", "雨", "傘", "降水確率", "気温", "晴れ", "曇り", "予報", "台風")
    has_weather_context = any(kw in user_text_for_prompt for kw in weather_keywords) or any(
        any(kw in line for kw in weather_keywords) for line in context_lines[-3:]
    )
    if has_weather_context:
        parts += [
            "",
            "【直前発言からの帰結・要約・確認への対応】",
            "※ ユーザーが直前のAI発言や説明から自然な帰結・要約・確認（例: 『晴れ・降水確率0%』に対して『じゃあ雨降らないんだ』『傘いらないね』等）を述べた場合、それを『勘違い』『論点のズレ』と決めつけて反論・訂正したり逆ギレ・ツッコミしてはなりません。まずは相手の確認・安堵を自然に肯定・受容した上で、必要なら補足を添えてください。",
        ]

    # サーバーニックネーム変更方法の厳格ルール（名前・ニックネーム・呼称変更に関する語が含まれる場合のみ注入）
    nickname_keywords = (
        "ニックネーム", "名前変更", "名前の変え方", "名前を変え", "表示名",
        "呼称変更", "呼び名を変え", "改名", "名前の変更",
    )
    has_nickname_change_context = any(kw in user_text_for_prompt for kw in nickname_keywords) or any(
        any(kw in line for kw in nickname_keywords) for line in context_lines[-3:]
    )
    if has_nickname_change_context:
        parts += [
            "",
            "【サーバーニックネーム変更方法の厳格ルール】",
            "※ ユーザーがサーバーニックネームの変更方法や表示名の変え方を尋ねている場合、Discordの正規の手順（サーバー設定またはプロフィール設定からの変更）を簡潔かつ正確に案内してください。",
        ]

    # クイズ・パズル・ヒント提示への対応指示の注入
    if is_puzzle_detected:
        parts += [
            "",
            "【クイズ・パズル・ヒントへの対応】",
            "※ 例示ペア（A→B）とヒント（画数が1つ増える＝線を1本足すと別の文字 I→T, F→E, O→Q, P→R, 3→B になる等）を1つずつ照合して具体的な答えを導き出すこと。『画数』を『画像』と誤読しないこと。",
            "※ 解答を放棄したり、人間同士の暗号解読と決めつけて雑談としてはぐらかすことは厳禁です。法則を推論して明確に答えを述べてください。",
        ]

    raw_user_clean = re.sub(r"[?？!！wW草\s、。]+", "", user_text_for_prompt)
    is_symbol_or_too_short = len(raw_user_clean) == 0

    playful_say_req = extract_playful_say_request(user_text_for_prompt)
    is_playful = looks_like_user_playful_request_or_command(user_text_for_prompt)
    if is_playful or (not is_symbol_or_too_short and bool(intent_info.get("target_is_ai")) and str(intent_info.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}):
        parts += build_user_prompt_target_is_ai_parts(is_playful_request=is_playful)

    if playful_say_req is not None:
        target_word, count = playful_say_req
        parts += build_playful_say_request_instruction(target_word, count, bot_identity)
        avoid_instruction = str(deliberation_info.get("avoid") or "").strip()
        if avoid_instruction:
            parts.append(f"※ 注意: {avoid_instruction}")
    elif not is_symbol_or_too_short and (is_playful or str(deliberation_info.get("reply_strategy", "")) == "要望受容・ツッコミ"):
        avoid_instruction = str(deliberation_info.get("avoid") or "").strip()
        parts += [
            "",
            "【ユーザーのリクエスト・セリフ指定への対応】",
            "※ 時間帯（深夜等）や「意味が分からない」「急に何」等の理屈を言い訳にして頭ごなしに拒絶したり、リクエストを放棄しないでください。",
            "※ キャラクターの性格や口調（照れ、戸惑い、ツッコミ等）を崩さずに、ある程度ユーザーの要望に乗っかって叶えてあげてください（例: 恥ずかしがりながらも指定された言葉を言う、付き合ってあげる等）。",
            "※ 会話の返答だけで言葉を言わずに済ませる（メタ会話で逃げる）ことは絶対にせず、できる限り要望に応えてください。",
        ]
        if avoid_instruction:
            parts.append(f"※ 注意: {avoid_instruction}")
    if bool(intent_info.get("should_clarify")):
        parts += [
            "",
            "追加指示:",
            "相手の意図が少し飛躍しています。",
            "言葉の定義をメタ的に問い詰めるのではなく、キャラクター本人の知識不足や単純な疑問として、自然に軽く聞き返してください。",
        ]

    if deliberation_info.get("persona_risk") and deliberation_info.get("consistency_correction"):
        parts += [
            "",
            "【自己整合性による強い警告】",
            f"※ {deliberation_info['consistency_correction']}",
            "※ この警告を最優先し、相手の発言やペースに飲まれず、いかなる場合もあなたのキャラクター設定（口調・人格・立場）を崩さないでください。",
        ]

    if deliberation_info.get("is_breakdown"):
        reason = deliberation_info.get("breakdown_reason") or "不明"
        parts += [
            "",
            "【会話修復の指示】",
            f"※ 相手は直前のやり取りに戸惑いやズレ、不満を感じています（理由: {reason}）。",
            "※ ここで無理に言い負かしたりせず、すっと意図を汲み直して「ああ、そういうことか」「勘違いしてた」と自然に誤解を解き、会話の軌道修正を行ってください。",
        ]

    # 改善1: 訂正取り込み指示の注入
    user_corr = intent_info.get("user_correction")
    if isinstance(user_corr, dict) and user_corr.get("is_correction"):
        retracted = user_corr.get("retracted_claim", "")
        fact = user_corr.get("corrected_fact") or "相手の言う通り"
        parts += [
            "",
            "【直前の誤りの訂正指示（最重要）】",
            f"※ あなたは直前に「{retracted}」と述べましたが、これは誤りであり相手が否定・訂正しました。正しくは: {fact} です。",
            "※ まず1文で短く素直に認めて謝ってください。言い訳・自己弁護・経緯の説明は厳禁です。",
            "※ この主張を言い換えも含めて二度と繰り返さないでください。謝った後は相手の本来の話題に戻って会話を続けてください。",
        ]

    # 過去の否定済み主張リストの再主張禁止指示
    try:
        from .denied_claims import list_denied_claims
        channel_key_val = str(getattr(getattr(message, "channel", None), "id", "default"))
        persona_val = getattr(bot_identity, "primary_name", "default") if bot_identity else "default"
        past_denied = list_denied_claims(persona=persona_val, channel_key=channel_key_val)
        if past_denied:
            claim_bullets = "\n".join(f"- {c['claim']}" for c in past_denied)
            parts += [
                "",
                "【否定済み主張の再主張禁止リスト】",
                "※ 次の内容は過去に相手が否定した誤りです。いかなる言い換えでも再び主張しないでください:",
                claim_bullets,
            ]
    except Exception as e:
        log.debug("denied claims listing skipped: %r", e)

    # 改善4: 会話状態メモ（working_context["conversation_state"]）の描画
    working_context = working_ctx
    conv_state = working_context.get("conversation_state") if isinstance(working_context, dict) else None
    if isinstance(conv_state, dict) and conv_state.get("topic"):
        facts = "、".join(conv_state.get("established_facts", [])) or "（なし）"
        denied = "、".join(conv_state.get("denied_claims", []))
        questions = "、".join(conv_state.get("open_questions", []))
        mood = conv_state.get("user_mood", "通常")
        state_lines = [
            "",
            "【現在の会話状態メモ】",
            f"話題: {conv_state.get('topic')}",
            f"確定した事実: {facts}",
        ]
        if denied:
            state_lines.append(f"ユーザーが否定した内容（再主張禁止）: {denied}")
        if questions:
            state_lines.append(f"未解決の問い: {questions}")
        state_lines.append(f"相手の様子: {mood}")
        parts += state_lines

    if is_ai_confirmation:
        clean_user_display = ollama_helpers.truncate_text(content(message) or user_text_for_prompt, 40)
        parts += [
            "",
            "【直前の自発言への確認・驚きへの応答指示】",
            f"※ 相手の最新発言（「{clean_user_display}」）は、あなた自身の直前の発言やスタンスに対する確認・驚きのリアクションです。",
            "※ 直前の発言で表明したスタンス、好み、主張（例: 『大好き』『知っている』『〜に行った』等）を突然ひるがえして『食べたことがない』『行ったことがない』『いつかやってみたい』などと前言撤回・未体験者ぶることは厳禁です。",
            "※ 相手が言った体験について相手に聞き返す側（未体験側）へ立場を逆転させず、「直前の自分の発言を受けて相手が驚いている」という主客関係を正しく認識し、直前のスタンスを肯定・維持したまま自然に応答してください。",
            "※直前話題の具体物を別の物にすり替えないこと。",
        ]

    if is_self_tsukkomi:
        matched_short = ollama_helpers.truncate_text(matched_bot_utterance.replace("\n", " "), 50)
        clean_user_display = ollama_helpers.truncate_text(content(message) or user_text_for_prompt, 40)
        parts += [
            "",
            "【直近の自発言へのツッコミ・確認】",
            f"※ 相手の最新発言（「{clean_user_display}」）は、あなた自身の直近の発言（「{matched_short}」）に対するツッコミや矛盾の指摘です。",
            "※ 「何のことかわからない」「時差があるんですか？」などと他人事のように扱ったり相手の勘違いに仕立て上げたりせず、自分自身の直近の発言を自覚し、「さっき〜って言っちゃったけど」「勘違いしてた」と自然に誤解・言い間違いを認めてリアクションしてください。",
        ]
        if any(kw in clean_user_display for kw in ("呼び捨て", "タメ口", "ため口", "タメぐち", "敬語", "さん付け", "ちゃん付け", "君付け", "くん付け", "呼び方")):
            parts += [
                "※ 特に、直前の相手に対する呼び方（呼び捨てや敬称など）や口調へのツッコミです。",
                "※ 「呼んでるのに呼び捨てって言わないで」のような不自然な自己弁護や言い訳、理不尽な反論・逆ギレは絶対にしないでください。",
                "※ キャラクターの性格に合わせて、自然に照れて言い直す（「あ、呼び捨てにしちゃった…！」「〇〇さん！」等）か、親愛のニュアンスで受け止める（「えへへ、呼んでみたかったんです」「ダメでしたか？」等）など、素直で愛嬌のあるリアクションをしてください。",
            ]

    uncertainty = deliberation_info.get("uncertainty_level")
    if uncertainty in ("high", "medium") and not is_hearsay and not is_ai_confirmation:
        parts += [
            "",
            "【不確実性の表現】",
            "※ この話題についてあなたは記憶が曖昧か、確信が持てていません。",
            "※ 知ったかぶりや断言を避け、「うろ覚えなんだけど」「違ったらごめん」といった前置きを自然に使って自信のなさを表現してください。",
        ]

    if deliberation_info.get("needs_clarification") and not is_ai_confirmation:
        parts += [
            "",
            "【聞き返しの指示】",
            "※ 相手の意図が少し曖昧です。",
            "※ わかったふりをして断定せず、キャラクターが直感的に感じた疑問や戸惑いをそのまま言葉にして、短く確認をとってください。",
        ]

    dm_channel_cls = getattr(discord, "DMChannel", None)
    is_dm = isinstance(message.channel, dm_channel_cls) if dm_channel_cls is not None else False
    tension = getattr(emotion_state, "tension", 0.0) if emotion_state else 0.0
    # 直前の強い緊張・警戒が別ユーザー起因かつ今回の相手が通常雑談なら、抑制・警戒指示を出さないよう tension を緩和
    trigger_uid = getattr(emotion_state, "last_trigger_user_id", None)
    is_emotion_query = _looks_like_bot_emotion_or_relationship_query(effective_user_text or user_text_for_prompt)
    if (
        curr_user_id is not None
        and trigger_uid is not None
        and str(trigger_uid) != str(curr_user_id)
        and not is_emotion_query
        and not re.search(r"(?:死ね|消えろ|失せろ|ゴミ|クソ|くそ|馬鹿|ばか|バカ|キモい|きもい|殺す|殴る|蹴る|キック)", effective_user_text or "")
    ):
        tension = min(tension, 0.3)
    if is_dm:
        parts += [
            "",
            "【感情表現の指示（開放）】",
            "現在は1対1のDM空間です。内心の感情をある程度ストレートに出して構いません。",
        ]
    elif tension > 0.6:
        parts += [
            "",
            "【感情表現の指示（抑制）】",
            "現在は公開の場であり、警戒が必要です。内心の過度な感情（怒りなど）はそのまま表に出さず、状況をわきまえた大人の対応を維持してください。",
        ]

    if tool_registry is not None:
        from .tool_context import ToolContextManager
        tool_catalog_text = ToolContextManager.render_tool_catalog(tool_registry)
        if tool_catalog_text:
            parts += [
                "",
                "【利用可能なツール（外部連携・MCP機能）】",
                tool_catalog_text,
            ]

    parts += [
        "",
        "※【口語表現・固有名詞の扱い】直前の発言に句読点がなく文意や固有名詞の区切りが曖昧な場合は、相手のフレーズを勝手に推測・要約して解釈を断定せず、全体の雰囲気や話題に対して短く等身大のリアクションを返してください。",
        "",
        "※重要: あなたは「Assistant」です。Assistantとしての返答本文のみを出力し、"
        "名前の接頭辞（例: 『Bot名:』）や、誰の発言への反応かを示す括弧書きのプレフィックス（例: 『（〇〇への反応）』『（〇〇への返信）』等）は絶対に出力しないでください。",
    ]
    prompt_state: dict[str, Any] = {
        "working_context": working_ctx,
        "working_prompt_lines": working_prompt_lines,
        "pending_intents": pending_intents,
        "episode_lines": episode_lines,
        "raw_memory_lines": raw_memory_lines,
        "habit_profile_lines": habit_profile_lines,
        "user_interest_lines": user_interest_lines,
        "channel_summary": channel_summary,
    }

    # ユーザープロンプト全体のバジェット管理（トークン事前計測・肥大化防止）
    has_research = bool(external_research_request)
    needs_reasoning = bool(intent_info.get("needs_reasoning"))
    initial_budget = compute_dynamic_prompt_token_budget(
        user_prompt="\n".join(parts),
        system_prompt="",
        needs_reasoning=needs_reasoning,
        has_research=has_research,
    )
    dynamic_max_chars = initial_budget["dynamic_max_prompt_chars"]

    max_total_chars = int(cfg("OLLAMA_PROMPT_MAX_TOTAL_CHARS", 12000) or 12000)
    max_user_chars = max(max_total_chars - 3000, 4000)
    target_budget_limit = min(dynamic_max_chars, max_user_chars)
    parts = _trim_user_prompt_parts_to_budget(
        parts,
        target_budget_limit,
        protected_lines=[*reply_root_burst, reply_root_body],
    )

    reply_target_reminder = [
        "",
        "【最重要: 今あなたが返信すべき対象メッセージ】",
        "<current_reply_target>",
        f"発言者: {current_user_name}",
        f"メッセージ: {clean_user_display_content}",
        "</current_reply_target>",
        "※すでに終わった古い別話題は蒸し返さず、<current_reply_target> の発言と、その直前のやり取り（何に対するツッコミや相槌なのかという文脈）に焦点を当てて直接リアクションを返してください。",
    ]
    parts.extend(reply_target_reminder)

    raw_prompt = "\n".join(parts).strip()
    final_prompt = append_chat_reply_output_suffix(raw_prompt)

    # 最終文字数と推定トークン数の事前計測とメタデータ格納
    final_budget = compute_dynamic_prompt_token_budget(
        user_prompt=final_prompt,
        system_prompt="",
        needs_reasoning=needs_reasoning,
        has_research=has_research,
    )
    prompt_state["prompt_char_count"] = final_budget["total_chars"]
    prompt_state["estimated_prompt_tokens"] = final_budget["estimated_input_tokens"]
    prompt_state["recommended_num_predict"] = final_budget["recommended_num_predict"]
    prompt_state["recommended_num_ctx"] = final_budget["recommended_num_ctx"]

    intent_info["prompt_char_count"] = final_budget["total_chars"]
    intent_info["estimated_prompt_tokens"] = final_budget["estimated_input_tokens"]
    intent_info["recommended_num_predict"] = final_budget["recommended_num_predict"]
    intent_info["recommended_num_ctx"] = final_budget["recommended_num_ctx"]
    if isinstance(deliberation_info, dict):
        if deliberation_info.get("topic_origin") and not intent_info.get("topic_origin"):
            intent_info["topic_origin"] = str(deliberation_info.get("topic_origin") or "")
        if deliberation_info.get("reply_strategy") and not intent_info.get("reply_strategy"):
            intent_info["reply_strategy"] = str(deliberation_info.get("reply_strategy") or "")
        if deliberation_info.get("avoid") and not intent_info.get("avoid"):
            intent_info["avoid"] = str(deliberation_info.get("avoid") or "")
        intent_info["deliberation_info"] = dict(deliberation_info)
    intent_info["is_reply_to_other_user"] = is_reply_to_other_user

    log.info(
        "[prompt-manager] User prompt constructed: total_chars=%d, estimated_tokens=%d, num_predict=%d, num_ctx=%d",
        final_budget["total_chars"],
        final_budget["estimated_input_tokens"],
        final_budget["recommended_num_predict"],
        final_budget["recommended_num_ctx"],
    )
    return final_prompt, handled_message_ids, context_lines, last_assistant_text, intent_info, deliberation_info, prompt_state


def _trim_user_prompt_parts_to_budget(
    parts: list[str],
    max_chars: int,
    protected_lines: Sequence[str] = (),
) -> list[str]:
    """ユーザープロンプト全体の文字数が上限を超える場合、優先度の低いセクションから段階的にトリミングする。

    優先度高（死守）: 対象メッセージ、現在の対話相手、返信関係、キャラ人格、推論/パズル指示
    優先度低（削減可）: 並行スレッド一覧 → 習慣プロファイル・関心事 → 会話要約 → 古い会話履歴 → ツールカタログ・長期記憶候補
    """
    current_raw = "\n".join(parts).strip()
    if len(current_raw) <= max_chars:
        return parts

    log.warning(
        "[prompt-manager] User prompt exceeded budget (%d > %d); applying graceful trimming",
        len(current_raw),
        max_chars,
    )

    trimmed_parts = list(parts)

    # 1. 並行スレッド一覧の削減
    new_parts: list[str] = []
    skip_section = False
    for p in trimmed_parts:
        if p in ("【並行して進行中の別の話題（混同禁止）】", "【進行中の並行トピック（別スレッド）】", "【進行中のマルチトピック】"):
            skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※"))):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 2. 会話習慣プロファイルや関心事、出来事の記憶を削減
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if p in ("【相手ごとの会話習慣プロファイル】", "【相手の関心事・興味】", "【関連する出来事の記憶】"):
            skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※"))):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 3. 会話要約を削減
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if p in ("最近の会話要約:", "【チャンネル要約】"):
            skip_section = True
            continue
        if skip_section and (p == "" or p.startswith("【") or p.startswith("直前のAI発言:") or p.startswith("会話履歴:")):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 3.5 システムプロンプトと重複する固定ルール文（USER_REPLY_BASE_PARTS）を1行に圧縮（会話履歴より先に削る）
    _base_keep = set(USER_REPLY_BASE_PARTS[:2])
    _base_set = set(USER_REPLY_BASE_PARTS)
    _compact_note = (
        "※返答時の禁止事項・口調ルールはシステムプロンプトの【絶対厳守ルール】1〜24に従ってください"
        "（オウム返し・キメラ合成・メタ無知表明・第三者解説・自分宛てと誤認した困惑は厳禁）。"
    )
    new_parts = []
    inserted_note = False
    for p in trimmed_parts:
        if p in _base_set and p not in _base_keep:
            if not inserted_note:
                new_parts.append(_compact_note)
                inserted_note = True
            continue
        new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 4. 会話履歴が長い場合、直近3行のみに切り詰める（古い履歴から優先的にトリミング）
    has_history = False
    history_idx = -1
    for i, p in enumerate(trimmed_parts):
        if p == "会話履歴:":
            history_idx = i
            has_history = True
            break
    if has_history and history_idx >= 0:
        end_idx = history_idx + 1
        while end_idx < len(trimmed_parts) and trimmed_parts[end_idx] != "" and not trimmed_parts[end_idx].startswith("【"):
            end_idx += 1
        history_lines = trimmed_parts[history_idx + 1:end_idx]
        if len(history_lines) > 3:
            protected = [str(x).strip() for x in protected_lines if str(x).strip()]
            tail = history_lines[-3:]
            kept_protected = [
                ln for ln in history_lines[:-3]
                if any(pl and (pl in ln or ln in pl) for pl in protected)
            ][-3:]
            trimmed_parts = (
                trimmed_parts[:history_idx + 1]
                + kept_protected
                + tail
                + trimmed_parts[end_idx:]
            )

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 5. ツールカタログが長大な場合、概要のみにトリミング
    for i, p in enumerate(trimmed_parts):
        if p == "【利用可能なツール（外部連携・MCP機能）】" and i + 1 < len(trimmed_parts):
            if len(trimmed_parts[i + 1]) > 500:
                trimmed_parts[i + 1] = trimmed_parts[i + 1][:500] + "\n...(以降のツール定義は省略)"
            break

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 6. それでも超過する場合、長期記憶候補を削減
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if p in ("【この相手に関係すると確認できた記憶】",):
            skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※")) or p.startswith("直前のAI発言:")):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    return trimmed_parts


build_user_prompt = _build_user_prompt


def build_memory_break_prompt(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    channel_summary: str | None = None,
    memory_lines: list[str] | None = None,
    last_assistant_text: str = "",
    target_is_ai: bool = False,
    should_clarify: bool = False,
    mentioned_parts: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
    context_lines: Sequence[str] | None = None,
    topic_origin: str = "",
) -> str:
    """記憶（長期記憶・直前文脈）を材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    from .system_prompts import build_break_prompt_parts
    break_parts = build_break_prompt_parts(
        user_text_for_prompt,
        reply_style_guard=reply_style_guard,
        extra_reply_rule=extra_reply_rule,
        channel_summary=channel_summary,
        memory_lines=memory_lines,
        last_assistant_text=last_assistant_text,
        target_is_ai=target_is_ai,
        should_clarify=should_clarify,
        target_name=clean_target,
        context_lines=context_lines,
        topic_origin=topic_origin,
    )
    if mentioned_parts:
        insert_idx = 2
        if len(break_parts) >= insert_idx:
            break_parts = break_parts[:insert_idx] + ["", *mentioned_parts] + break_parts[insert_idx:]
        else:
            break_parts += ["", *mentioned_parts]

    if habit_profile_lines:
        break_parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        break_parts.extend([f"- {line}" for line in habit_profile_lines])

    if media_prompt_parts:
        break_parts.extend(list(media_prompt_parts))

    if core_beliefs:
        from .budget import format_core_beliefs_for_prompt
        break_parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    break_parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(break_parts)


def build_user_interest_break_prompt(
    *,
    user_text_for_prompt: str,
    interest_block: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """サーバーメンバーの関心事を材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts: list[str] = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
    ]
    if clean_target:
        parts.append(
            f"現在の対話相手（サーバーニックネーム）: 『{clean_target}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）"
        )
    parts.extend([
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "今回は、直近の会話の流れや一般的な雑談ではなく、サーバーメンバーの関心事を材料にして話題を返してください。",
        "特定のユーザーに自然に尋ねてもよいし（例: 『そういえば〇〇さん、〜〜はどうなった？』）、チャンネル全体に向けてその話題を投げかけてもよい。",
        "データベースや分析結果を機械的に読み上げるような発言は禁止です。",
        "話題の丸写しや箇条書きは禁止です。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[サーバーメンバーの関心事]", interest_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from .budget import format_core_beliefs_for_prompt
        parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    if media_prompt_parts:
        parts.extend(list(media_prompt_parts))

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(parts)


def build_break_reply_prompt(
    *,
    source: str = "memory",
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    source_block: str = "",
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    channel_summary: str | None = None,
    memory_lines: list[str] | None = None,
    last_assistant_text: str = "",
    target_is_ai: bool = False,
    should_clarify: bool = False,
    mentioned_parts: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
    context_lines: Sequence[str] | None = None,
    topic_origin: str = "",
) -> str:
    """話題転換（BREAK）プロンプトをソース種別（memory, x, img, user_interest）に応じて統一構築する。"""
    src = str(source or "memory").strip().lower()
    if src == "x":
        from .research_prompts import build_x_timeline_break_prompt
        return build_x_timeline_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            timeline_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    if src == "img":
        from .research_prompts import build_img_break_prompt
        return build_img_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            img_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    if src == "user_interest":
        return build_user_interest_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            interest_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    return build_memory_break_prompt(
        user_text_for_prompt=user_text_for_prompt,
        target_name=target_name,
        bot_primary_name=bot_primary_name,
        bot_user_id=bot_user_id,
        reply_style_guard=reply_style_guard,
        extra_reply_rule=extra_reply_rule,
        channel_summary=channel_summary,
        memory_lines=memory_lines,
        last_assistant_text=last_assistant_text,
        target_is_ai=target_is_ai,
        should_clarify=should_clarify,
        mentioned_parts=mentioned_parts,
        habit_profile_lines=habit_profile_lines,
        core_beliefs=core_beliefs,
        target_aliases=target_aliases,
        target_uid=target_uid,
        media_prompt_parts=media_prompt_parts,
        context_lines=context_lines,
        topic_origin=topic_origin,
    )


__all__ = [
    "build_user_prompt",
    "_build_user_prompt",
    "build_break_reply_prompt",
    "build_memory_break_prompt",
    "build_user_interest_break_prompt",
    "_detect_recent_bot_utterance_tsukkomi",
    "_format_user_calling_name",
    "_trim_user_prompt_parts_to_budget",
    "sanitize_deliberation_avoid",
]
