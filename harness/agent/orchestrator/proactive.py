"""AIエージェント・ハーネス オーケストレーター 自発的発話制御モジュール (orchestrator/proactive.py)

自発的な話題提供（Xタイムライン、ふたばIMG掲示板、ユーザー関心事、記憶）、プロンプト構築、送信処理を担当するMixin。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import deque
from typing import Any, Literal, TYPE_CHECKING

import discord

from lib.config_utils import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
    cfg_primary_channel_id,
    model_supports_thinking,
)
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


def _get_research_dispatch() -> Any:
    try:
        import ollama_bot.ollama_chat_.ollama_chat_helpers as helpers
        if hasattr(helpers, "research_dispatch"):
            return getattr(helpers, "research_dispatch")
    except Exception:
        pass
    return research_dispatch


async def fetch_recent_x_timeline_items(
    limit: int = 10,
    *,
    custom_dispatch: Any = None,
) -> list[dict[str, str]]:
    resolved_limit = max(int(limit or 10), 1)
    dispatch_fn = custom_dispatch or _get_research_dispatch()
    result = await dispatch_fn(
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

    async def _build_proactive_memory_prompt(self) -> str:
        action_type = random.choices(["chat", "topic"], weights=[0.7, 0.3])[0]
        if action_type == "topic":
            return _build_topic_prompt()

        topic_hint = ""
        async with self._emotion_lock:
            topic_hint = str(self.emotion_state.recent_topic_hint or "").strip()
        topic_hint = re.sub(r"\s+", " ", topic_hint).strip(" -・\t")
        prompt = "Discordの雑談チャンネルに、会話のきっかけになる自然で短い話題を1つ投げて。"
        prompt += (
            "\n\n※最重要ルール:\n"
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを"
            "『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。\n"
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。"
        )
        if topic_hint:
            prompt += (
                "\n\n[内部素材]"
                f"\n最近気になっている題材（このラベルと原文は出力禁止）: {topic_hint}"
                "\nこの題材は必要なら自然に言い換えて、セリフ本文だけを出してください。"
            )
        return prompt

    def _build_proactive_x_timeline_prompt(self, timeline_block: str) -> str:
        return "\n".join([
            "Xのタイムラインで見かけた投稿をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "投稿本文の丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[Xタイムライン直近投稿]",
            timeline_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

    def _build_proactive_img_prompt(self, catalog_block: str) -> str:
        return "\n".join([
            "ふたば☆ちゃんねるのIMG掲示板で見かけた面白いスレッドを材料にして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "スレッドの丸写しや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[IMG 勢い上位スレッド]",
            catalog_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

    def _build_proactive_user_interest_prompt(self, interest_block: str) -> str:
        return "\n".join([
            "サーバーのメンバーが以前興味を持っていた話題をきっかけにして、Discordの雑談チャンネルに自然な短い話題を1つ投げて。",
            "分析結果の読み上げや箇条書きは避け、日常の会話として自然に触れること。",
            "",
            "[サーバーメンバーの関心事]",
            interest_block,
            "",
            "※最重要ルール:",
            "あなた自身がこのBot本人です。自分自身やこのAIシステムのことを『AIちゃん』『AI』『bot』などと第三者目線で語ってはいけません。",
            "必ず一人称で、相手に直接話しかける自然なセリフだけを出力してください。",
        ])

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

    def _sanitize_proactive_reply(self, reply: str) -> str:
        cleaned = extract_first_user_facing_reply(str(reply or ""))
        if cleaned and not looks_like_abnormal_assistant_reply(cleaned):
            return cleaned
        cleaned = sanitize_generated_reply(str(reply or "")).strip()
        if cleaned and not looks_like_abnormal_assistant_reply(cleaned):
            return cleaned
        return ""

    async def _generate_proactive_reply(
        self,
        prompt: str,
        *,
        system_prompt: str,
        source: str,
    ) -> str:
        prompt_with_suffix = append_chat_reply_output_suffix(prompt)
        raw_reply = await self._call_model(
            prompt_with_suffix,
            system_prompt=system_prompt,
            num_predict=self._proactive_num_predict(),
            timeout_sec=self._proactive_timeout_sec(),
        )
        cleaned = self._sanitize_proactive_reply(raw_reply)
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
        cleaned = self._sanitize_proactive_reply(retried)
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

    async def _proactive_action(self) -> None:
        if not getattr(self, "is_active", True):
            return
        if not cfg_bool("AGENT_PROACTIVE_POST_ENABLED", True):
            return
        channel_id_value = cfg_primary_channel_id(0)
        if channel_id_value <= 0:
            return

        async with self._emotion_lock:
            last_human_ts = float(self.emotion_state.last_human_message_ts or 0.0)
            last_post_ts = float(self.emotion_state.last_proactive_post_ts or 0.0)

        if last_human_ts > 0:
            idle_sec = max(time.time() - float(self._last_managed_human_message_ts or 0.0), 0.0)
            if idle_sec < cfg_float("AGENT_PROACTIVE_MIN_IDLE_SEC", 45 * 60.0):
                return

        if (time.time() - last_post_ts) < cfg_float("AGENT_PROACTIVE_COOLDOWN_SEC", 3 * 3600.0):
            return

        channel = await self._resolve_proactive_channel(channel_id_value)
        if channel is None:
            return

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
                        prompt = self._build_proactive_x_timeline_prompt(timeline_block)
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

                            prompt = self._build_proactive_img_prompt(timeline_block)
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
                        guild_obj = getattr(channel, "guild", None)
                        guild_id_val = getattr(guild_obj, "id", None)
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
                        prompt = self._build_proactive_user_interest_prompt(interest_block)
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
                    prompt = await self._build_proactive_memory_prompt()

                reply = await self._generate_proactive_reply(
                    prompt,
                    system_prompt=proactive_system_prompt,
                    source=source,
                )
                if not reply and source != "memory":
                    log.warning("proactive reply source=%s was unusable; retrying with memory prompt", source)
                    source = "memory"
                    prompt = await self._build_proactive_memory_prompt()
                    reply = await self._generate_proactive_reply(
                        prompt,
                        system_prompt=proactive_system_prompt,
                        source=source,
                    )
                if not reply:
                    log.warning("proactive action skipped: no usable reply source=%s", source)
                    return
            sent = await channel.send(_format_reply(reply))
            await self._store_proactive_bot_utterance(channel, reply)
            append_context_message(self.channel_context_cache, sent, bot_user_id=self.bot.user.id if self.bot.user else None)

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
