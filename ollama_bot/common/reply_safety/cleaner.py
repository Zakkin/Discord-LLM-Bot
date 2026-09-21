"""テキストの正規化、不要記号・Markdown装飾除去、一人称や感謝表現の補正処理。"""
from __future__ import annotations

import re


def strip_markdown_artifacts(text: str) -> str:
    """Markdown装飾（太字、斜体、リンク、コード、引用記号、見出し等）を除去して平文にする。"""
    value = str(text or "").strip()
    if not value:
        return ""

    value = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", lambda m: f"{m.group(1).strip()}: {m.group(2)}".strip(": "), value)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f"{m.group(1).strip()}: {m.group(2)}", value)
    value = re.sub(r"`([^`\n]+)`", r"\1", value)
    value = re.sub(r"(\*\*|__)(.*?)\1", r"\2", value)
    value = re.sub(r"(~~)(.*?)\1", r"\2", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", value)

    lines: list[str] = []
    for line in value.splitlines():
        s = line.rstrip()
        if re.fullmatch(r"\s*(?:[-*_]\s*){3,}", s):
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
        s = re.sub(r"^\s{0,3}>\s?", "", s)
        s = re.sub(r"^\s*(?:[-*+・]|[0-9０-９]+[.)．、])\s+", "", s)
        lines.append(s.rstrip())

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_leading_list_marker(text: str) -> tuple[str, bool]:
    """箇条書き記号（-, *, +, ・, 1. など）が先頭にあれば除去する。"""
    value = str(text or "").strip()
    match = re.match(r"^(?:[-*+・]|[0-9０-９]+[.)．、])\s*(.+)$", value)
    if not match:
        return value, False
    return str(match.group(1) or "").strip(), True


def _strip_self_addressed_thanks(text: str) -> str:
    """文末やお礼表現で『ありがとう、Bot名』のように自分自身の名前で相手に感謝・呼びかけている誤りを修復する。"""
    t = str(text or "")
    if not t:
        return ""
    from ..bot_identity import _extract_character_names_from_config
    names = _extract_character_names_from_config()
    if names:
        escaped = "|".join(re.escape(n) for n in names if n)
        pattern = rf"(ありがとう(?:ございます)?|サンキュー|感謝(?:します|いたします)?)[、,\s]+(?:{escaped})(?:ちゃん|さん)?([!！…\.\?？~〜\s]*)$"
    else:
        pattern = r"(ありがとう(?:ございます)?|サンキュー|感謝(?:します|いたします)?)[、,\s]+(?:アシスタント|AI|Bot)(?:ちゃん|さん)?([!！…\.\?？~〜\s]*)$"
    t = re.sub(pattern, r"\1\2", t, flags=re.MULTILINE | re.IGNORECASE)
    return t


def _normalize_unintended_watashi(text: str) -> str:
    """動揺時や自己弁護時に一人称が『私』にブレてしまう誤りを設定一人称に修復する。"""
    t = str(text or "")
    if not t:
        return ""
    from lib.config_utils import cfg
    should_replace = cfg("CHARACTER_DEVIATION_AUTO_WATASHI_REPLACE", False)
    target_fp = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    if not should_replace or not target_fp:
        return t
    pattern = r"(^|[\s、。！？!?.…「」『』\n])私([、，はがのもにへよりからでって]|だって|なんか|なんて|自身)"
    return re.sub(pattern, rf"\g<1>{target_fp}\2", t)


def strip_unprompted_nerd_emoji(user_text: str, reply: str) -> str:
    """ユーザー発言に 🤓 が含まれていない場合、AI返答に含まれる 🤓 を除去する。"""
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r:
        return ""
    if "🤓" in u:
        return r
    if "🤓" in r:
        r = r.replace("🤓", "").strip()
    return r


def strip_trailing_reasoning_notes(text: str) -> str:
    """文末に付着した英語の検証メモや思考メモ（(Matches exactly), (verified) 等）を除去する。"""
    t = str(text or "").strip()
    if not t:
        return ""
    # 1. (Matches exactly), (Matches), (verified), (passed), (done), (OK) 等の明示的な思考メモ
    t = re.sub(
        r"\s*\(?(?:Matches(?:\s+exactly)?|Verified|Passed|Proceeds|Output matches)\)?\.?$",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    # 2. クォート閉じの後ろ、または文末の英字括弧メモ: \([A-Za-z\s_-]{2,40}\)\.?$
    t = re.sub(r"\s*\([A-Za-z\s_-]{2,40}\)\.?$", "", t).strip()
    # 3. クォートやシングルクォートの残骸（例: ' や "）が末尾に残っている場合をトリム
    t = re.sub(r"['\"]+$", "", t).strip()
    return t


from lib.text_utils import (
    extract_effective_first_clause,
    normalize_compare_text as _normalize_compare_text,
    strip_leading_interjections,
)


def _normalize_topic_echo_text(text: str) -> str:
    """話題オウム返し判定用に表記ゆれや助詞を除去して正規化する。"""
    t = _normalize_compare_text(text)
    t = re.sub(r"(?:ごはん|ご飯|御飯)", "飯", t)
    t = re.sub(r"[をがはのにへと]", "", t)
    return t



def _opening_echo_fragment(text: str) -> str:
    """文頭のオウム返し検出用の冒頭句を抽出・正規化する。"""
    head = extract_effective_first_clause(text)
    head = re.sub(
        r"(?:だよな|だろ|だよ|なのか|なの|だと|だな|って|とは|という|か|ね|な|だ)$",
        "",
        head,
    ).strip()
    return _normalize_topic_echo_text(head)
