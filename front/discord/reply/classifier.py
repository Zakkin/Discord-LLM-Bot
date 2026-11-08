"""直接返信 (message.reply) か通常送信 (channel.send / エアリプ) かを決定論的に判定する分類エンジン、
および推論遅延によるステール（陳腐化）メッセージの送信抑止判定。

サイコロ抽選（DIRECT_REPLY_RATE）を完全廃止し、以下の基準を総合的に評価して決定する：
1. マスターフラグ（ALWAYS_CHANNEL_SEND, ALWAYS_DIRECT_REPLY）
2. 物理的・システム的距離基準（経過時間60秒以上でIn-reply-to必須、挟まりメッセージ数 gap、is_directトリガーとマスコット例外）
3. 文脈・トピック混線度基準（並行話題・別ユーザー割り込み検知、主語の明確さ・自己完結性）
4. 発言のスタンス・温度感基準（対話・呼びかけ・疑問形 vs 茶々入れ・独り言・共感）
5. デフォルト基準（直前会話ならエアリプ優先）
6. ステール検知（is_stale_reply: 自発返信で120秒超かつ新規人間メッセージ2件以上でドロップ）
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Sequence

import discord

from lib.config_utils import cfg_bool, cfg_float, cfg_int
from front.discord.utils import author_id, channel_id, content, display_name, message_created_at_ts

log = logging.getLogger("ollama_bot.reply_classifier")

# マスコット的怯えリアクション極短文の判定パターン（通知を避けるためエアリプ許容）
_MASCOT_FEAR_PATTERNS = re.compile(
    r"^(?:ぴゃ[ぁ-んっー!！？…\s]*|ひ[ぃいぇえっー!！？…\s]+|うぅ[…っー!！？\s]*|ふぇ[ぇえっー!！？…\s]*|ひえ[ぇえっー!！？…\s]*|きゃ[ぁあっー!！？…\s]*|わ[ぁあっー!！？…\s]*)$"
)

# 指示代名詞や文脈依存プレフィックス（自己完結していない開始語句）
_DEMONSTRATIVE_PREFIX_PATTERNS = re.compile(
    r"^(?:それ|あれ|これ|どれ|ダメ[だようよ！!]|ちがう[よ]?|違う[よ]?|そう[だよね！!]|ほんと[に]?|ホント[に]?)"
)

# 独り言・共感・便乗語尾パターン（1対多の茶々入れ）
_AMBIENT_COMMENTARY_PATTERNS = re.compile(
    r"(?:ですね|ですよね|なんだよな|だし[…・\s]*|かも[…・\s]*|そう[…・\s]*|ですし[…・\s]*|な気がする|かもしれない|わかる[…・\s]*|確かに[…・\s]*)$"
)

# 疑問形パターン
_QUESTION_PATTERNS = re.compile(
    r"(?:[?？]|ですか|ますか|どうして|なぜ|何で|何故|何が|誰が|どこで|いつ|教えて)"
)

# 強いツッコミパターン
_RETORT_PATTERNS = re.compile(
    r"(?:なんでやねん|おかしいでしょ|どういうこと|嘘でしょ|待って[くだされよ！!]|ツッコミ)"
)


def _extract_keywords(text: str) -> set[str]:
    """文章からトピック比較用の主要キーワード（カタカナ語、漢字語句）を抽出する。"""
    if not text:
        return set()
    stop_words = {
        "それ", "これ", "あれ", "どれ", "なん", "こと", "もの", "そう", "どう", "どこ", "いつ", "だれ", "誰",
        "本当", "自分", "相手", "今日", "明日", "昨日", "返信", "会話", "発言", "理由", "ところ", "わけ", "やつ", "感じ",
        "私", "僕", "俺", "あなた", "さん", "ちゃん", "くん", "君",
    }
    katakana = re.findall(r"[ァ-ヶヴー]{2,}", text)
    kanji = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return {w for w in (katakana + kanji) if w not in stop_words}


def _calculate_elapsed_sec(target_message: discord.Message) -> float:
    """対象メッセージからの経過秒数を算出する。"""
    ts = message_created_at_ts(target_message)
    if ts is not None and ts > 0:
        return max(0.0, time.time() - ts)
    created_at = getattr(target_message, "created_at", None)
    if created_at is not None and hasattr(created_at, "timestamp"):
        try:
            return max(0.0, time.time() - float(created_at.timestamp()))
        except Exception:
            pass
    return 0.0


def _calculate_gap(target_message: discord.Message, context_queue: Sequence[Any] | None) -> int:
    """context_queue 内で target_message と最新メッセージの間に何件挟まっているか（gap）を計算する。"""
    if not context_queue:
        return 0
    target_id = getattr(target_message, "id", None)
    if target_id is None:
        return 0

    found_idx = -1
    for idx in range(len(context_queue) - 1, -1, -1):
        item = context_queue[idx]
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if item_id == target_id:
            found_idx = idx
            break

    if found_idx != -1:
        return (len(context_queue) - 1) - found_idx

    oldest_item = context_queue[0]
    oldest_id = oldest_item.get("id") if isinstance(oldest_item, dict) else getattr(oldest_item, "id", None)
    if oldest_id is not None and isinstance(target_id, int) and isinstance(oldest_id, int) and target_id < oldest_id:
        return len(context_queue)

    return 0


def _is_human_turn(item: Any, bot_user_id: int | None = None) -> bool:
    """メッセージ/ターンが人間による発言であるか判定する。"""
    if isinstance(item, dict):
        role = item.get("role")
        if role == "assistant":
            return False
        aid = item.get("author_id")
        if bot_user_id is not None and aid == bot_user_id:
            return False
        return True

    # discord.Message 互換オブジェクト
    author = getattr(item, "author", None)
    if author is not None:
        if getattr(author, "bot", False):
            return False
        if bot_user_id is not None and getattr(author, "id", None) == bot_user_id:
            return False
    return True


def _calculate_human_gap(
    target_message: discord.Message,
    context_queue: Sequence[Any] | None,
    bot_user_id: int | None = None,
) -> int:
    """context_queue 内で target_message 以降に投稿された新しい人間メッセージ件数を計算する。"""
    if not context_queue:
        return 0
    target_id = getattr(target_message, "id", None)
    if target_id is None:
        return 0

    found_idx = -1
    for idx in range(len(context_queue) - 1, -1, -1):
        item = context_queue[idx]
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if item_id == target_id:
            found_idx = idx
            break

    if found_idx != -1:
        return sum(
            1
            for idx in range(found_idx + 1, len(context_queue))
            if _is_human_turn(context_queue[idx], bot_user_id=bot_user_id)
        )

    oldest_item = context_queue[0]
    oldest_id = oldest_item.get("id") if isinstance(oldest_item, dict) else getattr(oldest_item, "id", None)
    if oldest_id is not None and isinstance(target_id, int) and isinstance(oldest_id, int) and target_id < oldest_id:
        return sum(
            1 for item in context_queue
            if _is_human_turn(item, bot_user_id=bot_user_id)
        )

    return 0


def is_stale_reply(
    target_message: discord.Message | None,
    *,
    is_direct: bool = False,
    context_queue: Sequence[Any] | None = None,
    bot_user_id: int | None = None,
    elapsed_threshold: float | None = None,
    gap_threshold: int | None = None,
) -> bool:
    """推論遅延等によってメッセージが陳腐化（ステール）し、会話が先に進んでいるかを判定する。

    ドロップ条件:
      - メンション等の強制返答 (is_direct=True) 以外
      - 経過時間が 120 秒超 (STALE_REPLY_ELAPSED_THRESHOLD)
      - かつ、その間に別ユーザーまたは同一ユーザーの新しい人間メッセージが 2 件以上 (STALE_REPLY_GAP_THRESHOLD) 投稿されている
    """
    if target_message is None:
        return False

    # メンション等の強制返答は遅延してもドロップしない
    if is_direct:
        return False

    eff_elapsed_threshold = (
        elapsed_threshold
        if elapsed_threshold is not None
        else cfg_float("STALE_REPLY_ELAPSED_THRESHOLD", 120.0)
    )
    eff_gap_threshold = (
        gap_threshold
        if gap_threshold is not None
        else cfg_int("STALE_REPLY_GAP_THRESHOLD", 2)
    )

    elapsed_sec = _calculate_elapsed_sec(target_message)
    if elapsed_sec <= eff_elapsed_threshold:
        return False

    gap = _calculate_human_gap(target_message, context_queue, bot_user_id=bot_user_id)
    if gap >= eff_gap_threshold:
        log.info(
            "is_stale_reply -> True: elapsed_sec=%.1f > %.1f, human_gap=%d >= %d (stale message dropped)",
            elapsed_sec,
            eff_elapsed_threshold,
            gap,
            eff_gap_threshold,
        )
        return True

    return False


def _is_mascot_fear_short_reaction(reply_text: str) -> bool:
    """発言内容がマスコット的怯えリアクション極短文（ぴゃっ…！、ひぃ等）か判定する。"""
    clean = reply_text.strip()
    if len(clean) > 30:
        return False
    return bool(_MASCOT_FEAR_PATTERNS.search(clean))


def _is_topic_interrupted(
    target_message: discord.Message,
    context_queue: Sequence[Any],
    thread_tracker: Any | None,
) -> bool:
    """直前の発言（queue[-1]）が別ユーザーによる別トピックの割り込みであるか判定する。"""
    if not context_queue:
        return False

    latest_item = context_queue[-1]
    latest_author_id = latest_item.get("author_id") if isinstance(latest_item, dict) else getattr(getattr(latest_item, "author", None), "id", None)
    target_author_id = author_id(target_message)

    # 直前発言が同一ユーザーであれば割り込みではない
    if latest_author_id is not None and target_author_id is not None and latest_author_id == target_author_id:
        return False

    # thread_tracker によるスレッド判定
    if thread_tracker is not None:
        try:
            target_mid = getattr(target_message, "id", None)
            latest_mid = latest_item.get("id") if isinstance(latest_item, dict) else getattr(latest_item, "id", None)
            cid = channel_id(target_message)
            msg_map = getattr(thread_tracker, "_message_to_thread", {}).get(cid, {})
            t_tid = msg_map.get(target_mid)
            l_tid = msg_map.get(latest_mid)
            if t_tid and l_tid and t_tid != l_tid:
                return True
            # 並行アクティブスレッドの存在チェック
            if t_tid and hasattr(thread_tracker, "get_parallel_threads") and cid is not None:
                parallel = thread_tracker.get_parallel_threads(cid, t_tid)
                if parallel and latest_author_id != target_author_id:
                    return True
        except Exception as e:
            log.debug("thread_tracker comparison failed: %r", e)

    # キーワード連続性のチェック（フォールバック）
    latest_content = str(latest_item.get("content", "") if isinstance(latest_item, dict) else content(latest_item) or "")
    target_content = str(content(target_message) or "")

    latest_kw = _extract_keywords(latest_content)
    target_kw = _extract_keywords(target_content)

    # キーワードの重なりが全くなく、発言者も異なる場合は別トピックの割り込み
    if not (latest_kw & target_kw):
        return True

    return False


def _starts_with_demonstrative_and_lacks_context(
    reply_text: str,
    target_message: discord.Message,
) -> bool:
    """発言が指示代名詞等で始まり、元メッセージのキーワードを含んでいない（自己完結していない）か判定する。"""
    clean_reply = reply_text.strip()
    if not _DEMONSTRATIVE_PREFIX_PATTERNS.search(clean_reply):
        return False

    target_content = str(content(target_message) or "")
    target_keywords = _extract_keywords(target_content)
    if not target_keywords:
        return True

    reply_keywords = _extract_keywords(clean_reply)
    # 元メッセージのキーワードが含まれていなければ自己完結していない
    if not (target_keywords & reply_keywords):
        return True

    return False


def _is_conversational_dialogue(
    reply_text: str,
    target_message: discord.Message,
) -> bool:
    """相手への呼びかけ、質問、強いツッコミなどの1対1対話スタンスであるか判定する。"""
    # 相手の名前を呼んでいるか
    target_name = (display_name(target_message) or "").strip()
    if target_name:
        clean_name = re.sub(r"[さんくん君ちゃん氏様]+$", "", target_name).strip()
        if clean_name and len(clean_name) >= 2:
            name_call_pattern = rf"{re.escape(clean_name)}(?:さん|くん|君|ちゃん|氏|様)"
            if re.search(name_call_pattern, reply_text):
                return True

    # 文末が疑問形、または疑問語
    if _QUESTION_PATTERNS.search(reply_text):
        return True

    # 強いツッコミ
    if _RETORT_PATTERNS.search(reply_text):
        return True

    return False


def _is_ambient_commentary(reply_text: str) -> bool:
    """返信を期待しない茶々入れ・独り言・共感・便乗であるか判定する。"""
    # 疑問形や強いツッコミが含まれていないことを前提
    if _QUESTION_PATTERNS.search(reply_text) or _RETORT_PATTERNS.search(reply_text):
        return False
    return bool(_AMBIENT_COMMENTARY_PATTERNS.search(reply_text.strip()))


def decide_reply_mode(
    target_message: discord.Message | None,
    reply_text: str,
    *,
    is_direct: bool = False,
    context_queue: Sequence[Any] | None = None,
    thread_tracker: Any | None = None,
) -> bool:
    """直接返信 (True / message.reply) か通常送信 (False / エアリプ) かを決定論的に判定する。"""
    # 1. マスターフラグの確認
    if cfg_bool("ALWAYS_CHANNEL_SEND", False):
        return False
    if cfg_bool("ALWAYS_DIRECT_REPLY", False):
        return True

    # 後方互換・強制返信フラグ: DIRECT_REPLY_RATE >= 1.0 の場合は即座に True
    rate = cfg_float("DIRECT_REPLY_RATE", 0.0)
    if rate >= 1.0:
        return True

    if target_message is None:
        return False

    elapsed_sec = _calculate_elapsed_sec(target_message)
    gap = _calculate_gap(target_message, context_queue)

    # 2. 物理的・システム的距離基準
    # 経過時間（鮮度）: 60秒以上なら話題が古いため True（返信必須・エアリプ禁止）
    reply_elapsed_threshold = cfg_float("REPLY_ELAPSED_SEC_THRESHOLD", 60.0)
    if elapsed_sec >= reply_elapsed_threshold:
        log.debug("decide_reply_mode -> True: elapsed_sec=%.1f >= threshold=%.1f", elapsed_sec, reply_elapsed_threshold)
        return True

    # 挟まりメッセージ数（gap）: 4件以上挟まっていれば True（返信必須）
    reply_gap_threshold = cfg_int("REPLY_GAP_THRESHOLD", cfg_int("DIRECT_REPLY_GAP_THRESHOLD", 4))
    if gap >= reply_gap_threshold:
        log.debug("decide_reply_mode -> True: gap=%d >= threshold=%d", gap, reply_gap_threshold)
        return True

    # トリガー種別: is_direct == True
    if is_direct:
        if not cfg_bool("DIRECT_REPLY_ON_MENTION", True):
            return False
        # 例外（ウザさ回避）: gap <= 1 かつ elapsed_sec <= 120 でマスコット怯え極短文
        if gap <= 1 and elapsed_sec <= 120.0 and _is_mascot_fear_short_reaction(reply_text):
            log.debug("decide_reply_mode -> False: mascot fear reaction in direct trigger")
            return False
        return True

    # 3. 文脈・トピック混線度基準 (is_direct == False)
    # 並行話題・割り込み検知: gap in (1, 2, 3) で直前発言が別ユーザー・別トピック
    if gap in (1, 2, 3) and context_queue:
        if _is_topic_interrupted(target_message, context_queue, thread_tracker):
            log.debug("decide_reply_mode -> True: topic interrupted with gap=%d", gap)
            return True

    # 主語の明確さ（文脈の自己完結性）: 指示代名詞で始まりキーワード不足なら gap >= 1 で True
    if gap >= 1 and _starts_with_demonstrative_and_lacks_context(reply_text, target_message):
        log.debug("decide_reply_mode -> True: demonstrative prefix lacking context keywords with gap=%d", gap)
        return True

    # 4. 発言のスタンス・温度感基準
    # 対話（1対1 / 質問・強いツッコミ・相手呼びかけ）
    if _is_conversational_dialogue(reply_text, target_message):
        log.debug("decide_reply_mode -> True: conversational dialogue")
        return True

    # 茶々入れ・独り言・共感・便乗（1対多）
    if _is_ambient_commentary(reply_text):
        log.debug("decide_reply_mode -> False: ambient commentary")
        return False

    # 5. 通常デフォルト（サイコロ抽選完全廃止）
    # 直前（gap <= 1 かつ elapsed_sec < reply_elapsed_threshold）なら False（エアリプ）
    if gap <= 1 and elapsed_sec < reply_elapsed_threshold:
        log.debug("decide_reply_mode -> False: default immediate air-reply (gap=%d, elapsed=%.1f)", gap, elapsed_sec)
        return False

    log.debug("decide_reply_mode -> True: default fallback (gap=%d, elapsed=%.1f)", gap, elapsed_sec)
    return True
