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
from ollama_bot.common.bot_identity import BotIdentity, is_bot_name_mentioned
from ollama_bot.common.discord_helpers import (
    author_id,
    channel_id,
    content,
    context_boundary_of,
    display_name,
    is_context_item_after_boundary,
    message_created_at_ts,
    to_context_turn,
    ContextTurn,
)

log = logging.getLogger("harness.agent.prompt_manager.thread_tracker")


def _extract_thread_topic_label(
    texts: list[str],
    max_length: int = 30,
    *,
    origin_topic: Optional[str] = None,
) -> str:
    """メッセージ群からスレッドのトピックラベル（名詞・キーワード・返信関係）を決定論的に抽出する。"""
    if not texts:
        return "雑談"

    # 一般的な接続詞や無関係語を除外
    stop_words = {
        "それ", "これ", "あれ", "どれ", "なん", "こと", "もの", "そう", "どう", "どこ", "いつ", "だれ", "誰",
        "本当", "自分", "相手", "今日", "明日", "昨日", "返信", "会話", "発言", "理由", "ところ", "わけ", "やつ", "感じ",
    }

    # 返信プレフィックスの検出
    reply_target: Optional[str] = None
    parent_quote_topic: Optional[str] = None
    cleaned_texts: list[str] = []

    for text in texts:
        m = re.search(r"^[（(](.+?)(?:の「(.+?)」)?への返信[）)]\s*", text)
        if m:
            if not reply_target:
                reply_target = m.group(1).strip()
            quote = m.group(2)
            if quote and not parent_quote_topic:
                q_katakana = re.findall(r"[ァ-ヶヴー]{2,}", quote)
                q_kanji = re.findall(r"[\u4e00-\u9fff]{2,}(?:[\u3041-\u309f]{1,2})?", quote)
                q_cand = [w for w in (q_katakana[:2] + q_kanji[:2]) if w not in stop_words and len(w) >= 2]
                if q_cand:
                    parent_quote_topic = "・".join(dict.fromkeys(q_cand))
            cleaned_texts.append(text[m.end():].strip())
        else:
            cleaned_texts.append(text)

    # 直近テキストから主要名詞を抽出
    combined = " ".join(cleaned_texts[-5:]) if cleaned_texts else " ".join(texts[-5:])

    # 1. カタカナ連続（2文字以上、固有名詞）
    katakana = re.findall(r"[ァ-ヶヴー]{2,}", combined)
    # 2. 漢字+ひらがな語句（2文字以上）
    kanji_words = re.findall(r"[\u4e00-\u9fff]{2,}(?:[\u3041-\u309f]{1,2})?", combined)

    filtered_kanji = [w for w in kanji_words if w not in stop_words]
    filtered_katakana = [w for w in katakana if w not in stop_words and w != "ーー"]

    candidates = list(dict.fromkeys(filtered_katakana[:2] + filtered_kanji[:3]))

    effective_origin = origin_topic or parent_quote_topic

    # 返信がある場合のラベル生成
    if reply_target:
        clean_target = re.sub(r"[さんくん君ちゃん氏様]+$", "", reply_target).strip() or reply_target
        if effective_origin:
            label = f"{clean_target}への返信（親話題: {effective_origin}）"
            if len(label) <= max_length:
                return label
            label = f"{clean_target}への返信（{effective_origin}）"
            if len(label) <= max_length:
                return label
            avail = max_length - len(f"{clean_target}への返信（）")
            if avail > 3:
                return f"{clean_target}への返信（{effective_origin[:avail]}）"
            return f"{clean_target}への返信"[:max_length]
        elif candidates:
            cand_str = "・".join(candidates[:2])
            label = f"{clean_target}への返信: {cand_str}"
            if len(label) <= max_length:
                return label
            return label[:max_length]
        else:
            return f"{clean_target}への返信"[:max_length]

    # 通常のキーワード結合
    if candidates:
        label = "・".join(candidates[:3])[:max_length]
        return label or "雑談"

    # 候補が取れなければ最初の発言から要約
    first_clean = re.sub(r"^[（(].+?[）)]\s*", "", texts[0]).strip()
    first_clean = re.sub(r"^(?:ここから|それでは|さて|えーっと|あの|うーん|あー)[、,\s]*", "", first_clean)
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
    # turns と同じ並びで各ターンの発言者表示名を保持する（境界スナップショット時の参加者再計算用）
    turn_author_names: list[str] = field(default_factory=list)
    parent_thread_id: Optional[str] = None
    origin_topic: Optional[str] = None
    reply_target_user: Optional[str] = None

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
        self.turn_author_names.append(author_name or "")
        now_ts = turn.get("created_at_ts") or time.time()
        if now_ts > self.last_active_ts:
            self.last_active_ts = float(now_ts)

        # トピックの更新
        texts = [str(t.get("content", "") or "") for t in self.turns if t.get("content")]
        self.topic = _extract_thread_topic_label(texts, origin_topic=self.origin_topic)

    def turns_until(
        self,
        *,
        boundary_id: int | None,
        boundary_ts: float | None,
        include_boundary_message: bool = True,
    ) -> list[ContextTurn]:
        """返信対象メッセージ（境界）以前のターンのみを返す（未来メッセージ混入防止）。"""
        if boundary_id is None and boundary_ts is None:
            return list(self.turns)
        return [
            t for t in self.turns
            if not is_context_item_after_boundary(
                t,
                boundary_id=boundary_id,
                boundary_ts=boundary_ts,
                exclude_boundary_message=not include_boundary_message,
            )
        ]

    def snapshot_until(
        self,
        *,
        boundary_id: int | None,
        boundary_ts: float | None,
        include_boundary_message: bool = True,
    ) -> "ConversationThread":
        """返信対象メッセージ以前のターンだけで構成したスレッドの読み取り用コピーを返す。

        返信待機中（Middle-LLM 分析中など）に届いた後続メッセージは ``record_message`` で
        元スレッドへ追加され続けるため、プロンプト構築時はこのスナップショットを使う。
        トピック・参加者・最終活動時刻も境界内のターンのみから再計算する（元スレッドは変更しない）。
        """
        if boundary_id is None and boundary_ts is None:
            return self
        kept_idx = [
            i for i, t in enumerate(self.turns)
            if not is_context_item_after_boundary(
                t,
                boundary_id=boundary_id,
                boundary_ts=boundary_ts,
                exclude_boundary_message=not include_boundary_message,
            )
        ]
        if len(kept_idx) == len(self.turns):
            return self

        kept_turns = [self.turns[i] for i in kept_idx]
        names_aligned = len(self.turn_author_names) == len(self.turns)
        kept_names: list[str] = []
        if names_aligned:
            for i in kept_idx:
                name = self.turn_author_names[i]
                if name and name not in kept_names:
                    kept_names.append(name)
        else:
            turn_names = {str(t.get("name") or "") for t in kept_turns}
            kept_names = [n for n in self.participant_names if n in turn_names]

        kept_ids = {
            aid for t in kept_turns
            if isinstance((aid := t.get("author_id")), int)
        }
        kept_mids = [mid for t in kept_turns if isinstance((mid := t.get("id")), int) and mid]
        ts_values = [float(ts) for t in kept_turns if (ts := t.get("created_at_ts")) is not None]
        if ts_values:
            last_ts = max(ts_values)
        elif boundary_ts is not None:
            last_ts = min(self.last_active_ts, boundary_ts)
        else:
            last_ts = self.last_active_ts
        texts = [str(t.get("content", "") or "") for t in kept_turns if t.get("content")]

        return ConversationThread(
            thread_id=self.thread_id,
            channel_id=self.channel_id,
            root_message_id=self.root_message_id,
            message_ids=kept_mids,
            participant_ids=kept_ids,
            participant_names=kept_names,
            turns=kept_turns,
            topic=_extract_thread_topic_label(texts, origin_topic=self.origin_topic) if texts else "雑談",
            last_active_ts=last_ts,
            is_bot_participating=self.is_bot_participating,
            turn_author_names=[self.turn_author_names[i] for i in kept_idx] if names_aligned else [],
            parent_thread_id=self.parent_thread_id,
            origin_topic=self.origin_topic,
            reply_target_user=self.reply_target_user,
        )


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
        parent_thread: Optional[ConversationThread] = None

        if isinstance(ref_mid, int) and ref_mid in msg_map:
            p_tid = msg_map[ref_mid]
            parent_thread = channel_threads.get(p_tid)
            target_thread = parent_thread

        # 返信先ユーザー名（reply_target_user）の抽出（0ms）
        reply_target_name: Optional[str] = None
        if ref is not None:
            resolved_msg = getattr(ref, "resolved", None)
            if resolved_msg is not None and not isinstance(resolved_msg, getattr(discord, "DeletedReferencedMessage", ())):
                cand_name = display_name(resolved_msg)
                if isinstance(cand_name, str) and not cand_name.startswith("<MagicMock"):
                    reply_target_name = cand_name
        if not reply_target_name and parent_thread is not None and isinstance(ref_mid, int):
            for t_turn in parent_thread.turns:
                if t_turn.get("id") == ref_mid:
                    reply_target_name = str(t_turn.get("name") or "")
                    break
        if not reply_target_name and isinstance(ref_mid, int):
            for other_t in channel_threads.values():
                for t_turn in other_t.turns:
                    if t_turn.get("id") == ref_mid:
                        reply_target_name = str(t_turn.get("name") or "")
                        if parent_thread is None:
                            parent_thread = other_t
                        break
                if reply_target_name:
                    break
        if not reply_target_name:
            m_reply = re.search(r"^[（(](.+?)(?:の「.+?」)?への返信[）)]", msg_text)
            if m_reply:
                reply_target_name = m_reply.group(1).strip()

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

        # 親スレッドの因果関係（親話題・親スレッドID）および返信先ユーザーの伝播
        if parent_thread is not None:
            inherited_origin = parent_thread.origin_topic or parent_thread.topic
            if target_thread.thread_id != parent_thread.thread_id:
                # 独立スレッドとして分岐した場合でも親の話題・親スレッドIDを引き継ぐ
                target_thread.parent_thread_id = parent_thread.thread_id
                target_thread.origin_topic = inherited_origin
            else:
                # 親スレッドと同一スレッドの場合でも親の親スレッドIDがあれば保持
                if parent_thread.parent_thread_id and not target_thread.parent_thread_id:
                    target_thread.parent_thread_id = parent_thread.parent_thread_id
                if inherited_origin and not target_thread.origin_topic:
                    target_thread.origin_topic = inherited_origin

        if reply_target_name and not target_thread.reply_target_user:
            target_thread.reply_target_user = reply_target_name

        # ターンを追加
        if turn is not None:
            target_thread.add_turn(turn, author_user_id=aid, author_name=a_name, is_bot=is_bot)
        msg_map[mid] = target_thread.thread_id

        log.debug(
            "[prompt-manager] ThreadTracker matched message %s to thread %s (topic=%r, turns=%d)",
            mid,
            target_thread.thread_id,
            target_thread.topic,
            len(target_thread.turns),
        )

        self.prune_stale_threads(cid)
        return target_thread

    def get_message_origin_context(
        self,
        message: discord.Message,
        *,
        bot_user_id: int | None = None,
        bot_identity: BotIdentity | None = None,
    ) -> dict[str, str]:
        """メッセージが属するスレッドおよび返信元親スレッドから、発生文脈情報を返す（計算時間0ms）。

        戻り値:
            {
                "current_topic": "現在のスレッドの話題",
                "origin_topic": "返信元・派生元の話題（あれば）",
                "reply_target": "返信先ユーザー名（あれば）",
                "summary": "【文脈要約】〇〇スレッドの△△への返信 等"
            }
        """
        cid = channel_id(message)
        mid = getattr(message, "id", None)
        current_topic = "雑談"
        origin_topic = ""
        reply_target = ""

        matched_thread: Optional[ConversationThread] = None
        if cid is not None and isinstance(mid, int):
            msg_map = self._message_to_thread.get(cid, {})
            if mid in msg_map:
                tid = msg_map[mid]
                matched_thread = self._threads.get(cid, {}).get(tid)

        if matched_thread is None:
            matched_thread = self.match_thread(message, bot_user_id=bot_user_id, bot_identity=bot_identity)

        if matched_thread is not None:
            current_topic = matched_thread.topic or "雑談"
            origin_topic = matched_thread.origin_topic or ""
            reply_target = matched_thread.reply_target_user or ""

        # reply_target がまだ空の場合、メッセージから直接抽出
        if not reply_target:
            ref = getattr(message, "reference", None)
            if ref is not None:
                resolved_msg = getattr(ref, "resolved", None)
                if resolved_msg is not None and not isinstance(resolved_msg, getattr(discord, "DeletedReferencedMessage", ())):
                    cand_name = display_name(resolved_msg)
                    if isinstance(cand_name, str) and not cand_name.startswith("<MagicMock"):
                        reply_target = cand_name
            if not reply_target:
                m_reply = re.search(r"^[（(](.+?)(?:の「.+?」)?への返信[）)]", content(message))
                if m_reply:
                    reply_target = m_reply.group(1).strip()

        # 要約（summary）の組み立て
        if origin_topic and reply_target:
            summary = f"【文脈要約】『{origin_topic}』スレッドの『{reply_target}』さんへの返信"
        elif reply_target:
            summary = f"【文脈要約】『{reply_target}』さんへの返信"
        elif origin_topic:
            summary = f"【文脈要約】『{origin_topic}』スレッドから派生した話題"
        else:
            summary = f"【文脈要約】トピック: 『{current_topic}』"

        return {
            "current_topic": current_topic,
            "origin_topic": origin_topic,
            "reply_target": reply_target,
            "summary": summary,
        }

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

    def get_active_threads(
        self,
        channel_id_value: int,
        max_age_sec: float | None = None,
        *,
        before_message: Any = None,
    ) -> list[ConversationThread]:
        """指定チャンネル内のアクティブなスレッドを最終活動日時の新しい順で返す。

        ``before_message`` を指定した場合、そのメッセージ（返信対象）より後に届いたターンを
        除外したスナップショットを返し、境界内にターンが残らないスレッドは除外する。
        """
        age_limit = max_age_sec if max_age_sec is not None else self.thread_max_idle_sec
        channel_threads = self._threads.get(channel_id_value, {})
        now = time.time()
        active = [
            t for t in channel_threads.values()
            if (now - t.last_active_ts) <= age_limit and len(t.turns) >= 1
        ]
        if before_message is not None:
            boundary_id, boundary_ts = context_boundary_of(before_message)
            active = [
                snap for t in active
                if len((snap := t.snapshot_until(boundary_id=boundary_id, boundary_ts=boundary_ts)).turns) >= 1
            ]
        active.sort(key=lambda t: t.last_active_ts, reverse=True)
        return active

    def get_parallel_threads(
        self,
        channel_id_value: int,
        current_thread_id: str,
        max_age_sec: float | None = None,
        *,
        before_message: Any = None,
    ) -> list[ConversationThread]:
        """現在対話中のスレッド以外の、同一チャンネル内で並行稼働している別スレッド群を返す。"""
        all_active = self.get_active_threads(channel_id_value, max_age_sec=max_age_sec, before_message=before_message)
        return [t for t in all_active if t.thread_id != current_thread_id and len(t.turns) >= 2]

    @staticmethod
    def get_thread_turns(
        thread: ConversationThread,
        *,
        before_message: Any = None,
        include_boundary_message: bool = True,
    ) -> list[ContextTurn]:
        """スレッドのターンを、返信対象メッセージ（``before_message``）を上限として返す。"""
        if before_message is None:
            return list(thread.turns)
        boundary_id, boundary_ts = context_boundary_of(before_message)
        return thread.turns_until(
            boundary_id=boundary_id,
            boundary_ts=boundary_ts,
            include_boundary_message=include_boundary_message,
        )

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
