"""§173: 「これ何？」検索のクエリ候補化・定義ヒット判定・推測ガード（純粋関数群）。"""
from __future__ import annotations

from typing import Any, Callable

QUERY_CANDIDATES_ENABLED_SETTING: str = "WHAT_IS_THIS_QUERY_CANDIDATES_ENABLED"
SPECULATION_GUARD_ENABLED_SETTING: str = "WHAT_IS_THIS_SPECULATION_GUARD_ENABLED"

LOOKUP_STOPWORDS: frozenset[str] = frozenset(
    {"意味", "とは", "スラング", "何", "これ", "調べ", "検索"}
)

SPECULATION_MARKERS: tuple[str, ...] = (
    "たぶん",
    "おそらく",
    "じゃないかな",
    "ではないかな",
    "なんじゃない",
    "と思われ",
)

_MAX_FALLBACK_TOPIC_LEN: int = 20
_MAX_CANDIDATES: int = 3

cfg_bool: Callable[[str, bool], bool] | None
try:
    from ollama_bot.common.config_helpers import cfg_bool as _cfg_bool_impl
    cfg_bool = _cfg_bool_impl
except Exception:
    cfg_bool = None

def extract_lookup_term(search_query: str, topic_label: str) -> str:
    """検索対象の中心語を決定的に取り出す。

    search_query を空白で分割し、停止語を除いた最も長いトークンを返す。
    無ければ topic_label 側で同じことを試し、それも無ければ topic_label が
    20字以内なら topic_label 自体を、そうでなければ "" を返す。
    """
    query_tokens = [
        tok for tok in str(search_query or "").split()
        if tok and tok not in LOOKUP_STOPWORDS
    ]
    if query_tokens:
        return max(query_tokens, key=len)

    label_text = str(topic_label or "").strip()
    label_tokens = [
        tok for tok in label_text.split()
        if tok and tok not in LOOKUP_STOPWORDS
    ]
    if label_tokens:
        return max(label_tokens, key=len)

    if label_text and len(label_text) <= _MAX_FALLBACK_TOPIC_LEN:
        return label_text
    return ""


def build_query_candidates(search_query: str, topic_label: str) -> list[str]:
    """ディスパッチ候補を最大3本返す。

    先頭は search_query そのもの。次に中心語（extract_lookup_term）が取れた場合、
    「{中心語} 意味」「{中心語} とは」を重複しない順に追加する。
    search_query が空なら空リストを返す。
    """
    base = str(search_query or "").strip()
    if not base:
        return []

    candidates: list[str] = [base]
    seen: set[str] = {" ".join(base.split())}

    term = extract_lookup_term(base, topic_label)
    if term:
        for suffix in ("意味", "とは"):
            candidate = f"{term} {suffix}"
            normalized = " ".join(candidate.split())
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(candidate)
                if len(candidates) >= _MAX_CANDIDATES:
                    break
    return candidates[:_MAX_CANDIDATES]


def is_definitional_hit(result: Any, term: str) -> bool:
    """調査結果が中心語の定義・解説を含むとみなせるかを決定的に判定する。

    次を全て満たす場合に True:
    - result が dict で、error が空
    - summary_degraded が真でない（キーが無い場合は False 扱い）
    - summary が空でない
    - term が空、または summary に term が含まれる、または sources のいずれかの title に term が含まれる
    """
    if not isinstance(result, dict):
        return False
    if str(result.get("error") or "").strip():
        return False
    if bool(result.get("summary_degraded", False)):
        return False

    summary = str(result.get("summary") or "").strip()
    if not summary:
        return False

    clean_term = str(term or "").strip()
    if not clean_term:
        return True

    needle = clean_term.lower()
    if needle in summary.lower():
        return True

    sources = result.get("sources")
    if isinstance(sources, (list, tuple)):
        for item in sources:
            if isinstance(item, dict):
                title = str(item.get("title") or "")
                if needle in title.lower():
                    return True
    return False


def contains_speculation(text: str) -> bool:
    """推測マーカー（たぶん / おそらく / じゃないかな / ではないかな / なんじゃない / と思われ）のいずれかを含むか。"""
    target = str(text or "")
    if not target:
        return False
    return any(marker in target for marker in SPECULATION_MARKERS)


def build_honest_miss_text(topic_label: str) -> str:
    """固定文を返す: 「『{topic_label}』について調べましたが、意味や出典を確認できませんでした。」"""
    label = str(topic_label or "").strip() or "対象"
    return f"『{label}』について調べましたが、意味や出典を確認できませんでした。"


def is_query_candidates_enabled() -> bool:
    """WHAT_IS_THIS_QUERY_CANDIDATES_ENABLED（既定 True）。取得失敗時は True。"""
    if cfg_bool is None:
        return True
    try:
        return bool(cfg_bool(QUERY_CANDIDATES_ENABLED_SETTING, True))
    except Exception:
        return True


def is_speculation_guard_enabled() -> bool:
    """WHAT_IS_THIS_SPECULATION_GUARD_ENABLED（既定 True）。取得失敗時は True。"""
    if cfg_bool is None:
        return True
    try:
        return bool(cfg_bool(SPECULATION_GUARD_ENABLED_SETTING, True))
    except Exception:
        return True
