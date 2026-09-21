"""
web_research/helpers.py

このファイルはウェブリサーチ機能で使用されるユーティリティ関数群を提供します。
主な責務:
- URL安全性チェック（パブリックIPかどうか、ログインウォールかどうか）
- テキストのクリーンアップ（HTMLタグ除去・空白正規化）
- 既知用語キャッシュ管理（調べた用語を一定時間キャッシュし再調査を抑制）
- リサーチ要否判定ロジック（detect_reply_research_rules / detect_deepdive_followup）
- 調査クエリ・用語候補の抽出

AIエージェントが新しい判定ルールを追加する場合はこのファイルを修正してください。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import ipaddress
import logging
import re
import socket
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..fact_check import extract_urls, looks_like_url_only_text, should_build_link_summaries
from ..ollama_helpers import truncate_text
from lib.text_utils import compact_whitespace as _compact_whitespace

from .config import (
    _ASCII_TERM_RE,
    _BROWSER_BOILERPLATE_PATTERNS,
    _CONTEXT_URL_ACTION_KEYWORDS,
    _CONTEXT_URL_STRONG_ACTION_KEYWORDS,
    _CONTEXT_URL_TARGET_KEYWORDS,
    _CONVERSATIONAL_FOLLOWUP_MARKERS,
    _DEFAULT_ALLOWED_URL_SCHEMES,
    _DEFAULT_X_TIMELINE_HARD_LIMIT,
    _DEEPDIVE_CACHE_TTL_SEC,
    _EXPLANATION_KEYWORDS,
    _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS,
    _FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN,
    _FALLBACK_XCOM_LOGIN_WALL_PHRASES,
    _FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES,
    _GENERIC_TERM_STOPWORDS,
    _IMG_DEEPDIVE_FOLLOWUP_KEYWORDS,
    _KANJI_TERM_RE,
    _KATAKANA_TERM_RE,
    _NEWS_KEYWORDS,
    _ORDINAL_TO_INDEX,
    _PREFERENCE_OR_OPINION_MARKERS,
    _PRODUCT_SPEC_KEYWORDS,
    _QUERY_TERM_RE,
    _RECENCY_KEYWORDS,
    _RESEARCH_REQUEST_KEYWORDS,
    _SOURCE_REQUEST_KEYWORDS,
    _X_DEEPDIVE_FOLLOWUP_KEYWORDS,
    _YEAR_OR_DATE_RE,
    ResearchDispatchMode,
    _known_term_cache_max_size,
    _known_term_cache_ttl_sec,
    _safe_browser_page_limit,
)
from .topic_investigation import detect_topic_opinion_inquiry

log = logging.getLogger("ollama_bot.common.web_research")

# 実行時に動的にインポートされるヘルパーモジュール
_xcom: Any = None
_img_helper: Any = None


def _init_helpers(xcom: Any, img_helper: Any) -> None:
    global _xcom, _img_helper
    _xcom = xcom
    _img_helper = img_helper

# ---------------------------------------------------------------------------
# Module-level state for term cache
# ---------------------------------------------------------------------------

_known_term_cache: OrderedDict[str, float] = OrderedDict()

# ---------------------------------------------------------------------------
# Date/time
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def _strip_urls(text: str) -> str:
    return re.sub(r"https?://\S+", " ", str(text or ""))


def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:

    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _cleanup_browser_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    return _compact_whitespace(value)


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def _is_safe_public_browser_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False

    if parsed.scheme.lower() not in _DEFAULT_ALLOWED_URL_SCHEMES:
        return False

    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        return False

    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(hostname)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False

    return True


def _is_safe_public_ip_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _resolve_browser_host_addresses(hostname: str) -> list[str]:
    normalized = str(hostname or "").strip()
    if not normalized:
        return []
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(
        normalized,
        None,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    addresses: list[str] = []
    for item in results:
        sockaddr = item[4] if len(item) >= 5 else None
        if not isinstance(sockaddr, tuple) or not sockaddr:
            continue
        address = str(sockaddr[0] or "").strip()
        if address and address not in addresses:
            addresses.append(address)
    return addresses


async def _is_safe_public_browser_url_resolved(url: str) -> bool:
    if not _is_safe_public_browser_url(url):
        return False

    from urllib.parse import urlparse
    parsed = urlparse(str(url or "").strip())
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if _is_safe_public_ip_address(hostname):
        return True
    if hostname == "localhost" or hostname.endswith(".local"):
        return False

    try:
        resolved_addresses = await _resolve_browser_host_addresses(hostname)
    except Exception as e:
        log.warning("browser URL host resolution failed host=%s err=%r", hostname, e)
        return False

    if not resolved_addresses:
        return False
    return all(_is_safe_public_ip_address(address) for address in resolved_addresses)


# ---------------------------------------------------------------------------
# Login wall detection
# ---------------------------------------------------------------------------


def _is_login_wall_or_error_page_fallback(content_text: str) -> bool:
    stripped = str(content_text or "").strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if any(phrase in lowered for phrase in _FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES):
        return True
    if len(stripped) <= _FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN:
        return any(phrase in lowered for phrase in _FALLBACK_XCOM_LOGIN_WALL_PHRASES)
    return False


# ---------------------------------------------------------------------------
# Text analysis utilities
# ---------------------------------------------------------------------------


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _QUERY_TERM_RE.findall(str(query or "")):
        candidate = _normalize_candidate_term(match)
        if len(candidate) <= 1:
            continue
        lowered = candidate.lower()
        if lowered in {term.lower() for term in terms}:
            continue
        terms.append(candidate)
    return terms[:8]


def _split_text_chunks(text: str) -> list[str]:
    normalized = str(text or "").replace("。", "。\n").replace("！", "！\n").replace("？", "？\n")
    chunks = [_compact_whitespace(chunk) for chunk in normalized.splitlines()]
    return [chunk for chunk in chunks if len(chunk) >= 20]


def _looks_like_boilerplate(chunk: str) -> bool:
    lowered = str(chunk or "").lower()
    if any(pattern in lowered for pattern in _BROWSER_BOILERPLATE_PATTERNS):
        return True
    if lowered.count("|") >= 4:
        return True
    return False


def _select_relevant_excerpt(text: str, query: str, *, max_chars: int) -> str:
    query_terms = _query_terms(query)
    chunks = [chunk for chunk in _split_text_chunks(text) if not _looks_like_boilerplate(chunk)]
    if not chunks:
        return truncate_text(_compact_whitespace(text), max_chars)

    scored_chunks: list[tuple[int, int, str]] = []
    for chunk in chunks:
        score = 0
        lowered = chunk.lower()
        for term in query_terms:
            if term.lower() in lowered:
                score += 3
        score += min(len(chunk) // 80, 3)
        scored_chunks.append((score, len(chunk), chunk))

    scored_chunks.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[str] = []
    total = 0
    for _, _, chunk in scored_chunks[:6]:
        add_len = len(chunk) + (1 if selected else 0)
        if selected and total + add_len > max_chars:
            continue
        selected.append(chunk)
        total += add_len
        if total >= max_chars or len(selected) >= 3:
            break

    if not selected:
        selected = chunks[:2]
    return truncate_text(" ".join(selected), max_chars)


# ---------------------------------------------------------------------------
# Helper Loaders & Fallbacks
# ---------------------------------------------------------------------------


class _MissingXComHelpers:
    DEFAULT_DYNAMIC_BROWSER_HOSTS: tuple[str, ...] = _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS
    DEFAULT_X_TIMELINE_LIMIT = 10
    X_TIMELINE_HOME_URL = "https://x.com/home"
    X_TIMELINE_TWEET_SELECTOR = 'div[data-testid="primaryColumn"] article[data-testid="tweet"]'

    @staticmethod
    def is_dynamic_browser_site(
        url: str,
        *,
        dynamic_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> bool:
        hostname = str(urlparse(str(url or "").strip()).hostname or "").lower()
        if not hostname:
            return False
        for raw_host in dynamic_hosts or _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS:
            host = str(raw_host or "").strip().lower()
            if host and (hostname == host or hostname.endswith(f".{host}")):
                return True
        return False

    @staticmethod
    def is_login_wall_or_error_page(content_text: str) -> bool:
        stripped = str(content_text or "").strip()
        if not stripped:
            return True
        lowered = stripped.lower()
        if any(phrase in lowered for phrase in _FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES):
            return True
        if len(stripped) <= _FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN:
            return any(phrase in lowered for phrase in _FALLBACK_XCOM_LOGIN_WALL_PHRASES)
        return False

    @staticmethod
    def is_x_timeline_request(text: str) -> bool:
        lowered = str(text or "").lower().replace("　", "")
        has_x = any(token in lowered for token in ("x", "twitter", "ツイッター"))
        has_timeline = any(token in lowered for token in ("タイムライン", "tl", "timeline", "feed", "フィード"))
        has_action = any(token in lowered for token in ("今", "いま", "現在", "最新", "直近", "流れて", "見て", "取得", "何", "なに", "どう"))
        return bool(has_timeline and has_action and (has_x or "tl" in lowered))

    @staticmethod
    def extract_x_timeline_limit(text: str, *, default: int = 10, max_limit: int = 10) -> int:
        fallback = max(int(default or 10), 1)
        hard_limit = max(int(max_limit or fallback), 1)
        match = re.search(r"([0-9０-９]{1,2})\s*(?:件|個|ツイート|ポスト|tweet|post)", str(text or ""))
        if not match:
            return max(1, min(fallback, hard_limit))
        try:
            value = int(str(match.group(1)).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except ValueError:
            value = fallback
        return max(1, min(value, hard_limit))

    @staticmethod
    def parse_x_timeline_entries(text: str, *, limit: int = 10) -> list[dict[str, str]]:
        return []

    @staticmethod
    def format_x_timeline_summary(entries: list[dict[str, str]], *, limit: int = 10) -> str:
        return "Xホームタイムラインの投稿を抽出できませんでした。ログイン状態や表示状態を確認してください。"


def _load_chrome_mcp_xcom_helpers(helper_path: Path | None = None) -> Any:
    helper_path = (
        Path(helper_path)
        if helper_path is not None
        else Path(__file__).resolve().parents[2] / "chrome-mcp" / "xcom.py"
    )
    if not helper_path.is_file():
        log.warning(
            "chrome-mcp x.com helper not found at %s; using built-in fallback",
            helper_path,
        )
        return _MissingXComHelpers
    spec = importlib.util.spec_from_file_location(
        "discord_ai_bot_chrome_mcp_xcom",
        helper_path,
    )
    if spec is None or spec.loader is None:
        log.warning(
            "unable to load chrome-mcp x.com helper from %s; using built-in fallback",
            helper_path,
        )
        return _MissingXComHelpers
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        log.warning(
            "chrome-mcp x.com helper load failed path=%s err=%r; using built-in fallback",
            helper_path,
            e,
        )
        return _MissingXComHelpers
    missing = [
        name
        for name in (
            "DEFAULT_DYNAMIC_BROWSER_HOSTS",
            "DEFAULT_X_TIMELINE_LIMIT",
            "X_TIMELINE_HOME_URL",
            "X_TIMELINE_TWEET_SELECTOR",
            "extract_x_timeline_limit",
            "format_x_timeline_summary",
            "is_dynamic_browser_site",
            "is_login_wall_or_error_page",
            "is_x_timeline_request",
            "parse_x_timeline_entries",
        )
        if not hasattr(module, name)
    ]
    if missing:
        log.warning(
            "chrome-mcp x.com helper missing attributes path=%s missing=%s; using built-in fallback",
            helper_path,
            ",".join(missing),
        )
        return _MissingXComHelpers
    return module


class _MissingImgHelpers:
    @staticmethod
    def is_img_top_request(text: str) -> bool:
        return False


def _load_chrome_mcp_img_helpers(helper_path: Path | None = None) -> Any:
    helper_path = (
        Path(helper_path)
        if helper_path is not None
        else Path(__file__).resolve().parents[2] / "chrome-mcp" / "img2channet.py"
    )
    if not helper_path.is_file():
        log.warning("chrome-mcp img2channet.py not found at %s", helper_path)
        return _MissingImgHelpers
    spec = importlib.util.spec_from_file_location("discord_ai_bot_chrome_mcp_img", helper_path)
    if spec is None or spec.loader is None:
        log.warning("unable to load chrome-mcp img2channet.py")
        return _MissingImgHelpers
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        log.warning("chrome-mcp img2channet load failed: %r", e)
        return _MissingImgHelpers
    return module


# ---------------------------------------------------------------------------
# Term candidate / cache
# ---------------------------------------------------------------------------


def _normalize_candidate_term(term: str) -> str:
    value = _compact_whitespace(term)
    value = value.strip("「」『』\"'()（）[]【】.,!?！？:：")
    return value


def _known_term_cache_key(term: str) -> str:
    return _normalize_candidate_term(term).lower()


def _prune_known_term_cache(*, now: float | None = None) -> None:
    now_value = float(now if now is not None else time.monotonic())
    ttl_sec = _known_term_cache_ttl_sec()
    expired_before = now_value - ttl_sec

    stale_keys = [key for key, seen_at in _known_term_cache.items() if float(seen_at or 0.0) < expired_before]
    for key in stale_keys:
        _known_term_cache.pop(key, None)

    max_size = _known_term_cache_max_size()
    while len(_known_term_cache) > max_size:
        _known_term_cache.popitem(last=False)


def _clear_known_term_cache() -> None:
    _known_term_cache.clear()


def _remember_known_term(term: str) -> None:
    key = _known_term_cache_key(term)
    if not key or key in {item.lower() for item in _GENERIC_TERM_STOPWORDS} or len(key) <= 1:
        return
    now = time.monotonic()
    _known_term_cache.pop(key, None)
    _known_term_cache[key] = now
    _prune_known_term_cache(now=now)


def _remember_known_terms_from_text(text: str) -> None:
    explicit = extract_explicit_research_term(text)
    if explicit:
        _remember_known_term(explicit)
    for term in extract_named_term_candidates(text):
        _remember_known_term(term)


def _is_known_term_cached(term: str) -> bool:
    key = _known_term_cache_key(term)
    if not key:
        return False
    _prune_known_term_cache()
    return key in _known_term_cache


# ---------------------------------------------------------------------------
# Specialized term detection
# ---------------------------------------------------------------------------


def _looks_like_specialized_unknown_term(term: str) -> bool:
    value = _normalize_candidate_term(term)
    if not value:
        return False
    if any(char.isdigit() for char in value):
        return True
    if any(char in "+._/-" for char in value):
        return True

    script_count = 0
    for pattern in (r"[A-Za-z]", r"[ぁ-ゖ]", r"[ァ-ヶー]", r"[一-龥]"):
        if re.search(pattern, value):
            script_count += 1
    if script_count >= 2:
        return True

    uppercase_count = sum(1 for char in value if char.isupper())
    if uppercase_count >= 2:
        return True

    if re.fullmatch(r"[ァ-ヶー]{6,}", value):
        return True
    return False


_PREFERENCE_PREFIX_RE = re.compile(
    r"(?:好き(?:な)?|推し(?:の)?|お気に入り(?:の)?|おすすめ(?:の)?|オススメ(?:の)?|好み(?:の)?|一番(?:好き(?:な)?)?)\s*$",
    re.IGNORECASE,
)


def extract_explicit_research_term(text: str) -> str:
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return ""

    patterns = (
        r"[「『\"]([^「」『\"]{1,40})[」』\"]\s*(?:って何|ってなに|とは|とは何|とはなに|って誰|ってだれ|の意味)",
        r"([A-Za-z][A-Za-z0-9+._/-]{1,40}|[ぁ-ゖー]{2,40}|[ァ-ヶー]{2,40}|[一-龥]{2,20})\s*(?:って何|ってなに|とは|とは何|とはなに|って誰|ってだれ|の意味)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        # 直前に「好き」「推し」などの好み修飾句がある場合は用語定義質問ではなく好み質問
        prefix = normalized[: match.start()].rstrip()
        if _PREFERENCE_PREFIX_RE.search(prefix):
            continue
        candidate = _normalize_candidate_term(match.group(1))
        if (
            candidate
            and candidate not in _GENERIC_TERM_STOPWORDS
            and not re.match(r"^(?:好き|推し|お気に入り|おすすめ|オススメ|好み)", candidate)
        ):
            return candidate
    return ""


def extract_named_term_candidates(text: str) -> list[str]:
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return []

    candidates: list[str] = []
    for regex in (_ASCII_TERM_RE, _KATAKANA_TERM_RE, _KANJI_TERM_RE):
        for match in regex.findall(normalized):
            candidate = _normalize_candidate_term(match)
            if (
                not candidate
                or candidate in _GENERIC_TERM_STOPWORDS
                or len(candidate) <= 1
            ):
                continue
            candidates.append(candidate)

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped[:6]


# ---------------------------------------------------------------------------
# Context URL reference detection
# ---------------------------------------------------------------------------


def _looks_like_conversational_preference_followup(user_text: str, context: str = "") -> bool:
    """Return True for elliptical preference/opinion follow-ups that should stay in chat."""
    normalized = _compact_whitespace(_strip_urls(user_text))
    if not normalized:
        return False

    if _contains_any_keyword(normalized, _RESEARCH_REQUEST_KEYWORDS + _SOURCE_REQUEST_KEYWORDS):
        return False
    if _contains_any_keyword(normalized, _NEWS_KEYWORDS + _PRODUCT_SPEC_KEYWORDS):
        return False
    if _contains_any_keyword(normalized, _RECENCY_KEYWORDS):
        return False
    if extract_urls(user_text):
        return False

    if extract_explicit_research_term(normalized):
        return False

    has_question = any(marker in normalized for marker in ("?", "？", "誰", "だれ", "何", "なに", "どれ", "どっち"))
    if not has_question:
        return False

    if _contains_any_keyword(normalized, _PREFERENCE_OR_OPINION_MARKERS):
        return True

    if re.search(r"(そう|みたい|らしい|かも)(?:だね|だよ|だな|ですか|ですね|笑|w|W|。|！|\?|？|$)", normalized):
        return True

    if _contains_any_keyword(normalized, _CONVERSATIONAL_FOLLOWUP_MARKERS):
        return bool(context.strip()) or len(normalized) <= 40

    return False


def _looks_like_context_url_reference(text: str, is_conversational: bool = False) -> bool:
    """Return True when the latest utterance is clearly asking about a URL in context."""
    normalized = _compact_whitespace(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    has_target = _contains_any_keyword(lowered, _CONTEXT_URL_TARGET_KEYWORDS)
    has_action = _contains_any_keyword(lowered, _CONTEXT_URL_ACTION_KEYWORDS)
    has_strong_action = _contains_any_keyword(lowered, _CONTEXT_URL_STRONG_ACTION_KEYWORDS)
    has_question = any(marker in normalized for marker in ("?", "？", "とは", "って何", "ってなに"))
    
    base_match = bool(has_target and (has_action or has_strong_action or has_question))
    if base_match:
        return True
        
    if has_strong_action and not is_conversational:
        return True
        
    return False


def _looks_like_context_term_reference(text: str) -> bool:
    """Return True when the latest utterance clearly asks about a term in context."""
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return False
    has_target = _contains_any_keyword(normalized, ("これ", "それ", "あれ", "この", "その", "さっき", "今の"))
    has_explanation = _contains_any_keyword(normalized, _EXPLANATION_KEYWORDS) or any(
        marker in normalized for marker in ("?", "？")
    )
    return bool(has_target and has_explanation)


# ---------------------------------------------------------------------------
# Deep-dive followup detection
# ---------------------------------------------------------------------------


def _extract_ordinal_index(text: str) -> int | None:
    """テキストから序数インデックスを抽出する。見つからなければ None。"""
    for token, idx in _ORDINAL_TO_INDEX.items():
        if token in text:
            return idx
    return None


def _is_explicit_fresh_research_request(text: str, cached_mode: str, xcom: Any, img_helper: Any) -> bool:
    """直前キャッシュの深掘りではなく、新規取得として扱うべき明示要求を判定する。"""
    normalized = str(text or "").strip()
    if not normalized:
        return False

    if cached_mode == "x_timeline":
        try:
            return bool(xcom.is_x_timeline_request(normalized))
        except Exception:
            return False

    if cached_mode == "img_top5":
        try:
            if hasattr(img_helper, "is_img_top_request") and img_helper.is_img_top_request(normalized):
                return True
            if hasattr(img_helper, "is_img_thread_url") and img_helper.is_img_thread_url(normalized):
                return True
        except Exception:
            return False

    return False


def detect_deepdive_followup(
    user_text: str,
    last_research_cache: dict | None,
    *,
    xcom: Any = None,
    img_helper: Any = None,
) -> dict[str, Any]:
    if xcom is None:
        xcom = _xcom
    if img_helper is None:
        img_helper = _img_helper
    """直前の調査結果に対する深掘りフォローアップかどうかを判定する。

    Args:
        user_text: ユーザーの最新メッセージ
        last_research_cache: _channel_research_cache[cid] の値
        xcom: X.com ヘルパーモジュール
        img_helper: img2channet ヘルパーモジュール

    Returns:
        {
            "is_deepdive": bool,
            "deepdive_mode": str,    # "img_thread" / "x_deepdive" / ""
            "target_url": str,       # img_thread の場合のURL（空文字の場合あり）
            "reason": str,
        }
    """
    _no_deepdive: dict[str, Any] = {
        "is_deepdive": False,
        "deepdive_mode": "",
        "target_url": "",
        "reason": "",
    }

    if not last_research_cache or not isinstance(last_research_cache, dict):
        return _no_deepdive

    cached_at = float(last_research_cache.get("timestamp", 0.0) or 0.0)
    if cached_at > 0 and (time.monotonic() - cached_at) > _DEEPDIVE_CACHE_TTL_SEC:
        return _no_deepdive

    cached_mode = str(last_research_cache.get("mode") or "").strip()
    normalized = str(user_text or "").strip()
    if _is_explicit_fresh_research_request(normalized, cached_mode, xcom, img_helper):
        return _no_deepdive

    if cached_mode == "img_top5":
        if not _contains_any_keyword(normalized, _IMG_DEEPDIVE_FOLLOWUP_KEYWORDS):
            return _no_deepdive

        threads: list[dict] = list(last_research_cache.get("threads") or [])
        target_url = ""
        ordinal_idx = _extract_ordinal_index(normalized)
        if threads:
            if ordinal_idx is not None and ordinal_idx < len(threads):
                target_url = str(threads[ordinal_idx].get("url") or "").strip()
            else:
                best = max(threads, key=lambda t: int(t.get("res_count") or 0), default=None)
                if best:
                    target_url = str(best.get("url") or "").strip()

        return {
            "is_deepdive": True,
            "deepdive_mode": "img_thread",
            "target_url": target_url,
            "reason": "img_top5へのフォローアップ",
        }

    if cached_mode == "x_timeline":
        if not _contains_any_keyword(normalized, _X_DEEPDIVE_FOLLOWUP_KEYWORDS):
            return _no_deepdive

        return {
            "is_deepdive": True,
            "deepdive_mode": "x_deepdive",
            "target_url": "",
            "reason": "x_timelineへのフォローアップ",
        }

    return _no_deepdive


# ---------------------------------------------------------------------------
# Research rule detection
# ---------------------------------------------------------------------------


def _choose_query(user_text: str, context: str) -> str:
    user_candidate = _compact_whitespace(_strip_urls(user_text))
    if user_candidate:
        return user_candidate

    context_candidate = _compact_whitespace(_strip_urls(context))
    if context_candidate:
        return truncate_text(context_candidate, 180)

    urls = extract_urls(f"{user_text}\n{context}")
    if urls:
        return urls[0]
    return _compact_whitespace(user_text or context)


def looks_like_user_teaching_or_definition(text: str) -> bool:
    """ユーザーがAIに知識・設定・定義を教えている文かどうかを判定する。

    例:
    - 「ワスとは、2026年8月28日に〜キャラクターの名称です。〜の姿がワスです」
    - 「玉ちゃんはホモです。覚えてください。」
    - 「〇〇は〜のことだよ」
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False

    # 疑問表現・調査要求表現が含まれている場合は除外
    has_question_or_query = any(
        marker in normalized
        for marker in (
            "?", "？", "何", "なに", "誰", "だれ", "どう", "どこ", "いつ", "なぜ", "なんで",
            "教えて", "教えろ", "説明して", "説明しろ", "調べて", "調べろ", "検索して", "検索しろ",
            "知ってる", "知ってる？", "kwsk", "どういうこと", "何者", "ソース", "出典",
        )
    )
    if has_question_or_query:
        return False

    # 記憶・教示・定義構文の検出
    if any(mem_req in normalized for mem_req in ("覚えて", "記憶して", "おぼえて")):
        return True

    definition_endings = (
        "です", "である", "のこと", "のことです", "の事", "の事です",
        "の名称", "の名称です", "の名前", "の名前です", "の姿", "の姿です",
        "キャラクター", "キャラクターです", "キャラです", "キャラ",
        "だよ", "だね", "なんだ", "だ。", "です。", "である。", "こと。", "名称。"
    )
    if "とは" in normalized and any(normalized.endswith(ending) for ending in definition_endings):
        return True

    if any(marker in normalized for marker in ("の名称", "の名前", "の姿が", "のことです", "のことだよ")) and any(
        normalized.endswith(ending) for ending in definition_endings
    ):
        return True

    return False


