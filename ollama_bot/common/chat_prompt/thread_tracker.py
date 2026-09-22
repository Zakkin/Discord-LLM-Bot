"""マルチスレッド・並行会話トピック追跡モジュール (ThreadTracker)。

Discordチャンネル内で複数のユーザー同士が並行して会話している状況において、
メッセージを会話スレッド単位に自律分類・追跡し、並行トピックの混同を防ぐ。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import discord

from lib.config_utils import cfg
from ..bot_identity import BotIdentity, is_bot_name_mentioned
from ..discord_helpers import (
    author_id,
    channel_id,
    content,
    display_name,
    message_created_at_ts,
    to_context_turn,
    ContextTurn,
)

log = logging.getLogger("ollama_bot.common.chat_prompt.thread_tracker")


def _extract_thread_topic_label(texts: list[str], max_length: int = 30) -> str:
    """メッセージ群からスレッドのトピックラベル（名詞・キーワード）を簡易抽出する。"""
    if not texts:
        return "雑談"
    combined = " ".join(texts[-5:])

    # 1. カタカナ連続（固有名詞・単語）
    katakana = re.findall(r"[ァ-ヶヴー]{3,}", combined)
    # 2. 漢字+ひらがな語句
    kanji_words = re.findall(r"[\u4e00-\u9fff]{2,}(?:[\u3041-\u309f]{1,2})?", combined)

    # 一般的な接続詞や無関係語を除外
    stop_words = {"それ", "これ", "あれ", "なん", "こと", "もの", "そう", "どう", "どこ", "いつ", "だれ", "本当", "本当", "自分", "相手"}
    filtered_kanji = [w for w in kanji_words if w not in stop_words]

    candidates = katakana[:2] + filtered_kanji[:2]
    if candidates:
        label = "・".join(dict.fromkeys(candidates))[:max_length]
        return label or "雑談"

    # 候補が取れなければ最初の発言から要約
    first_clean = re.sub(r"^[（(].+?[）)]\s*", "", texts[0]).strip()
    first_clean = re.sub(r"[!！?？\s]+", "", first_clean)
    if first_clean:
        return first_clean[:18] + ("…" if len(first_clean) > 18 else "")
    return "雑談"


@dataclass
class ConversationThread:
    """単一の会話スレッド／トピックの追跡データクラス。"""
    thread_id: str
    channel_id: int
    root_message_id: int | None = None
    message_ids: list[int] = field(default_factory=list)
    participant_ids: set[int] = field(default_factory=set)
    participant_names: list[str] = field(default_factory=list)
    turns: list[ContextTurn] = field(default_factory=list)
    topic: str = "雑談"
    last_active_ts: float = 0.0
    is_bot_participating: bool = False

    def add_turn(
        self,
        turn: ContextTurn,
        *,
        author_user_id: int | None,
        author_name: str,
        is_bot: bool,
    ) -> None:
        mid = turn.get("id")
        if mid and mid not in self.message_ids:
            self.message_ids.append(mid)
        if author_user_id is not None:
            self.participant_ids.add(author_user_id)
        if author_name and author_name not in self.participant_names:
            self.participant_names.append(author_name)
        if is_bot:
            self.is_bot_participating = True

        self.turns.append(turn)
        now_ts = turn.get("created_at_ts") or time.time()
        if now_ts > self.last_active_ts:
            self.last_active_ts = float(now_ts)

        # トピックの更新
        texts = [str(t.get("content", "") or "") for t in self.turns if t.get("content")]
        self.topic = _extract_thread_topic_label(texts)


class ThreadTracker:
    """Discordチャンネル内の複数会話スレッドを管理・分類するトラッカー。"""

    def __init__(self, max_active_threads_per_channel: int = 5, thread_max_idle_sec: float = 900.0) -> None:
        self.max_active_threads = max_active_threads_per_channel
        self.thread_max_idle_sec = thread_max_idle_sec
        # channel_id -> {thread_id: ConversationThread}
        self._threads: dict[int, dict[str, ConversationThread]] = {}
        # channel_id -> {message_id: thread_id}
        self._message_to_thread: dict[int, dict[int, str]] = {}

    def record_message(
        self,
        message: discord.Message,
        *,
        bot_user_id: int | None = None,
        bot_identity: BotIdentity | None = None,
    ) -> ConversationThread:
        """受信または送信されたメッセージを適切なスレッドに割り当てて記録する。"""
        cid = channel_id(message)
        mid = getattr(message, "id", None)
        if cid is None or mid is None or not isinstance(mid, int):
            # 識別不能なメッセージはダミースレッドを返す
            dummy = ConversationThread(thread_id="dummy", channel_id=cid or 0)
            return dummy

        channel_threads = self._threads.setdefault(cid, {})
        msg_map = self._message_to_thread.setdefault(cid, {})

        if mid in msg_map:
            thread_id = msg_map[mid]
            if thread_id in channel_threads:
                return channel_threads[thread_id]

        turn = to_context_turn(message, bot_user_id=bot_user_id)
        aid = author_id(message)
        a_name = display_name(message)
        is_bot = bool(bot_user_id is not None and aid == bot_user_id)
        msg_text = content(message)

        # 1. 返信先（Reply reference）によるスレッド結合
        ref = getattr(message, "reference", None)
        ref_mid = getattr(ref, "message_id", None) if ref is not None else None
        target_thread: Optional[ConversationThread] = None

        if isinstance(ref_mid, int) and ref_mid in msg_map:
            parent_thread_id = msg_map[ref_mid]
            target_thread = channel_threads.get(parent_thread_id)

        # 2. 既存スレッドが見つからないが、返信先が存在する場合（返信元メッセージをrootとして新規スレッド）
        if target_thread is None and isinstance(ref_mid, int):
            new_tid = f"reply_{ref_mid}"
            target_thread = channel_threads.get(new_tid)
            if target_thread is None:
                target_thread = ConversationThread(
                    thread_id=new_tid,
                    channel_id=cid,
                    root_message_id=ref_mid,
                )
                channel_threads[new_tid] = target_thread

        # 3. 返信ではない場合：Bot宛てメンションまたは名前呼びかけ
        bot_mentioned = False
        if target_thread is None:
            if bot_user_id is not None and any(getattr(u, "id", None) == bot_user_id for u in getattr(message, "mentions", [])):
                bot_mentioned = True
            elif bot_identity is not None and is_bot_name_mentioned(msg_text, bot_identity):
                bot_mentioned = True

            if bot_mentioned:
                # このユーザーとBotの直近アクティブスレッドがあれば継続
                now = time.time()
                for t in reversed(list(channel_threads.values())):
                    if (now - t.last_active_ts) <= self.thread_max_idle_sec:
                        if aid is not None and aid in t.participant_ids and t.is_bot_participating:
                            target_thread = t
                            break
                if target_thread is None:
                    new_tid = f"bot_{aid or 'anon'}_{mid}"
                    target_thread = ConversationThread(
                        thread_id=new_tid,
                        channel_id=cid,
                        root_message_id=mid,
                        is_bot_participating=True,
                    )
                    channel_threads[new_tid] = target_thread

        # 4. 発言者の時間的連続性（直前スレッドへの追従）
        if target_thread is None and aid is not None:
            now = time.time()
            for t in reversed(list(channel_threads.values())):
                # 直近120秒以内に同一話者が参加しているスレッド
                if (now - t.last_active_ts) <= 120.0 and aid in t.participant_ids:
                    target_thread = t
                    break

            # Bot自身の発言の場合、直近アクティブなBot参加スレッドに追従
            if target_thread is None and is_bot:
                for t in reversed(list(channel_threads.values())):
                    if (now - t.last_active_ts) <= 120.0 and t.is_bot_participating:
                        target_thread = t
                        break

        # 5. 上記に当てはまらない場合：新規独立スレッド
        if target_thread is None:
            new_tid = f"thread_{mid}"
            target_thread = ConversationThread(
                thread_id=new_tid,
                channel_id=cid,
                root_message_id=mid,
            )
            channel_threads[new_tid] = target_thread

        # ターンを追加
        if turn is not None:
            target_thread.add_turn(turn, author_user_id=aid, author_name=a_name, is_bot=is_bot)
        msg_map[mid] = target_thread.thread_id

        self.prune_stale_threads(cid)
        return target_thread

    def match_thread(
        self,
        message: discord.Message,
        *,
        bot_user_id: int | None = None,
        bot_identity: BotIdentity | None = None,
    ) -> ConversationThread:
        """メッセージが属するスレッドを返す。未記録なら記録して返す。"""
        cid = channel_id(message)
        mid = getattr(message, "id", None)
        if cid is not None and isinstance(mid, int):
            msg_map = self._message_to_thread.get(cid, {})
            if mid in msg_map:
                tid = msg_map[mid]
                if tid in self._threads.get(cid, {}):
                    return self._threads[cid][tid]
        return self.record_message(message, bot_user_id=bot_user_id, bot_identity=bot_identity)

    def get_active_threads(self, channel_id_value: int, max_age_sec: float | None = None) -> list[ConversationThread]:
        """指定チャンネル内のアクティブなスレッドを最終活動日時の新しい順で返す。"""
        age_limit = max_age_sec if max_age_sec is not None else self.thread_max_idle_sec
        channel_threads = self._threads.get(channel_id_value, {})
        now = time.time()
        active = [
            t for t in channel_threads.values()
            if (now - t.last_active_ts) <= age_limit and len(t.turns) >= 1
        ]
        active.sort(key=lambda t: t.last_active_ts, reverse=True)
        return active

    def get_parallel_threads(
        self,
        channel_id_value: int,
        current_thread_id: str,
        max_age_sec: float | None = None,
    ) -> list[ConversationThread]:
        """現在対話中のスレッド以外の、同一チャンネル内で並行稼働している別スレッド群を返す。"""
        all_active = self.get_active_threads(channel_id_value, max_age_sec=max_age_sec)
        return [t for t in all_active if t.thread_id != current_thread_id and len(t.turns) >= 2]

    def prune_stale_threads(self, channel_id_value: int) -> None:
        """非アクティブになった古いスレッドを削除し、上限数に収める。"""
        channel_threads = self._threads.get(channel_id_value)
        if not channel_threads:
            return
        now = time.time()
        # 1. 期限切れスレッドの削除
        to_delete = [
            tid for tid, t in channel_threads.items()
            if (now - t.last_active_ts) > (self.thread_max_idle_sec * 2)
        ]
        for tid in to_delete:
            t = channel_threads.pop(tid, None)
            if t:
                for mid in t.message_ids:
                    self._message_to_thread.get(channel_id_value, {}).pop(mid, None)

        # 2. 上限数（max_active_threads）を超えている場合、古い順に削除
        if len(channel_threads) > self.max_active_threads:
            sorted_threads = sorted(channel_threads.values(), key=lambda t: t.last_active_ts)
            overflow_count = len(channel_threads) - self.max_active_threads
            for t in sorted_threads[:overflow_count]:
                channel_threads.pop(t.thread_id, None)
                for mid in t.message_ids:
                    self._message_to_thread.get(channel_id_value, {}).pop(mid, None)
