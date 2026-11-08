from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any, Iterable, Literal

__all__ = [
    "compact_whitespace",
    "truncate_text",
    "truncate_lines",
    "trim_text_at_safe_boundary",
    "compact_exception_message",
    "normalize_text",
    "normalize_compare_text",
    "strip_leading_interjections",
    "extract_effective_first_clause",
    "extract_urls",
    "strip_urls",
]


def compact_whitespace(text: Any) -> str:
    """連続する空白文字・改行を単一スペースに正規化して両端をトリムする。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def truncate_text(text: str, max_len: int = 300, *, suffix: str = "...") -> str:
    """指定文字数を超える文字列を安全に切り詰める。"""
    normalized = "" if text is None else str(text)
    if max_len <= 0:
        return ""
    if len(normalized) <= max_len:
        return normalized
    suffix_len = len(suffix)
    if max_len <= suffix_len:
        return normalized[:max_len]
    return normalized[: max_len - suffix_len] + suffix


def truncate_lines(
    lines: Iterable[str],
    *,
    max_lines: int = 8,
    max_chars_per_line: int = 220,
    max_total_chars: int = 900,
) -> list[str]:
    """行リストを指定行数・文字数以内に安全に切り詰める。

    末尾（直近・最新）の行を優先して残し、文字数上限超過時は古い行から優先的にトリミングする。
    """
    normalized = [str(line or "").strip() for line in (lines or [])]
    normalized = [line for line in normalized if line]
    if max_lines > 0:
        normalized = normalized[-max_lines:]

    out_rev: list[str] = []
    total = 0
    for line in reversed(normalized):
        trimmed = truncate_text(line, max_chars_per_line)
        add_len = len(trimmed) + (1 if out_rev else 0)
        if max_total_chars > 0 and out_rev and total + add_len > max_total_chars:
            break
        if max_total_chars > 0 and not out_rev and len(trimmed) > max_total_chars:
            trimmed = truncate_text(trimmed, max_total_chars)
        out_rev.append(trimmed)
        total += add_len
    return list(reversed(out_rev))


_SAFE_TRIM_SENTENCE_END_RE = re.compile(r"[。！？!?](?:[」』）)】]*)")
_SAFE_TRIM_DANGLING_HEADER_RE = re.compile(r"^\s*(?:【[^】\n]*】|[^\n]{0,40}[:：])\s*$")


def _drop_trailing_dangling_headers(units: list[str]) -> list[str]:
    """末尾に本文を伴わない見出し（『【〜】』『〜:』だけの塊）や空要素が残らないよう除去する。"""
    out = list(units)
    while out and (not out[-1].strip() or _SAFE_TRIM_DANGLING_HEADER_RE.match(out[-1])):
        out.pop()
    return out


def _greedy_join_units(units: list[str], sep: str, max_chars: int) -> list[str]:
    kept: list[str] = []
    total = 0
    for unit in units:
        add_len = len(unit) + (len(sep) if kept else 0)
        if total + add_len > max_chars:
            break
        kept.append(unit)
        total += add_len
    return _drop_trailing_dangling_headers(kept)


def trim_text_at_safe_boundary(text: str, max_chars: int) -> str:
    """文章の途中でブツ切りにせず、安全な境界で文字数上限以内に切り詰める。

    1. 空行（``\\n\\n``）区切りのブロック単位で先頭から収まる分だけ残す。
    2. 先頭ブロック単体で上限を超える場合は、改行単位（行単位）で残す。
    3. 先頭行単体でも超える場合は、文末記号（。！？等）までで切る。
    4. 安全な境界が一切見つからない場合は空文字を返す（呼び出し側で扱いを判断すること）。

    末尾に本文を伴わない見出し（『【〜】』のみの塊）は残さない。
    """
    raw = "" if text is None else str(text)
    if max_chars <= 0:
        return ""
    if len(raw) <= max_chars:
        blocks = _drop_trailing_dangling_headers(raw.split("\n\n"))
        return "\n\n".join(blocks).rstrip()

    blocks = raw.split("\n\n")
    kept_blocks = _greedy_join_units(blocks, "\n\n", max_chars)
    if kept_blocks:
        return "\n\n".join(kept_blocks).rstrip()

    first_block = next((b for b in blocks if b.strip()), "")
    lines = first_block.split("\n")
    kept_lines = _greedy_join_units(lines, "\n", max_chars)
    if kept_lines:
        return "\n".join(kept_lines).rstrip()

    first_line = next((ln for ln in lines if ln.strip()), "")
    head = first_line[:max_chars]
    last_end = 0
    for m in _SAFE_TRIM_SENTENCE_END_RE.finditer(head):
        last_end = m.end()
    if last_end > 0:
        return head[:last_end].rstrip()
    return ""


def compact_exception_message(exc: Exception) -> str:
    """例外オブジェクトをログや通知用の簡潔な文字列表現に変換する。"""
    if isinstance(exc, asyncio.TimeoutError):
        return "タイムアウト"
    text = str(exc or "").strip()
    return text or type(exc).__name__


def normalize_text(text: Any, form: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFKC") -> str:
    """Unicode 正規化（デフォルト NFKC）を行い、前後の空白をトリムする。"""
    return unicodedata.normalize(form, str(text or "")).strip()


def normalize_compare_text(text: str) -> str:
    """比較・重複・オウム返し判定用に空白・約物・句読点を除去し小文字化する。"""
    t = str(text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[、\u3000\-—–‐・,，。.!！?？…:：;；\"'“”‘’（）()\[\]{}]+", "", t)
    return t


_INTERJECTION_PREFIX_PATTERN = re.compile(
    r"^(?:"
    r"ぴゃ[あっ]?|ひゃ[あっ]?|えー?っと?|え[っー〜]?|あ[っー〜]?|う[ー〜]+ん?|うん|"
    r"お[っー〜]?|おお|わ[ぁー〜]+|ふふ[っ]?|へ[えー〜]+|ん[ー〜]+|いや[あ]?"
    r")[\s\u3000、。,.!！?？…~～\-—–‐]*",
    re.IGNORECASE,
)


def strip_leading_interjections(text: str) -> str:
    """文頭のキャラクター感嘆詞やフィラー（ぴゃっ…！、えっ、等）を順次トリミングする。"""
    raw = str(text or "").strip()
    while True:
        m = _INTERJECTION_PREFIX_PATTERN.match(raw)
        if not m or not m.group(0):
            break
        trimmed = raw[m.end():].strip()
        if not trimmed:
            break
        raw = trimmed
    return raw


def extract_effective_first_clause(text: str) -> str:
    """文頭の感嘆詞やフィラー（ぴゃっ、えっ等）をスキップした実質的な開始節を抽出する。"""
    raw = str(text or "").strip()
    first_line = raw.splitlines()[0] if raw else ""
    line = strip_leading_interjections(first_line)
    clauses = [c.strip() for c in re.split(r"[、。,.!！?？…]+", line) if c.strip()]
    return clauses[0] if clauses else first_line.strip()



_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_URL_STRIP_SUFFIXES = ("を教えて", "について", "のこと", "って", "とは", "について教えて", "の記事")


def _normalize_url_candidate(raw: str) -> str:
    url = raw.rstrip(".,;:!?)>]}\"'")
    for suffix in _URL_STRIP_SUFFIXES:
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def extract_urls(text: str) -> list[str]:
    """テキストからURL候補を抽出し、末尾の助詞・約物をトリミングして重複排除して返す。"""
    seen: set[str] = set()
    urls: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = _normalize_url_candidate(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def strip_urls(text: str) -> str:
    """テキストからURLを除去し、空白を正規化して返す。"""
    stripped = _URL_RE.sub(" ", str(text or ""))
    return compact_whitespace(stripped)