def detect_reply_research_rules(
    user_text: str,
    context: str = "",
    *,
    xcom: Any = None,
    img_helper: Any = None,
) -> dict[str, Any]:
    if xcom is None:
        xcom = _xcom
    if img_helper is None:
        img_helper = _img_helper
    normalized_user_text = str(user_text or "").strip()
    normalized_context = str(context or "").strip()
    combined = "\n".join(part for part in (normalized_user_text, normalized_context) if part).strip()
    plain_text = _compact_whitespace(_strip_urls(combined))
    explicit_term = extract_explicit_research_term(normalized_user_text)
    named_terms = extract_named_term_candidates(normalized_user_text)
    if (
        not explicit_term
        and not named_terms
        and normalized_context
        and _looks_like_context_term_reference(normalized_user_text)
    ):
        explicit_term = extract_explicit_research_term(normalized_context)
        named_terms = extract_named_term_candidates(normalized_context)
    candidate_term = explicit_term or (named_terms[0] if named_terms else "")

    conversational_followup = _looks_like_conversational_preference_followup(
        normalized_user_text,
        normalized_context,
    )

    user_urls = extract_urls(normalized_user_text)
    context_urls = extract_urls(normalized_context)
    context_url_reference = (
        bool(context_urls)
        and not user_urls
        and _looks_like_context_url_reference(normalized_user_text, is_conversational=conversational_followup)
    )
    urls = user_urls or (context_urls if context_url_reference else [])
    url_present = bool(urls)
    x_timeline_flag = bool(xcom.is_x_timeline_request(normalized_user_text))
    img_top5_flag = bool(img_helper.is_img_top_request(normalized_user_text))
    img_thread_flag = bool(hasattr(img_helper, "is_img_thread_url") and img_helper.is_img_thread_url(normalized_user_text or combined))
    source_request = _contains_any_keyword(normalized_user_text, _SOURCE_REQUEST_KEYWORDS)
    research_request = source_request or _contains_any_keyword(normalized_user_text, _RESEARCH_REQUEST_KEYWORDS)
    recency_flag = (
        x_timeline_flag
        or _contains_any_keyword(normalized_user_text, _RECENCY_KEYWORDS)
        or bool(_YEAR_OR_DATE_RE.search(normalized_user_text))
    )
    news_flag = _contains_any_keyword(normalized_user_text, _NEWS_KEYWORDS)
    product_spec_flag = _contains_any_keyword(normalized_user_text, _PRODUCT_SPEC_KEYWORDS)
    
    teaching_statement = looks_like_user_teaching_or_definition(normalized_user_text)
    explanation_request = False if teaching_statement else (_contains_any_keyword(normalized_user_text, _EXPLANATION_KEYWORDS) or bool(explicit_term))
    
    known_term_cached = bool(candidate_term) and _is_known_term_cached(candidate_term)
    unknown_term_flag = (
        bool(candidate_term)
        and explanation_request
        and not conversational_followup
        and not known_term_cached
        and _looks_like_specialized_unknown_term(candidate_term)
    )
    question_like = any(marker in normalized_user_text for marker in ("?", "？", "って", "とは", "教えて", "知ってる"))

    mode: ResearchDispatchMode = "simple_search"
    if img_thread_flag:
        mode = "img_thread"
    elif img_top5_flag:
        mode = "img_top5"
    elif x_timeline_flag:
        mode = "x_timeline"
    elif url_present:
        mode = "browser_read_url"
    elif source_request or (research_request and (recency_flag or product_spec_flag or news_flag)):
        mode = "browser_search"

    rule_reasons: list[str] = []
    if img_thread_flag:
        rule_reasons.append("img_thread")
    if img_top5_flag:
        rule_reasons.append("img_top5")
    if x_timeline_flag:
        rule_reasons.append("x_timeline")
    if url_present:
        rule_reasons.append("url_present")
    if source_request:
        rule_reasons.append("source_request")
    if research_request:
        rule_reasons.append("explicit_research_request")
    if recency_flag:
        rule_reasons.append("recency")
    if news_flag:
        rule_reasons.append("news")
    if product_spec_flag:
        rule_reasons.append("product_spec")
    if unknown_term_flag:
        rule_reasons.append("unknown_term")

    topic_inquiry = detect_topic_opinion_inquiry(normalized_user_text)
    topic_opinion_flag = bool(topic_inquiry.get("is_opinion_inquiry"))
    if topic_opinion_flag:
        rule_reasons.append("topic_opinion_inquiry")

    soft_score = sum(
        1
        for flag in (
            recency_flag,
            news_flag,
            product_spec_flag,
            explanation_request and bool(candidate_term) and not conversational_followup,
        )
        if flag
    )
    hard_trigger = (
        img_thread_flag
        or img_top5_flag
        or x_timeline_flag
        or url_present
        or source_request
        or research_request
        or topic_opinion_flag
    )
    needs_research = hard_trigger or (not conversational_followup and (unknown_term_flag or soft_score >= 2))
    if teaching_statement and not (url_present or img_thread_flag or img_top5_flag or x_timeline_flag):
        needs_research = False

    should_consult_model = bool(
        not needs_research
        and not teaching_statement
        and (
            unknown_term_flag
            or (
                explanation_request
                and bool(candidate_term)
                and not conversational_followup
                and not known_term_cached
                and question_like
            )
            or (
                (news_flag or product_spec_flag)
                and (recency_flag or research_request or source_request)
            )
        )
    )

    if img_thread_flag:
        full_urls = (
            getattr(img_helper, "_extract_img_thread_urls_full", lambda t: [])(combined)
            if hasattr(img_helper, "_extract_img_thread_urls_full")
            else [u for u in urls if "img.2chan.net" in u]
        )
        query = full_urls[0] if full_urls else (urls[0] if urls else "")
    elif img_top5_flag:
        query = "https://img.2chan.net/b/futaba.php?mode=cat&sort=6"
    elif x_timeline_flag:
        query = str(getattr(xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home")
    elif topic_opinion_flag and topic_inquiry.get("search_query"):
        query = str(topic_inquiry.get("search_query") or "").strip()
    elif recency_flag or news_flag or product_spec_flag or research_request:
        query = _choose_query(normalized_user_text, normalized_context)
    else:
        query = urls[0] if urls else candidate_term or _choose_query(normalized_user_text, normalized_context)

    return {
        "needs_research": bool(needs_research),
        "mode": mode,
        "query": query,
        "candidate_term": candidate_term,
        "named_terms": named_terms,
        "topic_opinion_flag": topic_opinion_flag,
        "target_subject": str(topic_inquiry.get("target_subject") or ""),
        "target_aspect": str(topic_inquiry.get("target_aspect") or ""),
        "topic_inquiry": topic_inquiry,
        "img_thread_flag": img_thread_flag,
        "x_timeline_flag": x_timeline_flag,
        "url_present": url_present,
        "context_url_reference": context_url_reference,
        "source_request": source_request,
        "research_request": research_request,
        "recency_flag": recency_flag,
        "news_flag": news_flag,
        "product_spec_flag": product_spec_flag,
        "explanation_request": explanation_request,
        "unknown_term_flag": unknown_term_flag,
        "conversational_followup": conversational_followup,
        "known_term_cached": known_term_cached,
        "question_like": question_like,
        "rule_reasons": rule_reasons,
        "soft_score": soft_score,
        "hard_trigger": hard_trigger,
        "should_consult_model": should_consult_model,
        "stripped_text": plain_text,
    }


def resolve_research_mode(text: str, context: str = "", mode: str = "auto") -> ResearchDispatchMode:
    """テキストからリサーチモードを解決する。"""
    requested = str(mode or "auto").strip().lower()
    if requested in {"simple_search", "browser_search", "browser_read_url", "x_timeline", "img_top5", "img_thread"}:
        return requested  # type: ignore[return-value]

    rules = detect_reply_research_rules(text, context=context)
    resolved_mode = str(rules.get("mode") or "auto")
    
    # URLが含まれている場合、旧来の resolve_research_mode は「URLのみ」の時だけ browser_read_url を返していた
    if resolved_mode == "browser_read_url":
        if not looks_like_url_only_text(text):
            return "browser_search"
            
    return resolved_mode  # type: ignore[return-value]
