"""ユーザープロンプト構築で用いる純粋ヘルパー・正規化・サニタイズユーティリティ群。"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

import discord

from lib.text_utils import compact_whitespace, truncate_text
from ollama_bot.common.discord_helpers import (
    author_id,
    content,
    context_boundary_of,
    display_name,
    is_context_item_after_boundary,
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


_MEMORY_LINE_PREFIX_RE = re.compile(r"^\[(?P<meta>[^\]]+)\]\s*(?P<body>.+)$", re.DOTALL)


def _parse_formatted_memory_provenance(line: str) -> tuple[str, str]:
    """フォーマット済み記憶行 `[時間 / 出所: ...] 本文` から `(本文, 出所表示)` を抽出する。"""
    raw = str(line or "").strip()
    if not raw or raw.startswith("[過去の出来事]"):
        return "", ""
    m = _MEMORY_LINE_PREFIX_RE.match(raw)
    if not m:
        return raw, "不明"
    meta = m.group("meta")
    body = m.group("body").strip()
    source_val = "不明"
    for part in meta.split("/"):
        p = part.strip()
        if p.startswith("出所:") or p.startswith("出所："):
            source_val = re.sub(r"^出所[:：]\s*", "", p).strip() or "不明"
            break
    return body, source_val


def _select_target_memories_for_source_question(
    memory_candidates: Sequence[str],
    *,
    user_text: str,
    last_assistant_text: str = "",
) -> list[tuple[str, str]]:
    """出所質問の対象（固有名詞・数値）に当たる記憶行の `(content, source)` リストを返す。"""
    parsed_list: list[tuple[str, str]] = []
    seen_bodies: set[str] = set()
    for line in memory_candidates:
        body, src = _parse_formatted_memory_provenance(line)
        if body and body not in seen_bodies:
            seen_bodies.add(body)
            parsed_list.append((body, src))
    if not parsed_list:
        return []

    query_src = f"{user_text} {last_assistant_text}"
    tokens = set(
        re.findall(
            r"(?<!\d)\d+(?:\.\d+)?(?!\d)|[\u4E00-\u9FFF]{2,}|[\u30A0-\u30FFー]{2,}|[A-Za-z0-9_]{2,}",
            query_src,
        )
    )
    stop_tokens = {"何で", "なんで", "どこで", "誰が", "出所", "ソース", "元ネタ", "根拠", "出典", "知ってる", "覚えた", "言った", "あなた", "コイト", "返信"}
    tokens = {t for t in tokens if t not in stop_tokens and len(t) < 15}

    matched = [
        (body, src)
        for body, src in parsed_list
        if any(t in body for t in tokens)
    ]
    if matched:
        return matched[:2]
    return parsed_list[:1]


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
    from ..thread_tracker import looks_like_incomplete_topic_opener

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
