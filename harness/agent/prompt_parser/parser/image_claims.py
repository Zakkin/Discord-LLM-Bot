"""画像内テキスト引用の未接地チェック（§152）。

返信文が「画像に書かれている文字」として引用した文字列を抽出し、
そのターンの Vision 説明文（または Discord 文脈行の `[画像内容: ...]`）に
存在するかを決定論的に突き合わせる純関数モジュール。

I/O なし・config 参照なし・例外非送出（fail-open）。
"""
from __future__ import annotations

import re
import unicodedata

# 引用候補の内側文字数範囲
_MIN_CLAIM_LEN = 2
_MAX_CLAIM_LEN = 30

# 文境界文字
_SENTENCE_BOUNDARY_CHARS = frozenset("。！？!?\r\n")

# 条件 (a): 文中に「書かれている」旨を示す表現（否定形は含めない）
_WRITTEN_MARKERS: tuple[str, ...] = (
    "書いてある",
    "書いてあります",
    "書いてありました",
    "書かれている",
    "書かれています",
    "書かれていました",
    "書いてあった",
    "と書か",
    "って書か",
)

# 条件 (b): 引用符直後の「って」探索幅と、その後続の発話動詞探索幅
_TTE_WINDOW = 4
_SPEECH_VERB_WINDOW = 6
_SPEECH_VERB_RE = re.compile(r"[言聞答思]")

# 引用符パターン（ペア消費のずれを防ぐため長さ制約は抽出後に適用）
_QUOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"「([^「」\r\n]+)」"),
    re.compile(r"『([^『』\r\n]+)』"),
    re.compile(r'"([^"\r\n]*)"'),
)

# 照合用の正規化で除去する空白・句読点・記号類
_NORMALIZE_STRIP_RE = re.compile(r'[\s、。！？!?・…「」『』"“”＂]+')

# Discord 文脈行の [画像内容: ...] キャプション開始マーカー
_CAPTION_START_RE = re.compile(r"\[画像内容[:：]\s*")


def _normalize_for_match(text: str) -> str:
    """比較用に空白・句読点・記号類を除去し、NFKC・小文字で正規化する。"""
    stripped = _NORMALIZE_STRIP_RE.sub("", text)
    normalized = unicodedata.normalize("NFKC", stripped)
    return _NORMALIZE_STRIP_RE.sub("", normalized).lower()


def _collect_all_quote_matches(text: str) -> list[tuple[int, int, str]]:
    """テキスト中の全引用ペアを (start, end, inner_raw) の出現順リストで返す。"""
    matches: list[tuple[int, int, str]] = []
    for pattern in _QUOTE_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), match.group(1)))
    # 開始位置の昇順、同じ開始位置なら外側（長いスパン）を先に処理する
    matches.sort(key=lambda item: (item[0], -item[1]))
    return matches


def _find_sentence_bounds(
    text: str,
    quote_start: int,
    quote_end: int,
    all_spans: list[tuple[int, int]],
) -> tuple[int, int]:
    """対象引用を含む文の (sent_start, sent_end) を返す。

    他の独立した引用スパン内部の句読点・感嘆符では文を分断しない。
    """
    n = len(text)

    # 左方向探索: quote_start より前で独立したスパンの末尾位置 -> 開始位置
    left_jump: dict[int, int] = {}
    # 右方向探索: quote_end 以降で独立したスパンの開始位置 -> 終了位置
    right_jump: dict[int, int] = {}
    for s, e in all_spans:
        if e <= quote_start:
            prev_s = left_jump.get(e - 1)
            if prev_s is None or s < prev_s:
                left_jump[e - 1] = s
        elif s >= quote_end:
            prev_e = right_jump.get(s)
            if prev_e is None or e > prev_e:
                right_jump[s] = e

    # 文頭を探す
    pos = quote_start - 1
    sent_start = 0
    while pos >= 0:
        jump_to = left_jump.get(pos)
        if jump_to is not None and jump_to <= pos:
            pos = jump_to - 1
            continue
        if text[pos] in _SENTENCE_BOUNDARY_CHARS:
            sent_start = pos + 1
            break
        pos -= 1

    # 文末を探す（句点・感嘆符の終端まで含める）
    pos = quote_end
    sent_end = n
    while pos < n:
        jump_to = right_jump.get(pos)
        if jump_to is not None and jump_to > pos:
            pos = jump_to
            continue
        if text[pos] in _SENTENCE_BOUNDARY_CHARS:
            sent_end = pos + 1
            break
        pos += 1

    return sent_start, sent_end


def _has_written_marker(sentence_outside: str) -> bool:
    """条件 (a): 引用の外側の同一文内に「書かれている」系の表現があるか。"""
    return any(marker in sentence_outside for marker in _WRITTEN_MARKERS)


