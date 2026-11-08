"""マルチスレッド・並行会話トピック追跡モジュール (ThreadTracker)。

Discordチャンネル内で複数のユーザー同士が並行して会話している状況において、
メッセージを会話スレッド単位に自律分類・追跡し、並行トピックの混同を防ぐ。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, cast

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


_ASCII_KEYWORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{1,}")

_ASCII_STOP_WORDS = frozenset({
    "http", "https", "com", "net", "org", "www", "html", "htm", "png", "jpg", "jpeg",
    "gif", "webp", "mp4", "mov", "bot", "user", "assistant", "true", "false", "none",
    "ok", "ng", "id", "dm", "vc", "pc",
})

_TOPIC_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"nai", "novelai", "ノベルai", "ノベルエーアイ"}),
    frozenset({"comfy", "comfyui", "コンフィ", "コンフィui"}),
    frozenset({"pixai", "ピクスai", "ピクスエーアイ"}),
    frozenset({"lora", "lola", "ローラ"}),
    frozenset({"muse", "ミューズ"}),
    frozenset({"gemini", "ジェミニ"}),
    frozenset({"sd", "stablediffusion", "生成ai", "画像生成", "ローカル画像生成"}),
    frozenset({"tokioka", "時岡", "時オカ", "時のオカリナ"}),
)

_TOPIC_OPENER_RE = re.compile(
    r"^(.{2,24}?)(?:ってさ[あぁー]*|(?<![ァ-ヶー])さ[あぁー]+|なんだけど(?:さ[あぁー]*)?|だけど(?:さ[あぁー]*)?|の話(?:だけど|なんだけど|さ[あぁー]*)?|ってどうよ|ってどうなの)[!！?？…・\s]*$",
    re.IGNORECASE,
)


def looks_like_incomplete_topic_opener(text: str) -> bool:
    """『novelaiさあ』『〇〇ってさ』『〇〇なんだけど』のような、後続の連投で本論が続く話題切り出し導入短文かを判定する。"""
    raw = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", str(text or "")).strip()
    raw = re.sub(r"<@!?\d+>", "", raw).strip()
    if not raw or len(raw) > 24 or "\n" in raw:
        return False
    return bool(_TOPIC_OPENER_RE.match(raw))


def _expand_topic_synonyms(keywords: set[str]) -> set[str]:
    """抽出されたキーワード集合に対し、大文字小文字正規化と同義語・略称グループ展開を行う。"""
    if not keywords:
        return set()
    expanded: set[str] = set()
    lowered_map: set[str] = set()
    for kw in keywords:
        clean = str(kw or "").strip()
        if not clean:
            continue
        expanded.add(clean)
        low = clean.lower()
        expanded.add(low)
        lowered_map.add(low)

    for group in _TOPIC_SYNONYM_GROUPS:
        if lowered_map & group:
            expanded.update(group)
    return expanded


def _extract_message_topic_keywords(text: str) -> set[str]:
    """単一メッセージから英数字・カタカナ・漢字のトピックキーワードを抽出し同義語展開して返す。"""
    raw = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", str(text or "")).strip()
    raw = re.sub(r"https?://\S+", " ", raw)
    if not raw:
        return set()
    stop_words = {
        "それ", "これ", "あれ", "どれ", "なん", "こと", "もの", "そう", "どう", "どこ", "いつ", "だれ", "誰",
        "本当", "自分", "相手", "今日", "明日", "昨日", "最近", "普通", "返信", "会話", "発言", "理由", "ところ", "わけ", "やつ", "感じ",
        "雑談", "不明", "微妙", "完全", "的確", "強化", "不気味",
    }
    # 話題切り出し末尾（「〜さあ」「〜ってさ」等）を除去してから抽出
    m_opener = _TOPIC_OPENER_RE.match(raw)
    target_text = f"{m_opener.group(1)} {raw}" if m_opener else raw

    kws: set[str] = set()
    for w in _ASCII_KEYWORD_RE.findall(target_text):
        w_low = w.lower()
        if w_low not in _ASCII_STOP_WORDS and len(w_low) >= 2:
            kws.add(w)
            kws.add(w_low)
    for w in re.findall(r"[ァ-ヶヴー]{2,}", target_text):
        if w not in stop_words and w != "ーー":
            kws.add(w)
    for w in re.findall(r"[\u4e00-\u9fff]{2,}", target_text):
        if w not in stop_words and not any(w.startswith(sw) for sw in ("今日", "明日", "昨日", "最近", "自分", "普通")):
            kws.add(w)
    return _expand_topic_synonyms(kws)


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
        "本当", "自分", "相手", "今日", "明日", "昨日", "最近", "普通", "返信", "会話", "発言", "理由", "ところ", "わけ", "やつ", "感じ",
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
                q_kanji = re.findall(r"[\u4e00-\u9fff]{2,}(?:[いきしちにひみりぎじびぴえけせてねへめれげぜべぺうくすつぬふむるぐずぶぷっん])?", quote)
                q_cand = [
                    w for w in (q_katakana[:2] + q_kanji[:2])
                    if w not in stop_words and len(w) >= 2 and not any(w.startswith(sw) for sw in ("今日", "明日", "昨日", "最近", "自分", "普通"))
                ]
                if q_cand:
                    parent_quote_topic = "・".join(dict.fromkeys(q_cand))
            cleaned_texts.append(text[m.end():].strip())
        else:
            cleaned_texts.append(text)

    # 直近テキストから主要名詞を抽出
    combined = " ".join(cleaned_texts[-5:]) if cleaned_texts else " ".join(texts[-5:])

    # 1. カタカナ連続（2文字以上、固有名詞）
    katakana = re.findall(r"[ァ-ヶヴー]{2,}", combined)
    # 2. 英数字キーワード（2文字以上、NAI / ComfyUI / novelai / Muse 等）
    ascii_words = [
        w for w in _ASCII_KEYWORD_RE.findall(combined)
        if w.lower() not in _ASCII_STOP_WORDS and len(w) >= 2
    ]
    # 3. 漢字+ひらがな語句（2文字以上。助詞を巻き込まず、熟語または活用語尾に限定）
    kanji_words = re.findall(r"[\u4e00-\u9fff]{2,}(?:[いきしちにひみりぎじびぴえけせてねへめれげぜべぺうくすつぬふむるぐずぶぷっん])?", combined)

    filtered_kanji = [
        w for w in kanji_words
        if w not in stop_words and not any(w.startswith(sw) for sw in ("今日", "明日", "昨日", "最近", "自分", "普通"))
    ]
    filtered_katakana = [w for w in katakana if w not in stop_words and w != "ーー"]

    candidates = list(dict.fromkeys(filtered_katakana[:2] + ascii_words[:2] + filtered_kanji[:3]))

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


_DEMONSTRATIVE_PATTERN = re.compile(
    r"(?:これ|それ|あれ|どれ|この|その|あの|どの|こんな|そんな|あんな|どんな|こっち|そっち|あっち|どっち|ここ|そこ|あそこ|どこ)"
)

_PRONOUN_PATTERN = re.compile(
    r"(?:俺|おれ|僕|ぼく|私|わたし|あたし|自分|うち|ワイ|わい|お前|あんた|君|あなた|あいつ|こいつ|そいつ|彼|彼女)(?:も|だって|なら|は|って|の(?:こと|場合))"
)

_REACTIVE_AGREEMENT_PATTERN = re.compile(
    r"(?:本当|ほんと|確か|たしか|そう(?:だ|です|かも|だね|だよ|ですね|な|よな|だった|思う|おもう)|"
    r"賛成|同感|同じ|同意|納得|"
    r"安[いく]|安かっ|高[いく]|高かっ|うまい|美味い|まずい|すご[い期]|凄[い]|ヤバ|やば|草|なるほど|そっか|そうか|わかる|それな|まじ|マジ|いい|良い|ダメ|だめ)"
)

_CONTEXT_DEPENDENT_PATTERN = re.compile(
    r"(?:"
    r"言うほど|いうほど|そこまで|そんなに|"
    r"[くぐ]らい|(?<![ん])だけ(?!ど|れど)|(?<!た)しか(?!し)|"
    r"(?<![てで])(?:居る|いる|おる|ある)(?:か(?!も)|のか|んか|っけ|んだけど|んだが|んだけどな)|"
    r"だろ(?:う)?|じゃね[えぇよ]?|わけ[な無]|なくない|じゃな[いく]|"
    r"ちゃう(?:か|な)?|だっけ|ってこと|じゃん|"
    r"養っ|生計|飯を食|飯食っ"
    r")"
)


def _extract_thread_keywords(thread: ConversationThread) -> set[str]:
    """スレッドの話題や直近発言から主要な名詞・キーワードを抽出する。"""
    stop_words = {
        "それ", "これ", "あれ", "どれ", "なん", "こと", "もの", "そう", "どう", "どこ", "いつ", "だれ", "誰",
        "本当", "自分", "相手", "今日", "明日", "昨日", "返信", "会話", "発言", "理由", "ところ", "わけ", "やつ", "感じ",
        "雑談", "不明",
    }
    keywords: set[str] = set()

    # 1. topic から抽出
    if thread.topic and thread.topic != "雑談":
        for part in re.split(r"[・\s（）()「」:：、/]+", thread.topic):
            w = part.strip()
            if len(w) >= 2 and w not in stop_words:
                keywords.add(w)

    # 2. origin_topic から抽出
    if thread.origin_topic:
        for part in re.split(r"[・\s（）()「」:：、/]+", thread.origin_topic):
            w = part.strip()
            if len(w) >= 2 and w not in stop_words:
                keywords.add(w)

    # 3. 直近ターン（最大5ターン）の発言テキストから抽出
    recent_texts: list[str] = []
    for turn in thread.turns[-5:]:
        c = str(turn.get("content") or "").strip()
        if c:
            recent_texts.append(c)

    if recent_texts:
        combined = " ".join(recent_texts)
        # 英数字語（2文字以上: NAI, novelai, ComfyUI, Muse 等）
        for w in _ASCII_KEYWORD_RE.findall(combined):
            if w.lower() not in _ASCII_STOP_WORDS and len(w) >= 2:
                keywords.add(w)
                keywords.add(w.lower())
        # カタカナ語（2文字以上）
        for w in re.findall(r"[ァ-ヶヴー]{2,}", combined):
            if w not in stop_words and w != "ーー":
                keywords.add(w)
        # 漢字語（2文字以上）
        for w in re.findall(r"[\u4e00-\u9fff]{2,}", combined):
            if w not in stop_words:
                keywords.add(w)

    return _expand_topic_synonyms(keywords)


def _is_independent_new_topic(
    msg_text: str,
    thread: ConversationThread,
) -> bool:
    """メッセージが直前スレッドとは無関係な独立した新トピックであるかを判定する。

    同一話者または直近活動スレッドへの追従候補であっても、
    - 直前スレッドのキーワードを含まない
    - 指示語（これ・それ等）や文脈依存表現（程度・限定・疑問・反語・ツッコミ等）がない
    - 発言が20文字以上の自立した文である
    を満たす場合は、安易に過去スレッドにマージせず独立スレッドとして分離する。
    ただし、25文字以下の短い発言で相槌・感想・受容表現を含む場合や、
    文脈依存表現（「言うほど」「そんないるか」「ルフィぐらいじゃね」等）を含む場合は、
    キーワード不一致でもスレッド継続とする。
    また、『novelaiさあ』『〇〇ってさ』等の話題切り出し構文で、直前スレッドとキーワードが
    一致しない場合は20文字未満でも独立トピックと判定する。
    """
    raw = str(msg_text or "").strip()
    if not raw:
        return False

    clean_msg = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw).strip()

    # 代名詞（俺、お前、あいつ等）が含まれる場合は会話の掛け合いへの参加とみなしスレッド継続
    if _PRONOUN_PATTERN.search(clean_msg):
        return False

    # 指示語（これ、それ、そんな等）が含まれる場合は文脈依存の発言とみなしマージ許容
    if _DEMONSTRATIVE_PATTERN.search(clean_msg):
        return False

    # 文脈依存表現（程度・限定・比較・疑問・反語・ツッコミ等）が含まれる場合はスレッド継続
    if _CONTEXT_DEPENDENT_PATTERN.search(clean_msg):
        return False

    # 25文字以下の短い発言で相槌・感想・受容表現（「本当に」「確かに」「安い」「そうだよな」「そう思う」等）を含む場合はスレッド継続
    if len(clean_msg) <= 25 and _REACTIVE_AGREEMENT_PATTERN.search(clean_msg):
        return False

    # 直前スレッドのキーワード（同義語展開済み）とメッセージのキーワード照合
    thread_keywords = _extract_thread_keywords(thread)
    msg_keywords = _extract_message_topic_keywords(clean_msg)
    lowered_msg = clean_msg.lower()
    if thread_keywords:
        if msg_keywords and (thread_keywords & msg_keywords):
            return False
        for kw in thread_keywords:
            if kw.lower() in lowered_msg:
                return False

    # 『novelaiさあ』『〇〇ってさ』等の話題切り出し構文で、直前スレッドとキーワードが一致しない場合は20文字未満でも独立トピックと判定
    if looks_like_incomplete_topic_opener(clean_msg) and msg_keywords and thread_keywords:
        return True

    # 20文字未満の短い発言（相槌・ツッコミ・短い疑問等）は原則スレッド継続として許容
    if len(clean_msg) < 20:
        return False

    # キーワードを含まず、指示語・文脈依存表現もなく、20文字以上の自立した文であるため独立スレッドと判定
    return True


class ThreadTracker:
    """Discordチャンネル内の複数会話スレッドを管理・分類するトラッカー。"""

    def __init__(self, max_active_threads_per_channel: int = 5, thread_max_idle_sec: float = 900.0) -> None:
        self.max_active_threads = max_active_threads_per_channel
        self.thread_max_idle_sec = thread_max_idle_sec
        # channel_id -> {thread_id: ConversationThread}
        self._threads: dict[int, dict[str, ConversationThread]] = {}
        # channel_id -> {message_id: thread_id}
        self._message_to_thread: dict[int, dict[int, str]] = {}

    def hydrate_channel_threads(
        self,
        channel_id: int,
        db_rows: list[dict[str, Any]],
    ) -> None:
        """DBから読み出したスレッド情報を元に ConversationThread を復元登録する。

        インメモリ上に該当チャンネルのアクティブスレッドが存在しない（空である）場合に、
        DB（channel_threads テーブル等）から取得したスレッド情報をインメモリへ復元する。
        """
        if not db_rows:
            return
        channel_threads = self._threads.setdefault(channel_id, {})
        msg_map = self._message_to_thread.setdefault(channel_id, {})
        if channel_threads:
            return

        for row in db_rows:
            if not isinstance(row, dict):
                continue
            thread_id = str(row.get("thread_key") or row.get("thread_id") or "").strip()
            if not thread_id or thread_id in channel_threads:
                continue

            topic = str(row.get("topic") or "雑談").strip() or "雑談"

            # last_active_ts の解決
            raw_ts = row.get("last_active_ts")
            if raw_ts is None:
                raw_ts = row.get("last_active_at")
            last_ts = 0.0
            if isinstance(raw_ts, (int, float)):
                last_ts = float(raw_ts)
            elif isinstance(raw_ts, str) and raw_ts:
                try:
                    last_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    try:
                        last_ts = float(raw_ts)
                    except Exception:
                        last_ts = 0.0

            # turns の復元
            raw_turns = row.get("turns")
            turns: list[ContextTurn] = []
            if isinstance(raw_turns, list):
                turns = cast(list[ContextTurn], [t for t in raw_turns if isinstance(t, dict)])

            message_ids: list[int] = []
            turn_author_names: list[str] = []
            participant_ids: set[int] = set()
            participant_names: list[str] = []
            is_bot = False

            for t in turns:
                mid = t.get("id")
                if isinstance(mid, int) and mid:
                    message_ids.append(mid)
                    msg_map[mid] = thread_id
                aid = t.get("author_id")
                if isinstance(aid, int):
                    participant_ids.add(aid)
                name = str(t.get("name") or "").strip()
                turn_author_names.append(name)
                if name and name not in participant_names:
                    participant_names.append(name)
                if t.get("is_bot") or t.get("role") == "assistant":
                    is_bot = True

            # participants カラムの補完
            raw_parts = row.get("participants")
            if isinstance(raw_parts, list):
                for p in raw_parts:
                    if isinstance(p, int):
                        participant_ids.add(p)
                    elif isinstance(p, str) and p.isdigit():
                        participant_ids.add(int(p))
                    elif isinstance(p, str) and p.strip():
                        if p.strip() not in participant_names:
                            participant_names.append(p.strip())

            thread = ConversationThread(
                thread_id=thread_id,
                channel_id=channel_id,
                message_ids=message_ids,
                participant_ids=participant_ids,
                participant_names=participant_names,
                turns=turns,
                topic=topic,
                last_active_ts=last_ts,
                is_bot_participating=is_bot,
                turn_author_names=turn_author_names,
            )
            channel_threads[thread_id] = thread

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
                now = message_created_at_ts(message) or time.time()
                for t in reversed(list(channel_threads.values())):
                    if (now - t.last_active_ts) <= self.thread_max_idle_sec:
                        if aid is not None and aid in t.participant_ids and t.is_bot_participating:
                            target_thread = t
                            break
                # 直近60秒以内に活動があったチャンネル内の進行中スレッドがあれば追従（グループ会話の分断防止）
                if target_thread is None:
                    recent_active = [
                        t for t in channel_threads.values()
                        if (now - t.last_active_ts) <= 60.0
                        and ((aid is not None and aid in t.participant_ids) or not t.reply_target_user or t.is_bot_participating)
                    ]
                    if recent_active:
                        target_thread = max(recent_active, key=lambda t: t.last_active_ts)
                        target_thread.is_bot_participating = True
                if target_thread is None:
                    new_tid = f"bot_{aid or 'anon'}_{mid}"
                    target_thread = ConversationThread(
                        thread_id=new_tid,
                        channel_id=cid,
                        root_message_id=mid,
                        is_bot_participating=True,
                    )
                    channel_threads[new_tid] = target_thread

        # 4. 発言者の時間的連続性・グループ会話の追従および先行トピックスレッドへの再合流
        if target_thread is None and aid is not None:
            now = message_created_at_ts(message) or time.time()
            clean_msg = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", str(msg_text or "")).strip()
            is_short_reactive = (
                len(clean_msg) <= 25
                and _REACTIVE_AGREEMENT_PATTERN.search(clean_msg) is not None
            )
            sorted_threads = sorted(channel_threads.values(), key=lambda t: t.last_active_ts, reverse=True)

            # 4-A. 先行アクティブスレッドへのキーワード・同義語再合流（Re-attachment）:
            # 間に別ユーザーの単発別話題（例: 時岡リメイク）が挟まっていても、直近 thread_max_idle_sec（900秒）以内の
            # 先行スレッド（例: NAI / ComfyUI）と固有キーワード（NAI ⇔ novelai 等）が一致する場合は、その先行スレッドへ再合流させる。
            msg_keywords = _extract_message_topic_keywords(clean_msg)
            if msg_keywords:
                best_kw_thread: Optional[ConversationThread] = None
                best_overlap_score = 0
                for t in sorted_threads:
                    if (now - t.last_active_ts) > self.thread_max_idle_sec:
                        continue
                    t_keywords = _extract_thread_keywords(t)
                    overlap = msg_keywords & t_keywords
                    if overlap:
                        # 同一話者が既に参加しているスレッドにはボーナスを付与
                        score = len(overlap) * 10 + (5 if aid in t.participant_ids else 0)
                        if score > best_overlap_score:
                            best_overlap_score = score
                            best_kw_thread = t
                if best_kw_thread is not None:
                    target_thread = best_kw_thread

            # 4-B. 直近60秒以内に同一話者が直前に話題切り出し連投（バースト）を行っているスレッドへの追従
            if target_thread is None:
                for t in sorted_threads:
                    if (now - t.last_active_ts) <= 60.0 and t.turns:
                        last_turn = t.turns[-1]
                        if last_turn.get("author_id") == aid and not _is_independent_new_topic(msg_text, t):
                            target_thread = t
                            break

            # 4-C. 直近300秒（5分）以内に活動があった最新スレッドへの追従
            if target_thread is None:
                for t in sorted_threads:
                    if (now - t.last_active_ts) <= 300.0 and (
                        aid in t.participant_ids
                        or not t.reply_target_user
                        or t.is_bot_participating
                        or is_short_reactive
                    ):
                        if _is_independent_new_topic(msg_text, t):
                            continue
                        target_thread = t
                        break

            # 直近120秒以内に同一話者が参加しているスレッド
            if target_thread is None:
                for t in sorted_threads:
                    if (now - t.last_active_ts) <= 120.0 and aid in t.participant_ids:
                        if _is_independent_new_topic(msg_text, t):
                            continue
                        target_thread = t
                        break

            # Bot自身の発言の場合、直近アクティブなBot参加スレッドに追従
            if target_thread is None and is_bot:
                for t in sorted_threads:
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
        exclude_thread_ids: set[str] | None = None,
    ) -> list[ConversationThread]:
        """現在対話中のスレッド以外の、同一チャンネル内で並行稼働している別スレッド群を返す。"""
        all_active = self.get_active_threads(channel_id_value, max_age_sec=max_age_sec, before_message=before_message)
        channel_threads = self._threads.get(channel_id_value, {})
        excluded = {current_thread_id}
        if exclude_thread_ids:
            excluded.update(exclude_thread_ids)

        # 親・祖先スレッドの追跡・除外（ループ防止ガード付き）
        to_check = list(excluded)
        visited: set[str] = set()
        while to_check:
            tid = to_check.pop(0)
            if tid in visited:
                continue
            visited.add(tid)
            th = channel_threads.get(tid)
            if th and th.parent_thread_id and th.parent_thread_id not in excluded:
                excluded.add(th.parent_thread_id)
                to_check.append(th.parent_thread_id)

        return [t for t in all_active if t.thread_id not in excluded and len(t.turns) >= 2]

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
