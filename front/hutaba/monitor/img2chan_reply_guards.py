"""§172: ふたば返信の自己名呼びかけ除去および動画添付注意の純粋関数群。"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
import re

_DEFAULT_BOT_NAME = "コイト"
_QUOTE_PREFIXES = (">", "＞")
_VIDEO_CAUTION_NOTE = (
    "対象レスには動画ファイルが添付されていますが、あなたは動画を再生・視聴できません。"
    "動画の内容を見たような描写や断定はせず、対象レスの本文への感想に留めてください。"
)

def _normalize_self_names(names: Sequence[str] | Iterable[str]) -> tuple[str, ...]:
    """空文字と重複を除き、長い順に並べた名前タプルを返す。"""
    cleaned = [str(n).strip() for n in names if str(n or "").strip()]
    unique = list(dict.fromkeys(cleaned))
    unique.sort(key=len, reverse=True)
    return tuple(unique)


def collect_self_names() -> tuple[str, ...]:
    """Bot の自称名を集めて返す。

    cfg("BOT_NAME", "コイト") と cfg("OLLAMA_BOT_DISPLAY_NAME", "") を対象とし、
    空文字を除き、重複を除いて長い順（例: コイトフクマル → コイト）に並べる。
    設定読み取りは ollama_bot.common.config_helpers の cfg を try/except で使い、
    失敗した項目は既定値（BOT_NAME は "コイト"、表示名は ""）で埋める。
    """
    cfg_fn = None
    try:
        from ollama_bot.common.config_helpers import cfg as _cfg

        cfg_fn = _cfg
    except Exception:
        cfg_fn = None

    raw_values: list[str] = []
    for key, default in (("BOT_NAME", _DEFAULT_BOT_NAME), ("OLLAMA_BOT_DISPLAY_NAME", "")):
        if cfg_fn is None:
            val: object = default
        else:
            try:
                val = cfg_fn(key, default)
            except Exception:
                val = default
        name = str(val or "").strip()
        if name:
            raw_values.append(name)

    return _normalize_self_names(raw_values)


def find_self_name_vocative(
    text: str,
    names: Sequence[str] | Iterable[str],
) -> str | None:
    """本文中の「自分への呼びかけ」に一致した名前を返す。無ければ None。

    対象パターンは次の2種のみ:
    (a) 行頭（引用行 `>` / `＞` で始まる行は除く）の `{name}` の直後に `、` `，` `！` `!` のいずれか
    (b) 文末記号（`。` `！` `？`）の直後の `{name}` の直後に `、` `，`
    `{name}は` `{name}の` `{name}が` 等の通常の言及は対象外とする。
    names は長い順に試す（部分一致で短い名前が先に当たるのを防ぐ）。
    """
    raw = str(text or "")
    if not raw:
        return None
    ordered_names = _normalize_self_names(names)
    if not ordered_names:
        return None

    names_alt = "|".join(re.escape(n) for n in ordered_names)
    head_re = re.compile(rf"^[ \t]*({names_alt})[、，！!]")
    mid_re = re.compile(rf"[。！？][ \t]*({names_alt})[、，]")

    for line in raw.splitlines():
        if line.lstrip().startswith(_QUOTE_PREFIXES):
            continue
        m_head = head_re.match(line)
        if m_head:
            return m_head.group(1)
        m_mid = mid_re.search(line)
        if m_mid:
            return m_mid.group(1)
    return None


def strip_self_name_vocative(
    text: str,
    names: Sequence[str] | Iterable[str],
) -> str:
    """呼びかけ部分（名前＋直後の読点/感嘆符）だけを除去して返す。

    - 行ごとに処理し、引用行（`>` / `＞` で始まる行）は変更しない。
    - (a) は行頭で最大1回、(b) は行内の各文境界で適用する。
    - 除去後に本文（非引用行）が空になる場合は、元の text をそのまま返す（fail-open）。
    - names が空なら text をそのまま返す。
    """
    raw = str(text or "")
    if not raw:
        return raw
    ordered_names = _normalize_self_names(names)
    if not ordered_names:
        return raw

    names_alt = "|".join(re.escape(n) for n in ordered_names)
    head_re = re.compile(rf"^([ \t]*)(?:{names_alt})[、，！!]")
    mid_re = re.compile(rf"([。！？][ \t]*)(?:{names_alt})[、，]")

    processed_chunks: list[str] = []
    processed_contents: list[str] = []
    for chunk in raw.splitlines(keepends=True):
        content = chunk.rstrip("\r\n")
        eol = chunk[len(content):]
        if content.lstrip().startswith(_QUOTE_PREFIXES):
            processed_contents.append(content)
            processed_chunks.append(chunk)
            continue
        updated = head_re.sub(r"\1", content, count=1)
        updated = mid_re.sub(r"\1", updated)
        processed_contents.append(updated)
        processed_chunks.append(f"{updated}{eol}")

    has_non_quote_body = any(
        not ln.lstrip().startswith(_QUOTE_PREFIXES) and ln.strip()
        for ln in processed_contents
    )
    if not has_non_quote_body:
        return raw

    return "".join(processed_chunks)


def is_self_name_guard_enabled() -> bool:
    """HUTABA_SELF_NAME_VOCATIVE_GUARD_ENABLED（既定 True）を読む。取得失敗時は True。"""
    try:
        from ollama_bot.common.config_helpers import cfg_bool

        return bool(cfg_bool("HUTABA_SELF_NAME_VOCATIVE_GUARD_ENABLED", True))
    except Exception:
        return True


def is_video_caution_enabled() -> bool:
    """HUTABA_VIDEO_CAUTION_ENABLED（既定 True）を読む。取得失敗時は True。"""
    try:
        from ollama_bot.common.config_helpers import cfg_bool

        return bool(cfg_bool("HUTABA_VIDEO_CAUTION_ENABLED", True))
    except Exception:
        return True


def build_video_caution_note() -> str:
    """動画注意の固定文を返す。"""
    return _VIDEO_CAUTION_NOTE