def _matches_tte_without_speech_verb(after_in_sentence: str) -> bool:
    """条件 (b): 引用符直後4字以内に「って」が続き、その後6字以内に発話動詞がないか。"""
    tte_idx = after_in_sentence[:_TTE_WINDOW].find("って")
    if tte_idx < 0:
        return False
    after_tte = after_in_sentence[tte_idx + 2 : tte_idx + 2 + _SPEECH_VERB_WINDOW]
    return _SPEECH_VERB_RE.search(after_tte) is None


def extract_image_text_claims(reply_text: str) -> list[str]:
    """返信文から「画像に書かれている文字」として提示された引用文字列を抽出する。

    - 候補は引用符（「...」 / 『...』 / "..."）の内側が 2〜30 字のもの。
    - 次のいずれかを満たす引用を抽出する（出現順・重複除去）：
      (a) 引用を含む文（引用外側）に 書いてある / 書かれている / 書いてあった /
          と書か / って書か 等の記述表現を含む。
      (b) 引用符の直後（4字以内）に「って」が続き、その後6字以内に
          発話動詞（言 / 聞 / 答 / 思）が現れない。
    """
    try:
        if not isinstance(reply_text, str) or not reply_text:
            return []

        all_matches = _collect_all_quote_matches(reply_text)
        if not all_matches:
            return []

        all_spans = [(s, e) for s, e, _ in all_matches]
        accepted_spans: list[tuple[int, int]] = []
        claims: list[str] = []
        seen: set[str] = set()

        for start, end, raw_inner in all_matches:
            # 既に採用された外側引用の内部にネストする引用は重複計上しない
            if any(a_start <= start and end <= a_end for a_start, a_end in accepted_spans):
                continue

            inner = raw_inner.strip()
            if not (_MIN_CLAIM_LEN <= len(inner) <= _MAX_CLAIM_LEN):
                continue

            sent_start, sent_end = _find_sentence_bounds(reply_text, start, end, all_spans)
            before_in_sentence = reply_text[sent_start:start]
            after_in_sentence = reply_text[end:sent_end]
            sentence_outside = before_in_sentence + after_in_sentence

            if _has_written_marker(sentence_outside) or _matches_tte_without_speech_verb(
                after_in_sentence
            ):
                accepted_spans.append((start, end))
                if inner not in seen:
                    seen.add(inner)
                    claims.append(inner)

        return claims
    except Exception:
        return []


def find_ungrounded_image_claims(reply_text: str, image_description: str) -> list[str]:
    """返信文の画像内テキスト引用のうち、Vision 説明文に現れないものを返す。

    - image_description が空（または空白のみ）の場合は常に空リストを返す（fail-open）。
    - 入力が文字列でない場合や予期せぬエラー時も例外を投げず空リストを返す。
    """
    try:
        if not isinstance(reply_text, str) or not isinstance(image_description, str):
            return []
        if not reply_text.strip() or not image_description.strip():
            return []

        norm_desc = _normalize_for_match(image_description)
        if not norm_desc:
            return []

        claims = extract_image_text_claims(reply_text)
        if not claims:
            return []

        ungrounded: list[str] = []
        for claim in claims:
            norm_claim = _normalize_for_match(claim)
            if len(norm_claim) < _MIN_CLAIM_LEN:
                continue
            if norm_claim not in norm_desc:
                ungrounded.append(claim)
        return ungrounded
    except Exception:
        return []


def _extract_captions_from_line(line: str) -> list[str]:
    """1行から `[画像内容: ...]` 形式のキャプション中身をすべて抽出する。"""
    results: list[str] = []
    pos = 0
    n = len(line)
    while pos < n:
        match = _CAPTION_START_RE.search(line, pos)
        if match is None:
            break
        content_start = match.end()
        # 内側に [注意] のような対になる角括弧がある場合を考慮して深さカウントする
        depth = 1
        idx = content_start
        first_close = -1
        close_idx = -1
        while idx < n:
            ch = line[idx]
            if ch == "[":
                depth += 1
            elif ch == "]":
                if first_close < 0:
                    first_close = idx
                depth -= 1
                if depth == 0:
                    close_idx = idx
                    break
            idx += 1

        if close_idx < 0:
            # 対にならない '[' を含んで深さが 0 にならなかった場合は最初の ']' までを採用
            if first_close < 0:
                break
            close_idx = first_close

        caption = line[content_start:close_idx].strip()
        if caption:
            results.append(caption)
        pos = close_idx + 1
    return results


def extract_bracketed_image_captions(lines: list[str]) -> str:
    """行リストから `[画像内容: ...]` の中身を取り出し、改行で連結して返す。

    該当がなければ空文字列を返す。例外を投げない。
    """
    try:
        if not lines or isinstance(lines, (str, bytes)):
            return ""
        captions: list[str] = []
        for line in lines:
            if not isinstance(line, str) or "画像内容" not in line:
                continue
            captions.extend(_extract_captions_from_line(line))
        return "\n".join(captions)
    except Exception:
        return ""


__all__ = [
    "extract_image_text_claims",
    "find_ungrounded_image_claims",
    "extract_bracketed_image_captions",
]
