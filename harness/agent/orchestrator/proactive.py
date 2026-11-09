"""AIエージェント・ハーネス オーケストレーター 自発的発話制御モジュール (orchestrator/proactive.py)

自発的な話題提供（Xタイムライン、ふたばIMG掲示板、ユーザー関心事、記憶）、プロンプト構築、送信処理を担当するMixin。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import re
import time
from collections import deque
from typing import Any, Literal, TYPE_CHECKING

import discord


async def _safe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val

from lib.config_utils import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
    cfg_primary_channel_id,
    model_supports_thinking,
)
from lib.date_utils import is_in_quiet_window, now_jst
from lib.text_utils import normalize_compare_text as _normalize_compare_text
from ollama_bot.common.discord_helpers import append_context_message
from ollama_bot.common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
    truncate_text,
)
from harness.agent.prompt_parser.parser.reply_helpers import _format_reply
from harness.agent.prompt_parser.prompt_manager.retry_prompts import (
    append_chat_reply_output_suffix,
    build_output_only_retry_prompt,
)
from harness.agent.prompt_parser.prompt_manager.system_prompts import (
    build_topic_system_prompt,
)
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import format_user_interests_for_prompt
from harness.agent.tool_registry.web_research.dispatch import research_dispatch

log = logging.getLogger("harness.agent.orchestrator.proactive")

SpontaneousSource = Literal["memory", "x", "img", "user_interest"]


def _extract_semantic_tokens(text: str) -> set[str]:
    """意味を表す概念トークン（個々の漢字、カタカナ語、英数字語）を抽出する。"""
    kanji = set(re.findall(r"[\u4e00-\u9fff]", text))
    katakana = set(re.findall(r"[ァ-ヶー]{2,}", text))
    alnum = set(re.findall(r"[a-zA-Z0-9]{2,}", text.lower()))
    return kanji | katakana | alnum


def _extract_compound_words(text: str) -> set[str]:
    """2文字以上の実質語（漢字熟語、カタカナ語、英数字語）を抽出する。"""
    return set(re.findall(r"[\u4e00-\u9fff]{2,}|[ァ-ヶー]{2,}|[a-zA-Z0-9]{2,}", text.lower()))


def calculate_utterance_similarity(text1: str, text2: str) -> float:
    """2つの発話テキスト間の意味的・語句的類似度（0.0〜1.0）を算出する。

    BAN-94対応: 直近のBot自身の発言と酷似した話題の再発話（鍋物・独白重複等）を検知する。
    """
    t1 = str(text1 or "").strip()
    t2 = str(text2 or "").strip()
    if not t1 or not t2:
        return 0.0

    n1 = _normalize_compare_text(t1)
    n2 = _normalize_compare_text(t2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0

    # 包含判定（短い方が8文字以上で丸ごと含まれる）
    if len(n1) >= 8 and len(n2) >= 8:
        if n1 in n2 or n2 in n1:
            return 0.95

    # 文字 bi-gram による Dice係数 & Overlap係数
    bg1 = {n1[i : i + 2] for i in range(len(n1) - 1)} if len(n1) >= 2 else {n1}
    bg2 = {n2[i : i + 2] for i in range(len(n2) - 1)} if len(n2) >= 2 else {n2}
    common_bg = bg1 & bg2
    dice = (2.0 * len(common_bg)) / (len(bg1) + len(bg2)) if (bg1 and bg2) else 0.0
    overlap = len(common_bg) / min(len(bg1), len(bg2)) if (bg1 and bg2) else 0.0

    # 概念トークン（表意文字の漢字、カタカナ語、英数字語）による一致率
    tk1 = _extract_semantic_tokens(t1)
    tk2 = _extract_semantic_tokens(t2)
    common_tk = tk1 & tk2
    tk_overlap = len(common_tk) / min(len(tk1), len(tk2)) if (tk1 and tk2) else 0.0

    # 熟語キーワード（2文字以上の漢字・カタカナ・英数字）
    kw1 = _extract_compound_words(t1)
    kw2 = _extract_compound_words(t2)
    common_kw = kw1 & kw2
    kw_overlap = len(common_kw) / min(len(kw1), len(kw2)) if (kw1 and kw2) else 0.0

    score = dice
    if len(common_bg) >= 6 and overlap > score:
        score = max(score, overlap)
    if len(common_tk) >= 2 and tk_overlap >= 0.5:
        if dice >= 0.15 or tk_overlap >= 0.60:
            score = max(score, tk_overlap)
    if common_kw:
        if kw_overlap >= 0.5 and (dice >= 0.15 or tk_overlap >= 0.5):
            score = max(score, max(kw_overlap, 0.60))
        elif len(common_kw) >= 2:
            score = max(score, max(kw_overlap, 0.65))
    return score


def _build_topic_prompt() -> str:
    base = cfg("OLLAMA_TOPIC_USER_PROMPT", "雑談チャンネルに投げる、自然で短い話題を1つ作ってください。")
    guard = (
        "\n\n※最重要ルール:\n"
        "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを"
        "『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。\n"
        "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。"
    )
    return f"{base}{guard}"


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


from front.x.proactive_feed import (
    _get_research_dispatch,
    fetch_recent_x_timeline_items,
    format_x_timeline_items_for_prompt,
    rotate_x_timeline_items_for_spontaneous,
)


# =============================================================================
# サブチャンネル自発返信確率（サイコロ方式）および重複エンキュー抑止
# =============================================================================

def roll_spontaneous_dice(
    min_name: str = "OTHER_CHANNEL_RANDOM_MIN",
    max_name: str = "OTHER_CHANNEL_RANDOM_MAX",
    dmin: int = 5,
    dmax: int = 10,
) -> bool:
    """1通ごとに独立確率抽選（サイコロ方式）を行う。

    [lo, hi] の範囲から一様ランダムに整数 N を選び、1/N の確率でヒットを判定する。
    判定理由の可観測性を確保するため詳細な TRACE ログを出力する。
    """
    lo = cfg_int(min_name, dmin)
    hi = cfg_int(max_name, dmax)
    lo = max(lo, 1)
    hi = max(hi, lo)
    n = random.randint(lo, hi)
    hit = (random.randint(1, n) == 1)
    log.info(
        "TRACE random_dice_roll: min_name=%s (%d) max_name=%s (%d) chosen_n=%d hit=%s",
        min_name, lo, max_name, hi, n, hit,
    )
    return hit


def get_other_channel_message_counts(target: Any) -> dict[int, int]:
    """サブチャンネルの未返信メッセージカウント辞書を取得する。"""
    if target is None:
        return {}
    counts = getattr(target, "_other_channel_message_counts", None)
    if counts is None or not isinstance(counts, dict):
        counts = {}
        with contextlib.suppress(Exception):
            setattr(target, "_other_channel_message_counts", counts)
    return counts


def record_other_channel_message(target: Any, cid: int | None) -> int:
    """サブチャンネルでのユーザー発言を記録し、現在の未返信メッセージ数を返す。"""
    if target is None or cid is None:
        return 0
    counts = get_other_channel_message_counts(target)
    counts[cid] = counts.get(cid, 0) + 1
    return counts[cid]


def reset_other_channel_message_count(target: Any, cid: int | None) -> None:
    """サブチャンネルでBotが返信エンキュー/送信した際、未返信メッセージ数を0にリセットしてクールダウンを開始する。"""
    if target is None or cid is None:
        return
    counts = get_other_channel_message_counts(target)
    counts[cid] = 0


def should_respond_in_other_channel(
    cid: int | None = None,
    target: Any | None = None,
    message_count: int | None = None,
) -> bool:
    """サブチャンネルでBotが自発返信を行うべきかを判定する。

    クールダウン抑止、最大間隔強制返信、独立確率サイコロ抽選を統括する。
    """
    if cfg_bool("OTHER_CHANNEL_ALWAYS_RESPOND", False):
        return True

    if not cfg_bool("OTHER_CHANNEL_COOLDOWN_ENABLED", True) or (target is None and message_count is None) or cid is None:
        return roll_spontaneous_dice("OTHER_CHANNEL_RANDOM_MIN", "OTHER_CHANNEL_RANDOM_MAX", 5, 10)

    cooldown = max(0, cfg_int("OTHER_CHANNEL_COOLDOWN_MESSAGES", 0))
    max_interval = cfg_int("OTHER_CHANNEL_MAX_INTERVAL_MESSAGES", -1)

    if message_count is not None:
        current = message_count
    else:
        counts = get_other_channel_message_counts(target)
        current = counts.get(cid, 0)

    # 1. クールダウン中: 返信を抑止
    if cooldown > 0 and current < cooldown:
        log.info(
            "TRACE other_channel_decision: cid=%s cooldown active count=%d < cooldown=%d (suppressed)",
            cid, current, cooldown,
        )
        return False

    # 2. 最大間隔到達: 確定で返信
    if max_interval > 0 and current >= max_interval:
        log.info(
            "TRACE other_channel_decision: cid=%s max interval reached count=%d >= max=%d (forced response)",
            cid, current, max_interval,
        )
        return True

    # 3. 通常時（サイコロ抽選）
    res = roll_spontaneous_dice("OTHER_CHANNEL_RANDOM_MIN", "OTHER_CHANNEL_RANDOM_MAX", 5, 10)
    log.info(
        "TRACE other_channel_decision: cid=%s dice roll count=%d cooldown=%d max=%d result=%s",
        cid, current, cooldown, max_interval, res,
    )
    return res


def is_normal_reply_active(target: Any, cid: int | None) -> bool:
    """指定チャンネルで通常返信がアクティブ（処理中またはキュー内待機中）か確認する。"""
    if target is None or cid is None:
        return False
    active_cids = getattr(target, "_active_normal_reply_cids", None)
    return bool(active_cids is not None and cid in active_cids)


def mark_normal_reply_active(
    target: Any,
    cid: int | None,
    *,
    is_other_channel: bool = False,
) -> bool:
    """通常返信をアクティブとしてマークする。

    既にアクティブな場合は False を返し重複エンキューを抑止する。
    アクティブ化に成功し、is_other_channel=True の場合は未返信メッセージ数を即座にリセットする。
    """
    if target is None or cid is None:
        return True
    active_cids = getattr(target, "_active_normal_reply_cids", None)
    if active_cids is None or not isinstance(active_cids, set):
        active_cids = set()
        with contextlib.suppress(Exception):
            setattr(target, "_active_normal_reply_cids", active_cids)

    if cid in active_cids:
        log.info(
            "TRACE on_message: normal reply already active/queued for channel=%s drop channel=%s",
            cid, cid,
        )
        return False

    active_cids.add(cid)
    if is_other_channel:
        reset_other_channel_message_count(target, cid)
        log.info("TRACE on_message: reset other channel message count on normal reply enqueue cid=%s", cid)
    return True


def mark_normal_reply_inactive(target: Any, cid: int | None) -> None:
    """通常返信のアクティブ状態を解除する。"""
    if target is None or cid is None:
        return
    active_cids = getattr(target, "_active_normal_reply_cids", None)
    if active_cids is not None and isinstance(active_cids, set):
        active_cids.discard(cid)


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _AgentProactiveBase = OllamaChatProtocol
else:
    _AgentProactiveBase = object


class AgentProactiveMixin(_AgentProactiveBase):
    """自発的発言制御Mixin"""

    @property
    def active_normal_reply_cids(self) -> set[int]:
        """アクティブな通常返信のチャンネルID集合を取得する。"""
        active = getattr(self, "_active_normal_reply_cids", None)
        if active is None or not isinstance(active, set):
            active = set()
            with contextlib.suppress(Exception):
                setattr(self, "_active_normal_reply_cids", active)
        return active

    def is_normal_reply_active(self, cid: int | None) -> bool:
        """指定チャンネルで通常返信がアクティブか判定する。"""
        return is_normal_reply_active(self, cid)

    def mark_normal_reply_active(self, cid: int | None, *, is_other_channel: bool = False) -> bool:
        """指定チャンネルの通常返信をアクティブとしてマークする（重複時はFalse）。"""
        return mark_normal_reply_active(self, cid, is_other_channel=is_other_channel)

    def mark_normal_reply_inactive(self, cid: int | None) -> None:
        """指定チャンネルの通常返信アクティブ状態を解除する。"""
        mark_normal_reply_inactive(self, cid)

    def should_respond_in_other_channel(self, cid: int | None = None, message_count: int | None = None) -> bool:
        """サブチャンネル自発返信の判定を行う。"""
        return should_respond_in_other_channel(cid=cid, target=self, message_count=message_count)

    def record_other_channel_message(self, cid: int | None) -> int:
        """サブチャンネルでのメッセージを受信記録する。"""
        return record_other_channel_message(self, cid)

    def reset_other_channel_message_count(self, cid: int | None) -> None:
        """サブチャンネルの未返信メッセージ数をリセットする。"""
        reset_other_channel_message_count(self, cid)

    async def _build_proactive_channel_context_block(
        self,
        channel_id: int,
        guild_id: int | None,
    ) -> str:
        """自発発言向けに対象チャンネルの直近会話、進行中スレッド、チャンネル要約を取得してフォーマットする。"""
        recent_lines: list[str] = []
        try:
            cache = getattr(self, "channel_context_cache", None)
            if cache and isinstance(cache, dict) and channel_id in cache:
                raw_turns = list(cache[channel_id])
                valid_turns = [
                    t for t in raw_turns
                    if isinstance(t, dict) and t.get("role") != "system" and str(t.get("line") or t.get("content") or "").strip()
                ]
                for t in valid_turns[-6:]:
                    line_str = str(t.get("line") or "").strip()
                    if not line_str:
                        name = str(t.get("name") or "User").strip()
                        content = str(t.get("content") or "").strip()
                        line_str = f"{name}: {content}"
                    if line_str:
                        recent_lines.append(line_str)
        except Exception as e:
            log.warning("failed to get channel context lines channel_id=%s: %r", channel_id, e)

        active_thread_topics: list[str] = []
        try:
            tracker = getattr(self, "thread_tracker", None)
            if tracker:
                get_active_fn = getattr(tracker, "get_active_threads", None)
                if callable(get_active_fn):
                    threads = get_active_fn(channel_id)
                    for t in threads:
                        topic = str(getattr(t, "topic", "") or "").strip()
                        if topic and topic not in active_thread_topics:
                            active_thread_topics.append(topic)

            if not active_thread_topics and hasattr(self, "memory_store") and self.memory_store:
                store_active_fn = getattr(self.memory_store, "get_active_channel_threads", None)
                if callable(store_active_fn):
                    min_active_ts = time.time() - 1800.0
                    try:
                        res = store_active_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            guild_id=guild_id,
                            channel_id=channel_id,
                            min_active_ts=min_active_ts,
                        )
                        db_threads = await _safe_await(res)
                    except TypeError:
                        try:
                            res = store_active_fn(channel_id=channel_id)
                            db_threads = await _safe_await(res)
                        except Exception:
                            db_threads = []
                    if isinstance(db_threads, list):
                        for dt in db_threads:
                            topic = str(dt.get("topic") if isinstance(dt, dict) else getattr(dt, "topic", "") or "").strip()
                            if topic and topic not in active_thread_topics:
                                active_thread_topics.append(topic)
        except Exception as e:
            log.warning("failed to get active thread topics channel_id=%s: %r", channel_id, e)

        channel_summary = ""
        try:
            if hasattr(self, "memory_store") and self.memory_store:
                get_sum_fn = getattr(self.memory_store, "get_channel_summary", None)
                if callable(get_sum_fn):
                    try:
                        res = get_sum_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            guild_id=guild_id,
                            channel_id=channel_id,
                        )
                        s_rec = await _safe_await(res)
                    except TypeError:
                        try:
                            res = get_sum_fn(
                                persona=cfg_persona_namespace(custom_cfg=cfg),
                                channel_id=channel_id,
                            )
                            s_rec = await _safe_await(res)
                        except Exception:
                            s_rec = None
                    if s_rec:
                        if isinstance(s_rec, dict):
                            channel_summary = str(s_rec.get("summary") or "").strip()
                        else:
                            channel_summary = str(getattr(s_rec, "summary", "") or "").strip()
        except Exception as e:
            log.warning("failed to get channel summary channel_id=%s: %r", channel_id, e)

        parts: list[str] = []
        if channel_summary:
            parts.append(f"- チャンネルの話題要約: {channel_summary[:300]}")
        if active_thread_topics:
            topics_str = "、".join(active_thread_topics[:3])
            parts.append(f"- 進行中の話題: {topics_str}")
        if recent_lines:
            parts.append("- 直近の会話ログ:")
            for line in recent_lines:
                parts.append(f"  {line}")

        if not parts:
            return ""

        header = "【最近のチャンネル内の会話・流れ（参考）】"
        rule = "※この見出しや以下の会話ログ、分析テキストを直接オウム返し・引用・言及しないでください。チャンネルの空気感や話題の前提知識としてのみ参考にしてください。"
        return "\n".join([header, rule] + parts)

    async def _build_proactive_memory_prompt(
        self,
        channel_id: int | None = None,
        guild_id: int | None = None,
        channel_context_block: str = "",
    ) -> str:
        if not channel_context_block and channel_id is not None:
            channel_context_block = await self._build_proactive_channel_context_block(channel_id, guild_id)

        action_type = random.choices(["chat", "topic"], weights=[0.7, 0.3])[0]
        if action_type == "topic":
            base_topic = _build_topic_prompt()
            if channel_context_block:
                return f"{base_topic}\n\n{channel_context_block}"
            return base_topic

        topic_hint = ""
        async with self._emotion_lock:
            topic_hint = str(self.emotion_state.recent_topic_hint or "").strip()
        topic_hint = re.sub(r"\s+", " ", topic_hint).strip(" -・\t")

        episodes_summary = ""
        if channel_id is not None and hasattr(self, "memory_store") and self.memory_store:
            list_ep_fn = getattr(self.memory_store, "list_episodes", None) or getattr(self.memory_store, "list_recent_episodes", None)
            if callable(list_ep_fn):
                try:
                    res = list_ep_fn(
                        persona=cfg_persona_namespace(custom_cfg=cfg),
                        guild_id=guild_id,
                        channel_id=channel_id,
                        limit=3,
                    )
                    episodes = await _safe_await(res)
                    if isinstance(episodes, list):
                        summaries: list[str] = []
                        for ep in episodes:
                            sum_text = str(ep.get("summary") if isinstance(ep, dict) else getattr(ep, "summary", "") or "").strip()
                            if sum_text and sum_text not in summaries:
                                summaries.append(sum_text)
                        if summaries:
                            episodes_summary = " / ".join(summaries[:2])
                except Exception as e:
                    log.warning("failed to fetch episodes for proactive prompt: %r", e)

        prompt = "Discordの雑談チャンネルに、会話のきっかけになる自然で短い話題を1つ投げて。"
        prompt += (
            "\n\n※最重要ルール:\n"
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを"
            "『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。\n"
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。"
        )

        materials: list[str] = []
        if topic_hint:
            materials.append(f"最近気になっている題材: {topic_hint}")
        if episodes_summary:
            materials.append(f"過去のエピソード・思い出: {episodes_summary}")

        if materials:
            prompt += (
                "\n\n[内部素材（このラベルや原文の丸写しは出力禁止）]"
                "\n" + "\n".join(f"- {m}" for m in materials)
                + "\nこの題材は必要なら自然に言い換えて、セリフ本文だけを出してください。"
            )

        if channel_context_block:
            prompt += f"\n\n{channel_context_block}"

        return prompt

    def _build_proactive_x_timeline_prompt(
        self,
        timeline_block: str,
        *,
        channel_context_block: str = "",
    ) -> str:
        parts = [
            "Xのタイムラインで見かけた投稿をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "投稿本文の丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[Xタイムライン直近投稿]",
            timeline_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ]
        if channel_context_block:
            parts.extend(["", channel_context_block])
        return "\n".join(parts)

    def _build_proactive_img_prompt(
        self,
        catalog_block: str,
        *,
        channel_context_block: str = "",
    ) -> str:
        parts = [
            "ふたば☆ちゃんねるのIMG掲示板で見かけた面白いスレッドを材料にして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "スレッドの丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[IMG 勢い上位スレッド]",
            catalog_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ]
        if channel_context_block:
            parts.extend(["", channel_context_block])
        return "\n".join(parts)

    def _build_proactive_user_interest_prompt(
        self,
        interest_block: str,
        *,
        channel_context_block: str = "",
    ) -> str:
        parts = [
            "サーバーのメンバーが以前興味を持っていた話題をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "分析結果の読み上げや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[サーバーメンバーの関心事]",
            interest_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ]
        if channel_context_block:
            parts.extend(["", channel_context_block])
        return "\n".join(parts)

    def _proactive_num_predict(self) -> int | None:
        configured = cfg_int("AGENT_PROACTIVE_NUM_PREDICT", 0)
        if configured > 0:
            return configured

        if model_supports_thinking():
            return 2048

        base = cfg_int("OLLAMA_REPLY_NUM_PREDICT", 128)
        return max(base, 2048) if base > 0 else 2048

    def _proactive_retry_num_predict(self) -> int | None:
        configured = cfg_int("AGENT_PROACTIVE_RETRY_NUM_PREDICT", 0)
        if configured > 0:
            return configured

        if model_supports_thinking():
            return 3072

        base = self._proactive_num_predict() or 0
        return max(base * 2, 3072) if base > 0 else 3072

    def _proactive_timeout_sec(self) -> float:
        return max(cfg_float("AGENT_PROACTIVE_TIMEOUT_SEC", 120.0), 10.0)

    async def _resolve_proactive_channel(
        self,
        channel_id_value: int,
    ) -> discord.TextChannel | discord.Thread | None:
        channel = self.bot.get_channel(channel_id_value)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel

        fetch_channel = getattr(self.bot, "fetch_channel", None)
        if callable(fetch_channel):
            try:
                fetched = await fetch_channel(channel_id_value)
            except Exception as e:
                log.warning("proactive channel fetch failed channel_id=%s err=%r", channel_id_value, e)
            else:
                if isinstance(fetched, (discord.TextChannel, discord.Thread)):
                    return fetched
                log.warning(
                    "proactive channel fetch returned unsupported type channel_id=%s type=%s",
                    channel_id_value,
                    type(fetched).__name__,
                )
                return None

        if channel is None:
            log.warning("proactive channel is not cached channel_id=%s", channel_id_value)
        else:
            log.warning(
                "proactive channel has unsupported cached type channel_id=%s type=%s",
                channel_id_value,
                type(channel).__name__,
            )
        return None

    def _record_proactive_reply(self, reply: str) -> None:
        """送信した自発発話を内部キャッシュに記録する。"""
        cleaned = str(reply or "").strip()
        if not cleaned:
            return
        cache = getattr(self, "_recent_proactive_replies", None)
        if not isinstance(cache, deque):
            cache = deque(maxlen=max(cfg_int("RECENT_PROACTIVE_CACHE_SIZE", 10), 5))
            with contextlib.suppress(Exception):
                setattr(self, "_recent_proactive_replies", cache)
        cache.append(cleaned)

    def _get_recent_assistant_utterances(
        self,
        channel_id: int | None = None,
        limit: int = 5,
    ) -> list[str]:
        """直近のBot自身の発言リスト（自発発話キャッシュおよびチャンネルコンテキスト）を取得する。"""
        utterances: list[str] = []
        max_limit = max(int(limit or 5), 1)

        # 1. 内部の自発発話キャッシュから取得
        recent_proactive = getattr(self, "_recent_proactive_replies", None)
        if isinstance(recent_proactive, (list, deque)):
            for r in reversed(list(recent_proactive)):
                text = str(r or "").strip()
                if text and text not in utterances:
                    utterances.append(text)
                if len(utterances) >= max_limit:
                    return utterances

        # 2. channel_context_cache からBot発言を抽出
        cache = getattr(self, "channel_context_cache", None)
        if isinstance(cache, dict):
            bot_user = getattr(getattr(self, "bot", None), "user", None)
            bot_uid_val = getattr(bot_user, "id", None)
            cids: list[int] = []
            if channel_id is not None and channel_id in cache:
                cids.append(channel_id)
            else:
                cids.extend(list(cache.keys()))

            for cid in cids:
                turns = list(cache.get(cid) or [])
                for turn in reversed(turns):
                    if not isinstance(turn, dict):
                        continue
                    role = str(turn.get("role") or "").lower()
                    author_id = turn.get("author_id")
                    name = str(turn.get("name") or "").lower()

                    is_bot = (
                        role == "assistant"
                        or (bot_uid_val is not None and author_id == bot_uid_val)
                        or (name in ("assistant", "ai", "bot") and role != "user")
                    )
                    if is_bot:
                        content = str(turn.get("content") or "").strip()
                        if not content:
                            line = str(turn.get("line") or "").strip()
                            if ":" in line:
                                content = line.split(":", 1)[1].strip()
                            elif "：" in line:
                                content = line.split("：", 1)[1].strip()
                            else:
                                content = line
                        if content and content not in utterances:
                            utterances.append(content)
                        if len(utterances) >= max_limit:
                            return utterances
        return utterances

    def _is_proactive_reply_duplicate(
        self,
        reply: str,
        *,
        channel_id: int | None = None,
        recent_replies: list[str] | None = None,
        threshold: float | None = None,
    ) -> bool:
        """生成された自発発話が直近のBot発言と意味的・語句的に酷似しているか判定する。"""
        cleaned = str(reply or "").strip()
        if not cleaned:
            return False

        sim_threshold = (
            threshold
            if threshold is not None
            else cfg_float("AGENT_PROACTIVE_DUPLICATE_SIMILARITY_THRESHOLD", 0.55)
        )
        candidates = (
            list(recent_replies)
            if recent_replies is not None
            else self._get_recent_assistant_utterances(channel_id=channel_id)
        )
        for prev in candidates:
            sim = calculate_utterance_similarity(cleaned, prev)
            if sim >= sim_threshold:
                log.warning(
                    "proactive reply rejected due to duplication with recent assistant utterance (similarity=%.2f >= %.2f): reply=%r vs prev=%r",
                    sim,
                    sim_threshold,
                    cleaned,
                    prev,
                )
                return True
        return False

    def _sanitize_proactive_reply(
        self,
        reply: str,
        *,
        channel_id: int | None = None,
        recent_replies: list[str] | None = None,
    ) -> str:
        cleaned = extract_first_user_facing_reply(str(reply or ""))
        if not cleaned or looks_like_abnormal_assistant_reply(cleaned):
            cleaned = sanitize_generated_reply(str(reply or "")).strip()
            if looks_like_abnormal_assistant_reply(cleaned):
                return ""
        if not cleaned:
            return ""

        # BAN-94: 直近のBot自身の発言と酷似している場合はリジェクト
        if self._is_proactive_reply_duplicate(cleaned, channel_id=channel_id, recent_replies=recent_replies):
            return ""

        return cleaned

    async def _generate_proactive_reply(
        self,
        prompt: str,
        *,
        system_prompt: str,
        source: str,
        channel_id: int | None = None,
        recent_replies: list[str] | None = None,
    ) -> str:
        prompt_with_suffix = append_chat_reply_output_suffix(prompt)
        raw_reply = await self._call_model(
            prompt_with_suffix,
            system_prompt=system_prompt,
            num_predict=self._proactive_num_predict(),
            timeout_sec=self._proactive_timeout_sec(),
        )
        cleaned = self._sanitize_proactive_reply(raw_reply, channel_id=channel_id, recent_replies=recent_replies)
        if cleaned:
            return cleaned

        log.warning(
            "proactive reply unusable source=%s stage=initial raw=%r",
            source,
            truncate_text(str(raw_reply or ""), 240),
        )

        retry_prompt = build_output_only_retry_prompt(prompt)
        retried = await self._call_model(
            retry_prompt,
            system_prompt=system_prompt,
            num_predict=self._proactive_retry_num_predict(),
            timeout_sec=self._proactive_timeout_sec(),
        )
        cleaned = self._sanitize_proactive_reply(retried, channel_id=channel_id, recent_replies=recent_replies)
        if cleaned:
            return cleaned

        log.warning(
            "proactive reply unusable source=%s stage=retry raw=%r",
            source,
            truncate_text(str(retried or ""), 240),
        )
        return ""

    async def _store_proactive_bot_utterance(self, channel: discord.abc.Messageable, raw_reply: str) -> None:
        try:
            cleaned_reply = extract_first_user_facing_reply(str(raw_reply or ""))
            if not cleaned_reply or looks_like_abnormal_assistant_reply(cleaned_reply):
                return
            store_method = getattr(self.memory_store, "add_bot_utterance", None)
            if not callable(store_method):
                return
            guild_obj = getattr(channel, "guild", None)
            await store_method(
                persona=cfg_persona_namespace(custom_cfg=cfg),
                guild_id=getattr(guild_obj, "id", None),
                channel_id=getattr(channel, "id", None),
                content=cleaned_reply,
            )
        except Exception as e:
            log.exception("proactive bot utterance save failed: %s", e)

    async def _store_proactive_episode_memory(
        self,
        channel: discord.abc.Messageable,
        reply: str,
        *,
        source: str,
        topic_summary: str = "",
        details: str = "",
    ) -> None:
        try:
            store = getattr(self, "memory_store", None)
            add_episode = getattr(store, "add_episode", None)
            if not callable(add_episode) or not cfg_bool("MEMORY_EPISODE_ENABLED", True):
                return

            guild_obj = getattr(channel, "guild", None)
            guild_id_val = getattr(guild_obj, "id", None)
            cid = getattr(channel, "id", None)
            persona = cfg_persona_namespace(custom_cfg=cfg)

            source_label_map = {
                "img": "ふたばIMGスレッド",
                "x": "Xタイムライン",
                "user_interest": "ユーザー関心事",
                "memory": "最近の関心題材",
            }
            label = source_label_map.get(source, "自発的な話題")
            summary = f"{label}（{topic_summary[:80]}）をきっかけに、自発的に話題を振った。" if topic_summary else f"{label}から自発的に話題を振った。"
            what_text = f"情報源: {label}\n参照内容: {details[:400]}" if details else f"情報源: {label}"

            call_res = add_episode(
                persona=persona,
                guild_id=guild_id_val,
                channel_id=cid,
                user_id=None,
                summary=summary[:280],
                what=what_text[:600],
                bot_action=reply[:600],
                importance=float(cfg("MEMORY_EPISODE_BASE_IMPORTANCE", 0.50) or 0.50),
                tags=["proactive", source, "spontaneous"],
            )
            await _safe_await(call_res)
            log.info("proactive episode memory stored: source=%s summary=%r", source, summary)
        except Exception as e:
            log.warning("failed to store proactive episode memory: %r", e)

    async def _proactive_action(self) -> None:
        if not getattr(self, "is_active", True):
            log.info("proactive action skipped: reason=not_active")
            return
        if not cfg_bool("AGENT_PROACTIVE_POST_ENABLED", True):
            log.info("proactive action skipped: reason=proactive_post_disabled")
            return
        from lib.date_utils import is_in_quiet_window, now_jst
        if cfg_bool("BOT_QUIET_HOURS_ENABLED", False):
            if is_in_quiet_window(
                now_jst(),
                str(cfg("BOT_QUIET_HOURS_START", "01:00")),
                str(cfg("BOT_QUIET_HOURS_END", "07:00")),
            ):
                log.info("proactive action skipped: reason=quiet_hours (bot is sleeping)")
                return
        channel_id_value = cfg_primary_channel_id(0)
        if channel_id_value <= 0:
            log.info("proactive action skipped: reason=no_primary_channel channel_id=%s", channel_id_value)
            return

        async with self._emotion_lock:
            last_human_ts = float(self.emotion_state.last_human_message_ts or 0.0)
            last_post_ts = float(self.emotion_state.last_proactive_post_ts or 0.0)

        now = time.time()
        if last_human_ts > 0:
            idle_sec = max(now - float(self._last_managed_human_message_ts or 0.0), 0.0)
            min_idle_sec = cfg_float("AGENT_PROACTIVE_MIN_IDLE_SEC", 45 * 60.0)
            if idle_sec < min_idle_sec:
                log.info(
                    "proactive action skipped: reason=min_idle_not_met idle_sec=%.1f min_idle_sec=%.1f",
                    idle_sec,
                    min_idle_sec,
                )
                return

        cooldown_sec = cfg_float("AGENT_PROACTIVE_COOLDOWN_SEC", 3 * 3600.0)
        elapsed_post = now - last_post_ts
        if elapsed_post < cooldown_sec:
            log.info(
                "proactive action skipped: reason=cooldown elapsed_sec=%.1f cooldown_sec=%.1f",
                elapsed_post,
                cooldown_sec,
            )
            return

        channel = await self._resolve_proactive_channel(channel_id_value)
        if channel is None:
            log.info("proactive action skipped: reason=channel_not_resolved channel_id=%s", channel_id_value)
            return

        guild_obj = getattr(channel, "guild", None)
        guild_id_val = getattr(guild_obj, "id", None)
        channel_context_block = await self._build_proactive_channel_context_block(
            channel_id=channel.id,
            guild_id=guild_id_val,
        )

        proactive_system_prompt = build_topic_system_prompt()

        try:
            typing_enabled = bool(
                cfg_bool("DISCORD_TYPING_INDICATOR_ENABLED", False)
                or cfg_bool("TYPING_INDICATOR_ENABLED", False)
            )
            typing_cm = channel.typing() if typing_enabled else contextlib.nullcontext()
            async with typing_cm:
                source = choose_spontaneous_source()
                log.info("proactive action starting: chosen source=%s", source)
                prompt = ""

                if source == "x":
                    try:
                        log.info("proactive action: attempting to fetch X timeline")
                        timeline_limit = max(cfg_int("SPONTANEOUS_X_TIMELINE_POSTS", 10), 1)
                        timeline_items = await fetch_recent_x_timeline_items(limit=timeline_limit)
                        timeline_items = rotate_x_timeline_items_for_spontaneous(self, timeline_items, limit=timeline_limit)
                        timeline_block = format_x_timeline_items_for_prompt(timeline_items, limit=timeline_limit)
                        if not timeline_block:
                            raise RuntimeError("empty x timeline")
                        prompt = self._build_proactive_x_timeline_prompt(
                            timeline_block,
                            channel_context_block=channel_context_block,
                        )
                        log.info("proactive action: successfully built X timeline prompt")
                    except Exception as e:
                        log.warning("x proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "img":
                    try:
                        log.info("proactive action: attempting to fetch IMG catalog")
                        from harness.agent.tool_registry.web_research import _img_helper
                        fetch_func = getattr(_img_helper, "fetch_img_top5_threads", None)
                        if callable(fetch_func):
                            timeline_items = await fetch_func(limit=5)
                            if not timeline_items:
                                raise RuntimeError("empty img catalog")

                            format_func = getattr(_img_helper, "format_img_top5_summary", None)
                            if callable(format_func):
                                timeline_block = format_func(timeline_items)
                            else:
                                timeline_block = "\n".join([f"- {t.get('text', '')}" for t in timeline_items])

                            prompt = self._build_proactive_img_prompt(
                                timeline_block,
                                channel_context_block=channel_context_block,
                            )
                            log.info("proactive action: successfully built IMG prompt")
                        else:
                            raise RuntimeError("img helper fetch_img_top5_threads not found")
                    except Exception as e:
                        log.warning("img proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "user_interest":
                    try:
                        log.info("proactive action: attempting to fetch user interests")
                        persona = cfg_persona_namespace(custom_cfg=cfg)
                        interests = await self.memory_store.list_user_interests(
                            persona=persona,
                            guild_id=guild_id_val,
                            channel_id=channel.id,
                            limit=5,
                            min_interest_level=cfg_float("USER_INTERESTS_MIN_SCORE", 0.3),
                        )
                        if not interests:
                            raise RuntimeError("no user interests found")

                        selected_interests = random.sample(interests, min(len(interests), random.randint(1, 2)))
                        interest_block = format_user_interests_for_prompt(selected_interests)
                        if not interest_block:
                            raise RuntimeError("empty user interests block")
                        prompt = self._build_proactive_user_interest_prompt(
                            interest_block,
                            channel_context_block=channel_context_block,
                        )
                        log.info("proactive action: successfully built user interest prompt")
                        for it in selected_interests:
                            if it.get("id"):
                                with contextlib.suppress(Exception):
                                    await self.memory_store.touch_user_interest(int(it["id"]))
                    except Exception as e:
                        log.warning("user interest proactive prompt failed, fallback to memory: %r", e)
                        source = "memory"

                if source == "memory":
                    log.info("proactive action: using memory source")
                    prompt = await self._build_proactive_memory_prompt(
                        channel_id=channel.id,
                        guild_id=guild_id_val,
                        channel_context_block=channel_context_block,
                    )

                reply = await self._generate_proactive_reply(
                    prompt,
                    system_prompt=proactive_system_prompt,
                    source=source,
                    channel_id=channel.id,
                )
                if not reply and source != "memory":
                    log.warning("proactive reply source=%s was unusable; retrying with memory prompt", source)
                    source = "memory"
                    prompt = await self._build_proactive_memory_prompt(
                        channel_id=channel.id,
                        guild_id=guild_id_val,
                        channel_context_block=channel_context_block,
                    )
                    reply = await self._generate_proactive_reply(
                        prompt,
                        system_prompt=proactive_system_prompt,
                        source=source,
                        channel_id=channel.id,
                    )
                if not reply:
                    log.warning("proactive action skipped: no usable reply source=%s", source)
                    return
            sent = await channel.send(_format_reply(reply))
            self._record_proactive_reply(reply)
            await self._store_proactive_bot_utterance(channel, reply)
            bot_uid = self.bot.user.id if self.bot.user else None
            if hasattr(self, "channel_context_cache") and self.channel_context_cache is not None:
                if channel.id not in self.channel_context_cache and isinstance(self.channel_context_cache, dict):
                    self.channel_context_cache[channel.id] = deque(maxlen=max(cfg_int("DISCORD_CHANNEL_CONTEXT_LIMIT", 50), 10))
                append_context_message(self.channel_context_cache, sent, bot_user_id=bot_uid)
            if hasattr(self, "thread_tracker") and self.thread_tracker:
                try:
                    self.thread_tracker.record_message(sent, bot_user_id=bot_uid)
                except Exception as e:
                    log.warning("failed to record proactive message to thread_tracker: %r", e)

            # エピソード記憶の永続化
            topic_summary_val = ""
            details_val = ""
            if source in ("img", "x") and "timeline_block" in locals():
                details_val = str(timeline_block or "")
                first_line = details_val.splitlines()[0] if details_val else ""
                topic_summary_val = first_line.lstrip("- 【1位】URL: ").strip()[:60]
            elif source == "user_interest" and "interest_block" in locals():
                details_val = str(interest_block or "")
                topic_summary_val = "ユーザー関心事"
            elif source == "memory":
                topic_val_local = ""
                if "topic_val" in locals():
                    topic_val_local = str(locals()["topic_val"] or "")
                elif hasattr(self, "emotion_state") and hasattr(self, "_emotion_lock"):
                    async with self._emotion_lock:
                        topic_val_local = str(getattr(self.emotion_state, "recent_topic_hint", "") or "").strip()
                if topic_val_local:
                    topic_summary_val = topic_val_local
                    details_val = topic_val_local

            await self._store_proactive_episode_memory(
                channel,
                reply,
                source=source,
                topic_summary=topic_summary_val,
                details=details_val,
            )

            try:
                if hasattr(self, "_store_channel_research_cache"):
                    if source == "img" and "timeline_items" in locals() and timeline_items:
                        self._store_channel_research_cache(channel.id, {
                            "mode": "img_top5",
                            "summary": str(timeline_block or ""),
                            "threads": list(timeline_items),
                            "sources": [str(t.get("url") or "") for t in timeline_items if t.get("url")],
                            "query": "https://img.2chan.net/b/futaba.php?mode=cat&sort=6",
                        })
                    elif source == "x" and "timeline_items" in locals() and timeline_items:
                        self._store_channel_research_cache(channel.id, {
                            "mode": "x_timeline",
                            "summary": str(timeline_block or ""),
                            "sources": [str(p.get("url") or "") for p in timeline_items if p.get("url")],
                            "query": "https://x.com/home",
                        })

                pseudo_hint = ""
                if source == "x" and "timeline_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はXタイムラインから拾った: {timeline_block[:800]})"
                elif source == "img" and "timeline_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はふたばの勢い上位スレッドから拾った: {timeline_block[:800]})"
                elif source == "user_interest" and "interest_block" in locals():
                    pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景はユーザーの関心事から拾った: {interest_block[:800]})"
                elif source == "memory":
                    async with self._emotion_lock:
                        topic_val = str(self.emotion_state.recent_topic_hint or "").strip()
                    if topic_val:
                        pseudo_hint = f"(内心のメモ: 直前に自分が振った話題の背景は最近気になっていた題材から拾った: {topic_val})"

                if pseudo_hint and channel.id in self.channel_context_cache:
                    turn: dict[str, Any] = {
                        "id": -random.randint(1000000, 9999999),
                        "author_id": None,
                        "role": "system",
                        "name": "System",
                        "content": pseudo_hint,
                        "line": f"System: {pseudo_hint}",
                        "created_at_ts": time.time(),
                    }
                    self.channel_context_cache[channel.id].append(turn)
            except Exception as e:
                log.warning("failed to inject proactive pseudo hint or research cache: %r", e)
            async with self._emotion_lock:
                self.emotion_state.boredom = max(0.0, float(self.emotion_state.boredom or 0.0) - 0.70)
                self.emotion_state.loneliness = max(0.0, float(self.emotion_state.loneliness or 0.0) - 0.55)
                self.emotion_state.last_proactive_post_ts = time.time()
                self.emotion_state.mood, self.emotion_state.mood_reason = self._derive_mood_from_state()
            self._save_emotion_state()
        except Exception as e:
            log.exception("agent proactive action failed: %s", e)


_AgentProactiveMixin = AgentProactiveMixin
