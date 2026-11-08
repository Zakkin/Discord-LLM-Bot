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
import math
import re
import socket
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, overload
from urllib.parse import urlparse

from lib.text_utils import extract_urls
from harness.agent.tool_registry.mcp.fact_check import (
    looks_like_url_only_text,
    should_build_link_summaries,
)
from ollama_bot.common.ollama_helpers import truncate_text
from lib.text_utils import compact_whitespace as _compact_whitespace

from .config import (
    _ASCII_TERM_RE,
    _BROWSER_BOILERPLATE_PATTERNS,
    _COMMON_KATAKANA_STOPWORDS,
    _CONTEXT_URL_ACTION_KEYWORDS,
    _CONTEXT_URL_STRONG_ACTION_KEYWORDS,
    _CONTEXT_URL_TARGET_KEYWORDS,
    _CONVERSATIONAL_FOLLOWUP_MARKERS,
    _DEFAULT_ALLOWED_URL_SCHEMES,
    _DEFAULT_X_TIMELINE_HARD_LIMIT,
    _DEEPDIVE_CACHE_TTL_SEC,
    _EXPLANATION_KEYWORDS,
    _FACT_CHECK_HEARSAY_RE,
    _FACT_CHECK_KEYWORDS,
    _FACT_CHECK_QUESTION_RE,
    _FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS,
    _FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN,
    _FALLBACK_XCOM_LOGIN_WALL_PHRASES,
    _FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES,
    _GENERIC_TERM_STOPWORDS,
    _IMAGE_SEARCH_TRIGGER_KEYWORDS,
    _IMG_DEEPDIVE_FOLLOWUP_KEYWORDS,
    _KANJI_TERM_RE,
    _KATAKANA_TERM_RE,
    _NEWS_KEYWORDS,
    _NOISE_DOMAINS,
    _NOISE_DOMAIN_KEYWORD_PATTERNS,
    _NUMBER_UNIT_TERM_RE,
    _ORDINAL_TO_INDEX,
    _PREFERENCE_OR_OPINION_MARKERS,
    _PRODUCT_SPEC_KEYWORDS,
    _QUERY_TERM_RE,
    _RECENCY_KEYWORDS,
    _RESEARCH_REQUEST_KEYWORDS,
    _SIMULATION_IMPACT_KEYWORDS,
    _SIMULATION_IMPACT_QUESTION_RE,
    _SOURCE_REQUEST_KEYWORDS,
    _TRUSTED_FACT_DOMAINS,
    _X_DEEPDIVE_FOLLOWUP_KEYWORDS,
    _YEAR_OR_DATE_RE,
    ResearchDispatchMode,
    _known_term_cache_max_size,
    _known_term_cache_ttl_sec,
    _safe_browser_page_limit,
    _web_research_recency_prefer_x_search,
)
from ollama_bot.common.config_helpers import cfg_bool
from .topic_investigation import (
    detect_consultation_or_problem_solving_inquiry,
    detect_topic_opinion_inquiry,
)

log = logging.getLogger("ollama_bot.common.web_research")


def get_generic_term_stopwords() -> set[str]:
    """基本ストップワードに、現在稼働中Botの表示名・一人称・別名（BOT_ALIASES等）を動的に合算して返す。"""
    stopwords = set(_GENERIC_TERM_STOPWORDS)
    try:
        from lib.config_utils import cfg
        fp = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
        if fp:
            stopwords.add(fp)
        bd = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if bd:
            stopwords.add(bd)
        aliases = cfg("BOT_ALIASES", ())
        if isinstance(aliases, (list, tuple, set)):
            for a in aliases:
                al = str(a).strip()
                if al:
                    stopwords.add(al)
    except Exception:
        pass
    return stopwords

# 実行時に動的にインポートされるヘルパーモジュール
_xcom: Any = None
_img_helper: Any = None


def _init_helpers(xcom: Any, img_helper: Any) -> None:
    global _xcom, _img_helper
    _xcom = xcom
    _img_helper = img_helper


def _ensure_helpers() -> None:
    global _xcom, _img_helper
    if _xcom is None:
        _xcom = _load_chrome_mcp_xcom_helpers()
    if _img_helper is None:
        _img_helper = _load_chrome_mcp_img_helpers()

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


_NOISE_BLOCK_TAGS_RE = re.compile(
    r"<(nav|footer|header|aside|noscript|svg|form|menu|dialog|template)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", flags=re.IGNORECASE | re.DOTALL)


def _cleanup_browser_text(text: str) -> str:
    value = str(text or "")
    value = _HTML_COMMENT_RE.sub(" ", value)
    value = _SCRIPT_STYLE_RE.sub(" ", value)
    for _ in range(2):
        new_val = _NOISE_BLOCK_TAGS_RE.sub(" ", value)
        if new_val == value:
            break
        value = new_val
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


async def _resolve_browser_host_addresses(
    hostname: str,
    port: int | None = None,
) -> list[str]:
    normalized = str(hostname or "").strip()
    if not normalized:
        return []
    loop = asyncio.get_running_loop()
    resolved_port = port if port is not None else 80
    try:
        results = await loop.getaddrinfo(
            normalized,
            resolved_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as gai_err:
        log.debug(
            "getaddrinfo AF_UNSPEC failed for host=%s port=%s, retrying with AF_INET: %r",
            normalized,
            resolved_port,
            gai_err,
        )
        try:
            results = await loop.getaddrinfo(
                normalized,
                resolved_port,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        except Exception as retry_err:
            log.warning(
                "getaddrinfo AF_INET retry also failed for host=%s port=%s: %r",
                normalized,
                resolved_port,
                retry_err,
            )
            raise retry_err from gai_err
    except Exception as e:
        log.warning(
            "getaddrinfo unexpected failure for host=%s port=%s: %r",
            normalized,
            resolved_port,
            e,
        )
        raise

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

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80

    try:
        resolved_addresses = await _resolve_browser_host_addresses(hostname, port=port)
    except Exception as e:
        # すでに _is_safe_public_browser_url(url) でスキーム（HTTP/HTTPS）、非localhost、非.local、
        # 非プライベートIPリテラル（127.0.0.1, 192.168.x.x, 10.x.x.x, 169.254.x.x 等）を通過している正規ドメイン形式の場合、
        # 一時的なDNS解決エラー（gaierror等）が発生しても即座に危険URLとして遮断せず、
        # 警告ログを記録した上で安全にブラウザ（Chrome MCP）側の名前解決・閲覧へ委ねるフォールバックを設ける。
        log.warning(
            "browser URL host resolution failed host=%s port=%s err=%r (allowing browser MCP to resolve)",
            hostname,
            port,
            e,
        )
        return True

    if not resolved_addresses:
        log.warning(
            "browser URL host resolution returned empty host=%s port=%s (allowing browser MCP to resolve)",
            hostname,
            port,
        )
        return True

    return all(_is_safe_public_ip_address(address) for address in resolved_addresses)


# ---------------------------------------------------------------------------
# Domain reliability & Noise filtering
# ---------------------------------------------------------------------------


def is_noise_domain(url: str) -> bool:
    """一般掲示板・知恵袋・成人向け漫画/アップローダー等のノイズドメインか判定する。"""
    if not url:
        return False
    try:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").lower()
    except Exception:
        hostname = str(url or "").lower()
    if not hostname:
        return False
    for noise in _NOISE_DOMAINS:
        if hostname == noise or hostname.endswith("." + noise):
            return True
    for kw in _NOISE_DOMAIN_KEYWORD_PATTERNS:
        if kw in hostname:
            return True
    return False


def is_trusted_fact_domain(url: str) -> bool:
    """信頼できる百科事典・辞書・公的機関・大手ニュースサイトのドメインか判定する。"""
    if not url:
        return False
    try:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").lower()
    except Exception:
        hostname = str(url or "").lower()
    if not hostname:
        return False
    if hostname.endswith(".go.jp") or hostname.endswith(".ac.jp") or hostname.endswith(".ed.jp"):
        return True
    for trusted in _TRUSTED_FACT_DOMAINS:
        if hostname == trusted or hostname.endswith("." + trusted):
            return True
    return False


_is_noise_domain = is_noise_domain
_is_trusted_fact_domain = is_trusted_fact_domain


# ---------------------------------------------------------------------------
# Fact-check & Query refinement helpers
# ---------------------------------------------------------------------------


def looks_like_simulation_or_impact_verification(text: str, context: str = "") -> bool:
    """〜したらどうなる、人体への影響、致死量などのシミュレーション・客観的検証疑問文であるかを判定する。"""
    combined = f"{text}\n{context}".strip()
    if not combined:
        return False
    if any(kw in combined for kw in _SIMULATION_IMPACT_KEYWORDS):
        return True
    if bool(_SIMULATION_IMPACT_QUESTION_RE.search(combined)):
        return True
    return False


def get_matched_fact_check_pattern(text: str, context: str = "") -> str:
    """発言や文脈がファクトチェック・真偽検証等に合致した際の一致パターン文字列を返却する。"""
    combined = f"{text}\n{context}".strip()
    if not combined:
        return ""
    for kw in _FACT_CHECK_KEYWORDS:
        if kw in combined:
            return f"keyword:{kw}"
    m_fc = _FACT_CHECK_QUESTION_RE.search(combined)
    if m_fc:
        return f"question_re:{m_fc.group(0)}"
    m_hs = _FACT_CHECK_HEARSAY_RE.search(combined)
    if m_hs:
        return f"hearsay_re:{m_hs.group(0)}"
    if looks_like_simulation_or_impact_verification(text, context):
        return "simulation_impact"
    return ""


def looks_like_fact_check_or_verification(text: str, context: str = "") -> bool:
    """発言や文脈がファクトチェック・真偽検証・公式設定確認・伝聞確認・シミュレーション影響検証の依頼であるかを判定する。"""
    return bool(get_matched_fact_check_pattern(text, context))


_IMAGE_URL_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:\?.*)?$", re.IGNORECASE)
_DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>\"']+",
    re.IGNORECASE,
)


def looks_like_image_search_request(user_text: str) -> bool:
    """発言が添付画像や画像に関する人物・キャラクター・元ネタ・出典の特定要求であるかを判定する。"""
    if not user_text:
        return False
    if any(kw in user_text for kw in _IMAGE_SEARCH_TRIGGER_KEYWORDS):
        return True
    clean = re.sub(r"[\s\?？!！、。・「」『』]+", "", user_text)
    if any(kw in clean for kw in _IMAGE_SEARCH_TRIGGER_KEYWORDS):
        return True
    if re.search(r"(?:これ|このキャラ|この人|この娘|何のキャラ|何の画像)[はが]?(?:誰|だれ|何|なに)", clean):
        return True
    if re.search(r"(?:誰|だれ)[なん]?だ[よ]?(?:これ|この)", clean):
        return True
    return False


def has_image_context(user_text: str, context: str = "") -> bool:
    """発言や文脈内に添付画像、画像解析内容、画像URLが存在するかを判定する。"""
    combined = f"{user_text}\n{context}".strip()
    if not combined:
        return False
    if any(marker in combined for marker in ("添付画像", "画像解析内容", "[画像]", "画像URL", "画像ファイル", "attachment")):
        return True
    if _DISCORD_CDN_RE.search(combined):
        return True
    for u in extract_urls(combined):
        if _IMAGE_URL_EXT_RE.search(u):
            return True
    return False


def extract_core_noun_for_query_refinement(query: str, text: str = "") -> str:
    """複合キーワードから再検索用の固有名詞・主要キーワードを抽出する。"""
    explicit = extract_explicit_research_term(query) or extract_explicit_research_term(text)
    if explicit:
        return explicit
    named_terms = extract_named_term_candidates(query) or extract_named_term_candidates(text)
    exclude = set(get_generic_term_stopwords()) | set(_COMMON_KATAKANA_STOPWORDS) | {
        "公式", "設定", "公式設定", "エピローグ", "小説", "小説版", "本当", "事実", "デマ", "ガセ",
        "真偽", "検証", "裏取り", "キャラクター", "作品", "作者", "登場人物", "主人公",
        "キャラ", "新キャラ", "情報", "ニュース", "最新",
    }
    for term in named_terms:
        if term not in exclude and len(term) >= 2:
            return term
    terms = _query_terms(query)
    for term in terms:
        if term not in exclude and len(term) >= 2:
            return term
    return named_terms[0] if named_terms else (terms[0] if terms else "")


def generate_refined_queries(query: str, user_text: str = "", context: str = "") -> list[str]:
    """検索結果不十分時に試行するリファインクエリリスト（2nd pass用）を生成する。"""
    clean_query = str(query or "").strip()
    core_noun = extract_core_noun_for_query_refinement(clean_query, f"{user_text} {context}")
    if not core_noun:
        return []

    candidates: list[str] = [f"{core_noun} とは", core_noun]
    refined: list[str] = []
    for cand in candidates:
        if cand != clean_query and cand not in refined:
            refined.append(cand)
    return refined


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


def _query_terms(query: str | list[str]) -> list[str]:
    if isinstance(query, (list, tuple, set)):
        raw_queries = list(query)
    else:
        raw_queries = [str(query or "")]

    terms: list[str] = []
    for q in raw_queries:
        for match in _QUERY_TERM_RE.findall(str(q or "")):
            candidate = _normalize_candidate_term(match)
            if len(candidate) <= 1:
                continue
            lowered = candidate.lower()
            if lowered in {term.lower() for term in terms}:
                continue
            terms.append(candidate)
            if len(terms) >= 12:
                break
        if len(terms) >= 12:
            break
    return terms[:12] if len(raw_queries) > 1 else terms[:8]


def _split_text_chunks(text: str) -> list[str]:
    normalized = str(text or "").replace("。", "。\n").replace("！", "！\n").replace("？", "？\n")
    chunks = [_compact_whitespace(chunk) for chunk in normalized.splitlines()]
    return [chunk for chunk in chunks if len(chunk) >= 20]


def _split_into_sentences(text: str) -> list[str]:
    """テキストを句点（。！？\n）で意味のある文に分割する。"""
    normalized = str(text or "").replace("。", "。\n").replace("！", "！\n").replace("？", "？\n")
    sentences: list[str] = []
    for line in normalized.splitlines():
        s = _compact_whitespace(line)
        if len(s) >= 8:
            sentences.append(s)
    return sentences


def _query_term_weight(term: str) -> int:
    """クエリ単語の種類に応じた重要度スコア（カタカナ固有名詞/型番: 6, 漢字熟語: 4, その他: 1）を返す。"""
    t = term.strip()
    if not t:
        return 0
    # カタカナ固有名詞（3文字以上）
    if _KATAKANA_TERM_RE.fullmatch(t) and len(t) >= 3:
        return 6
    # 英数字型番の一致（英字+数字の組み合わせ、または3文字以上の英数）
    if _ASCII_TERM_RE.fullmatch(t) or (any(c.isalnum() for c in t) and (any(c.isdigit() for c in t) or len(t) >= 3)):
        return 6
    # 漢字熟語（2文字以上）
    if _KANJI_TERM_RE.fullmatch(t) and len(t) >= 2:
        return 4
    # ひらがな・一般語
    return 1


@overload
def _calculate_simple_idf(sentences: list[str], terms: str) -> float: ...


@overload
def _calculate_simple_idf(sentences: list[str], terms: list[str]) -> dict[str, float]: ...


def _calculate_simple_idf(
    sentences: list[str],
    terms: list[str] | str,
) -> dict[str, float] | float:
    """各クエリ単語の簡易IDF（逆文書頻度）重みを計算する。

    ページ内で頻出する一般語・サイト内共通語のスコア歪みを抑制し、
    特定の文にのみ出現する識別性の高いキーワードを重視する。
    """
    if isinstance(terms, str):
        t_low = terms.lower().strip()
        n = len(sentences)
        if n == 0 or not t_low:
            return 1.0
        df = sum(1 for s in sentences if t_low in s.lower())
        return max(math.log((n + 1.0) / (df + 1.0)) + 0.5, 0.2)

    n = len(sentences)
    if n == 0:
        return {t: 1.0 for t in terms}

    lowered_sentences = [s.lower() for s in sentences]
    idf_map: dict[str, float] = {}
    for term in terms:
        t_low = term.lower().strip()
        if not t_low:
            idf_map[term] = 1.0
            continue
        df = sum(1 for s in lowered_sentences if t_low in s)
        idf_map[term] = max(math.log((n + 1.0) / (df + 1.0)) + 0.5, 0.2)

    return idf_map


def _looks_like_boilerplate(chunk: str) -> bool:
    lowered = str(chunk or "").lower()
    if any(pattern in lowered for pattern in _BROWSER_BOILERPLATE_PATTERNS):
        return True
    if lowered.count("|") >= 4:
        return True
    return False


def _select_relevant_excerpt(text: str, query: str | list[str], *, max_chars: int = 1600) -> str:
    """スライディングウィンドウパッセージ抽出（前後文脈の包摂と複合スコアリング）。
    
    文単体ではなく中心文の前後に1文を含めた3文ウィンドウを評価し、
    指示語（これ、その等）を含む文脈の欠落を防止する。
    簡易IDF重み付け（_calculate_simple_idf）により一般語によるスコア歪みを抑制し、
    最終的なパッセージはインデックス順に結合して抽出する。
    """
    sentences = [s for s in _split_into_sentences(text) if not _looks_like_boilerplate(s)]
    if not sentences:
        return truncate_text(_compact_whitespace(text), max_chars)

    query_terms = _query_terms(query)
    if not query_terms:
        packed: list[str] = []
        cur_len = 0
        for s in sentences:
            if cur_len + len(s) + 1 > max_chars:
                break
            packed.append(s)
            cur_len += len(s) + 1
        return " ".join(packed) if packed else truncate_text(sentences[0], max_chars)

    idf_weights = _calculate_simple_idf(sentences, query_terms)
    assert isinstance(idf_weights, dict)

    n = len(sentences)
    scored_windows: list[tuple[float, int, list[int]]] = []
    for i in range(n):
        start_idx = max(0, i - 1)
        end_idx = min(n, i + 2)
        window_indices = list(range(start_idx, end_idx))
        window_text = " ".join(sentences[k] for k in window_indices)
        lowered_window = window_text.lower()

        score = 0.0
        matched_terms: set[str] = set()
        for term in query_terms:
            t_low = term.lower()
            if t_low in lowered_window:
                matched_terms.add(term)
                term_w = float(_query_term_weight(term))
                idf = idf_weights.get(term, 1.0)
                score += term_w * idf

        matched_count = len(matched_terms)
        if matched_count >= 2:
            score += matched_count * 4.0

        if _looks_like_boilerplate(window_text):
            score -= 20.0

        scored_windows.append((score, -start_idx, window_indices))

    # スコア上位順にソート（同点時は start_idx が小さい先頭文を優先）
    scored_windows.sort(key=lambda item: (item[0], item[1]), reverse=True)

    selected_indices: set[int] = set()
    for score, _, indices in scored_windows:
        if score <= 0.0 and selected_indices:
            continue
        candidate_indices = sorted(selected_indices | set(indices))
        total_len = sum(len(sentences[k]) for k in candidate_indices) + max(0, len(candidate_indices) - 1)
        if total_len <= max_chars:
            selected_indices.update(indices)
        elif not selected_indices:
            for idx in indices:
                cand = sorted(selected_indices | {idx})
                cand_len = sum(len(sentences[k]) for k in cand) + max(0, len(cand) - 1)
                if cand_len <= max_chars:
                    selected_indices.add(idx)
            break

    if not selected_indices:
        for i in range(n):
            cand = sorted(selected_indices | {i})
            cand_len = sum(len(sentences[k]) for k in cand) + max(0, len(cand) - 1)
            if cand_len <= max_chars:
                selected_indices.add(i)
            else:
                break

    sorted_indices = sorted(selected_indices)
    extracted = " ".join(sentences[k] for k in sorted_indices)
    return truncate_text(extracted, max_chars)


# ---------------------------------------------------------------------------
# Helper Loaders & Fallbacks
# ---------------------------------------------------------------------------

from front.hutaba.tools import (
    _MissingImgHelpers,
    _load_chrome_mcp_img_helpers,
)
from front.x.loader import (
    _MissingXComHelpers,
    _load_chrome_mcp_xcom_helpers,
)


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
    if not key or key in {item.lower() for item in get_generic_term_stopwords()} or len(key) <= 1:
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
    if value in _COMMON_KATAKANA_STOPWORDS or value in get_generic_term_stopwords():
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


_SCIENTIFIC_NOTATION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d+(?:\.\d+)?|\.\d+)[eE][+-]?\d+(?![A-Za-z0-9_])"
)
_MATH_CALCULATION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d+\s*[\+\-\*\/×÷^]\s*\d+"
)
_MATH_EXPLANATION_PATTERN = re.compile(
    r"(?:10の(?:累乗|べき乗|乗)|指数(?:表記)?|科学(?:的)?表記|累乗を表す|記号[「『\"']?[eE][」』\"']?の意味)",
    re.IGNORECASE,
)


def looks_like_scientific_notation_or_math(text: str) -> bool:
    """発言が科学的指数表記（例: 12e23, 1e10 等）や単純計算式、数学的表記の意味質問であるかを判定する。
    これらは外部Web調査ではなくLLM自身の推論・数学知識で答えるべき対象である。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if _SCIENTIFIC_NOTATION_PATTERN.search(normalized):
        return True
    if _MATH_CALCULATION_PATTERN.search(normalized):
        return True
    if _MATH_EXPLANATION_PATTERN.search(normalized):
        return True
    return False


_PUZZLE_ARROW_PATTERN = re.compile(r"(?:→|->|=>|⇒)")
_PUZZLE_BLANK_PATTERN = re.compile(r"[○◯□？\?]")
_PUZZLE_HINT_PATTERN = re.compile(
    r"(?:^|[\s、,。])ヒント(?:[、,.:：\s]|は|として|だと)|"
    r"(?:[0-9０-９一二三四五六七八九十]+(?:つ|個)?\s*)?画数(?:が|を|に|は|の|で|増|減)|"
    r"(?:文字数|母音|子音|アナグラム|なぞなぞ|謎解き|並び替え|穴埋め|法則)(?:は|が|に|の|で|を|：|:)",
    re.IGNORECASE,
)


def looks_like_puzzle_or_riddle(text: str) -> bool:
    """発言が文字パズル、なぞなぞ、暗号の出題やヒント提示であるかを判定する。
    これらは外部Web調査ではなくLLM自身の推論・言語知識で答えるべき対象である。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False

    if _PUZZLE_HINT_PATTERN.search(normalized):
        return True

    arrow_matches = _PUZZLE_ARROW_PATTERN.findall(normalized)
    has_blank = bool(_PUZZLE_BLANK_PATTERN.search(normalized))

    if len(arrow_matches) >= 2 and has_blank:
        return True
    if len(arrow_matches) >= 1 and re.search(r"(?:→|->|=>|⇒)\s*[○◯□？\?]", normalized):
        return True

    return False


_DAILY_ACTION_VERB_RE = re.compile(
    r"(?:"
    # 調理・食事・飲食
    r"(?:焼く|焼いた|焼いてる|焼きます|焼こう|食べる|食べた|食べてる|食べます|食べよう|食う|食った|食ってる|食おう|飲む|飲んだ|飲んでる|飲みます|飲もう|作る|作った|作ってる|作ります|作ろう|炒める|炒めた|炒めてる|炒めよう|煮る|煮た|煮てる|煮よう|茹でる|茹でた|茹でてる|茹でよう|揚げる|揚げた|揚げてる|揚げよう|炊く|炊いた|炊いてる|温める|温めた|温めてる|自炊|料理する|調理する)"
    r"|"
    # 購買・入手
    r"(?:買っ(?:てきた|た|てくる|てみた)|買います|買いに行く|買おう|ポチっ(?:た|てる)|届い(?:た|てる)|仕入れ(?:た|てきた)|注文し(?:た|てる))"
    r"|"
    # 移動・外出・生活・作業
    r"(?:に?行く|行っ(?:た|てきた|てくる)|行きます|行こう|帰る|帰っ(?:た|てきた)|帰宅し(?:た|てる)|出かけ(?:る|た|てる)|散歩し(?:る|た|てる)|寝る|寝た|起きた|昼寝し(?:る|た|てる)|作業し(?:る|た|てる)|掃除し(?:る|た|てる)|洗濯し(?:る|た|てる)|片付け(?:る|た|てる)|遊(?:ぶ|んだ|んでる)|プレイし(?:る|た|てる))"
    r")",
    re.IGNORECASE,
)


def looks_like_daily_action_or_monologue(text: str) -> bool:
    """発言が日常行動報告、調理・食事、購買、近況独白などの平文であるかを判定する。

    疑問符（? / ？）や質問表現（「って何」「とは」「教えて」「知ってる」等）を含まず、
    「〜で焼く」「〜食べる」「〜買ってきた」「〜に行く」などの日常行動・独白である場合、
    未知カタカナ語が含まれていてもスラング/未知語Webリサーチ誤爆を抑制する。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False

    # 1. URLが含まれる場合はWebリサーチ対象として保護
    if extract_urls(normalized):
        return False

    # 2. 疑問符が含まれる場合は質問文とみなす
    if any(q in normalized for q in ("?", "？")):
        return False

    # 3. 調査・検索・出典要求キーワードが含まれる場合はリサーチ対象
    if _contains_any_keyword(normalized, _RESEARCH_REQUEST_KEYWORDS + _SOURCE_REQUEST_KEYWORDS):
        return False

    # 4. 疑問詞・質問表現・説明要求マーカーが含まれる場合は除外
    question_markers = (
        "って何", "ってなに", "とは何", "とはなに", "何それ", "なにそれ", "何者",
        "の意味", "どういうこと", "どういう意味", "どう思う", "どうですか", "どうなの", "どうなん",
        "教えて", "教えろ", "説明して", "説明しろ", "知ってる", "知ってますか", "知ってる？",
        "何", "なに", "誰", "だれ", "どう", "どこ", "いつ", "なぜ", "なんで",
        "kwsk", "方法", "やり方", "やりかた", "仕方", "しかた",
        "聞きたい", "困って", "途方に暮れ", "どうすりゃ", "どうすれば",
        "してる人いない", "やってる人いない", "いい方法", "代替", "対策",
    )
    if any(marker in normalized for marker in question_markers):
        return False

    # 5. 「とは」「というのは」などの定義質問文の除外
    if "とは" in normalized or "というのは" in normalized:
        return False

    # 6. ファクトチェック・シミュレーション疑問・画像特定要求の除外
    if looks_like_fact_check_or_verification(normalized):
        return False
    if looks_like_simulation_or_impact_verification(normalized):
        return False
    if looks_like_image_search_request(normalized):
        return False

    # 7. 日常行動・調理・食事・購買・生活独白パターンの検出
    if _DAILY_ACTION_VERB_RE.search(normalized):
        return True

    return False


_PREFERENCE_PREFIX_RE = re.compile(
    r"(?:好き(?:な)?|推し(?:の)?|お気に入り(?:の)?|おすすめ(?:の)?|オススメ(?:の)?|好み(?:の)?|一番(?:好き(?:な)?)?)\s*$",
    re.IGNORECASE,
)


def _is_greeting_or_pure_chitchat(text: str) -> bool:
    """発言が一般的な挨拶・相槌・笑い表現などの定型句だけで構成されているかを判定する。"""
    cleaned = _compact_whitespace(re.sub(r"[!！?？〜～wW。、.\s\-_/]+", "", text)).lower()
    if not cleaned:
        return True
    greetings = {
        # 挨拶
        "こんにちは", "こんにちわ", "こんばんは", "こんばんわ", "おはよう", "おやすみ",
        "お疲れ様", "お疲れ様です", "お疲れ", "おつかれ", "おつかれさま", "よろしく",
        "よろしくお願いします", "よろしくね", "ありがとう", "ありがとうございます",
        "サンキュー", "どうも", "バイバイ", "またね", "じゃあね",
        # 相槌・返事・日常リアクション
        "なるほど", "そうなんだ", "確かに", "たしかに", "それな", "わかる", "わかります",
        "うん", "はい", "いいえ", "ううん", "了解", "了解です", "りょ", "オッケー", "おっけー",
        "オーケー", "ok", "草", "笑", "ワロタ", "草生える", "すげえ", "すごい", "やばい",
        # 動作確認・テスト
        "テスト", "てすと", "チェック",
    }
    return cleaned in greetings


def extract_explicit_research_term(text: str) -> str:
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return ""

    # 科学的指数表記・数式・数学的表記が含まれている場合は途中の英字（例: 12e23 の e23）を抽出しない
    if looks_like_scientific_notation_or_math(normalized):
        return ""

    # パズル・なぞなぞ・ヒント文が含まれている場合は除外
    if looks_like_puzzle_or_riddle(normalized):
        return ""

    patterns = (
        r"[「『\"]([^「」『\"]{1,40})[」』\"]\s*(?:って何|ってなに|とは|とは何|とはなに|って誰|ってだれ|の意味)",
        r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9+._/-]{1,40}|[ぁ-ゖー]{2,40}|[ァ-ヶー]{2,40}|[一-龥]{2,20})\s*(?:って何|ってなに|とは|とは何|とはなに|って誰|ってだれ|の意味)",
    )
    generic_stopwords = get_generic_term_stopwords()
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
            and candidate not in generic_stopwords
            and not re.match(r"^(?:好き|推し|お気に入り|おすすめ|オススメ|好み)", candidate)
        ):
            return candidate
    return ""


_QUOTED_TERM_RE = re.compile(r"[「『\"“]([^「」『\"”\n]{1,30})[」』\"”]")


def extract_quoted_terms(text: str) -> list[str]:
    """テキストからカギ括弧や引用符で囲まれた語（俗称・スラング・ミーム候補）を抽出する。"""
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return []
    terms: list[str] = []
    generic_stopwords = get_generic_term_stopwords()
    for m in _QUOTED_TERM_RE.finditer(normalized):
        candidate = _normalize_candidate_term(m.group(1).strip())
        if candidate and len(candidate) >= 2 and candidate not in generic_stopwords:
            if candidate not in terms:
                terms.append(candidate)
    return terms


def extract_named_term_candidates(text: str) -> list[str]:
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return []

    candidates: list[str] = []
    occupied_spans: list[tuple[int, int]] = []
    generic_stopwords = get_generic_term_stopwords()

    # 1. 最優先: 数値＋単位・複合固有名詞（例: 「10万ボルト」「50mA」等）の抽出
    for m in _NUMBER_UNIT_TERM_RE.finditer(normalized):
        candidate = _normalize_candidate_term(m.group(0))
        if not candidate or len(candidate) <= 1:
            continue
        candidates.append(candidate)
        occupied_spans.append((m.start(), m.end()))

    # 2. ASCII語、カタカナ語、漢字語の抽出（数値＋単位に包含される部分文字列の分解抽出を遮断）
    for regex in (_ASCII_TERM_RE, _KATAKANA_TERM_RE, _KANJI_TERM_RE):
        for m in regex.finditer(normalized):
            m_start, m_end = m.start(), m.end()
            # 既存の複合語（数値＋単位等）のスパンと重複している場合は分解とみなしてスキップ
            if any(m_start < occ_end and m_end > occ_start for occ_start, occ_end in occupied_spans):
                continue

            candidate = _normalize_candidate_term(m.group(0))
            if (
                not candidate
                or candidate in generic_stopwords
                or candidate in _COMMON_KATAKANA_STOPWORDS
                or len(candidate) <= 1
            ):
                continue
            candidates.append(candidate)
            occupied_spans.append((m_start, m_end))

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

    # 「どんな内容だった？」「何の内容？」「どんな中身？」等の一般的な雑談質問は、
    # 指示代名詞（その、あの、この、それ）や明示的対象（URL、リンク、記事、スレ）を伴わない限りURL参照としない
    casual_content_question_markers = ("どんな内容", "何の内容", "どういう内容", "どんな中身", "何の中身", "どういう中身", "どんな話", "何の話")
    if any(marker in lowered for marker in casual_content_question_markers):
        explicit_url_markers = ("url", "リンク", "記事", "ページ", "サイト", "貼って", "貼られ", "そのスレ", "このスレ", "あのスレ")
        if not any(marker in lowered for marker in explicit_url_markers):
            return False

    has_target = _contains_any_keyword(lowered, _CONTEXT_URL_TARGET_KEYWORDS)
    has_action = _contains_any_keyword(lowered, _CONTEXT_URL_ACTION_KEYWORDS)
    has_strong_action = _contains_any_keyword(lowered, _CONTEXT_URL_STRONG_ACTION_KEYWORDS)
    has_question = any(marker in normalized for marker in ("?", "？", "とは", "って何", "ってなに"))

    # 「内容」「中身」「本文」単体 + 疑問符だけの場合は、指示語（それ、その、これ、この等）がなければ除外
    if has_target and not (has_action or has_strong_action):
        weak_targets = ("内容", "中身", "本文")
        if any(wt in lowered for wt in weak_targets):
            has_pointer = any(pt in lowered for pt in ("これ", "それ", "あれ", "この", "その", "あの", "url", "リンク", "記事", "ページ", "スレ"))
            if not has_pointer:
                return False

    base_match = bool(has_target and (has_action or has_strong_action or has_question))
    if base_match:
        return True

    # ユーザー発言が独立した調査要求や固有名詞・質問を含む自立文である場合は、
    # 指示代名詞や明示的な対象（has_target）がない限り文脈URLの参照とみなさない。
    action_keywords = set(_CONTEXT_URL_ACTION_KEYWORDS) | set(_CONTEXT_URL_STRONG_ACTION_KEYWORDS)
    topic_candidates = [
        t for t in extract_named_term_candidates(normalized)
        if t not in action_keywords
    ]
    has_explicit_topic = (
        len(normalized) > 20
        or bool(extract_explicit_research_term(normalized))
        or bool(topic_candidates)
    )
    if has_strong_action and not is_conversational and not has_explicit_topic:
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

_CHARACTER_DIALOGUE_QUOTE_RE = re.compile(
    r"([ァ-ヶー一-龥A-Za-z0-9_]{2,16})\s*[「『]([^」』]{2,120})[」』]"
)


def _has_character_dialogue_meme(text: str) -> tuple[bool, list[str]]:
    """発言内に キャラ名「台詞」のパターンが含まれ、かつスピーカー名または鉤括弧内に

    固有名詞候補（特にカタカナ語や未知語）が含まれているかを判定する。
    """
    normalized = _compact_whitespace(_strip_urls(text))
    if not normalized:
        return False, []

    found_terms: list[str] = []
    has_meme = False
    generic_stopwords_lower = {s.lower() for s in get_generic_term_stopwords()}

    for m in _CHARACTER_DIALOGUE_QUOTE_RE.finditer(normalized):
        raw_speaker = m.group(1).strip()
        speaker = _normalize_candidate_term(raw_speaker)
        quote = m.group(2).strip()

        # スピーカー名の検証
        speaker_valid = (
            len(speaker) >= 2
            and speaker.lower() not in generic_stopwords_lower
            and speaker not in _COMMON_KATAKANA_STOPWORDS
        )
        if speaker_valid and speaker not in found_terms:
            found_terms.append(speaker)

        # 鉤括弧内の台詞から固有名詞候補を抽出
        quote_terms = extract_named_term_candidates(quote)
        for qt in quote_terms:
            qt_norm = _normalize_candidate_term(qt)
            if (
                len(qt_norm) >= 2
                and qt_norm.lower() not in generic_stopwords_lower
                and qt_norm not in _COMMON_KATAKANA_STOPWORDS
                and qt_norm not in found_terms
            ):
                found_terms.append(qt_norm)

        # スピーカー名または台詞内の候補語にカタカナ語、英数語、または未知語が含まれているか確認
        has_specialized_or_katakana = any(
            bool(_KATAKANA_TERM_RE.search(t))
            or _looks_like_specialized_unknown_term(t)
            or bool(_ASCII_TERM_RE.search(t))
            for t in found_terms
        )
        if found_terms and has_specialized_or_katakana:
            has_meme = True

    return has_meme, found_terms


def _build_context_enriched_query(
    user_text: str,
    context: str,
    named_terms: list[str],
    context_named_terms: list[str],
    *,
    has_dialogue_meme: bool = False,
    dialogue_terms: list[str] | None = None,
) -> str:
    """最新発言の固有名詞と直近文脈の共起固有名詞（Context Co-occurrence）を

    結合した検索クエリを生成する。
    """
    base_terms: list[str] = []
    seen_lower: set[str] = set()

    for t in (dialogue_terms or []) + named_terms:
        if not t:
            continue
        key = t.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            base_terms.append(t)

    if not base_terms:
        return _choose_query(user_text, context)

    ctx_terms_to_add: list[str] = []
    for ct in context_named_terms:
        if not ct:
            continue
        key = ct.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            ctx_terms_to_add.append(ct)
            if len(ctx_terms_to_add) >= 2:
                break

    terms_to_join = base_terms[:3] + ctx_terms_to_add
    if terms_to_join:
        return " ".join(terms_to_join)

    return _choose_query(user_text, context)


def _choose_query(user_text: str, context: str) -> str:
    from lib.discord_utils import strip_discord_mentions
    clean_user = strip_discord_mentions(user_text)
    user_candidate = _compact_whitespace(_strip_urls(clean_user))
    if user_candidate:
        return user_candidate

    clean_context = strip_discord_mentions(context)
    context_candidate = _compact_whitespace(_strip_urls(clean_context))
    if context_candidate:
        return truncate_text(context_candidate, 180)

    urls = extract_urls(f"{user_text}\n{context}")
    if urls:
        return urls[0]
    return _compact_whitespace(clean_user or clean_context or user_text or context)



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


_CONTEXT_LINE_SPEAKER_PREFIX_RE = re.compile(
    r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?"
    r"[^:：\n]{1,48}?"
    r"(?:\s*(?:\[(?:独立発言|>>[^\]]+)\]|（>>[^）]+）|\(>>[^)]+\)))?"
    r"\s*[:：]\s*"
)
_CONTEXT_REPLY_BRACKET_RE = re.compile(
    r"\[>>[^\]]+\]|（(?:あなたの「[^」]*」|[^）]+?)への返信）|\((?:>>[^)]+|[^)]+?への返信)\)"
)
_SENRYU_YOMITE_LINE_RE = re.compile(r"^\s*詠み手\s*[:：].*$", re.MULTILINE)
_QUESTION_PATTERNS: re.Pattern[str] = re.compile(
    r"[?？]|(?:って(?:何|なに)|とは(?:何|なに)?|教えて|知ってる|ですか|ますか|でしょうか)"
)


def _strip_context_speaker_and_meta_labels(context_text: str) -> str:
    """会話履歴テキストから『[HH:MM] 話者名 [>>返信先 への返信]:』のプレフィックスや『詠み手:』行を除去し、本文のみを返す。"""
    if not context_text:
        return ""
    cleaned_lines: list[str] = []
    for raw_line in str(context_text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("System:", "system:")) or "内心のメモ:" in line:
            continue
        if _SENRYU_YOMITE_LINE_RE.match(line):
            continue
        # 先頭の [HH:MM] 話者名 [>>...]: を除去
        body = _CONTEXT_LINE_SPEAKER_PREFIX_RE.sub("", line)
        # 文中に残った [>>... への返信] 等のブラケットも除去
        body = _CONTEXT_REPLY_BRACKET_RE.sub(" ", body).strip()
        if body:
            cleaned_lines.append(body)
    return "\n".join(cleaned_lines)


def detect_reply_research_rules(
    user_text: str,
    context: str = "",
    *,
    xcom: Any = None,
    img_helper: Any = None,
    mode: str = "auto",
    is_fact_check: bool | None = None,
) -> dict[str, Any]:
    if xcom is None or img_helper is None:
        _ensure_helpers()
    if xcom is None:
        xcom = _xcom
    if img_helper is None:
        img_helper = _img_helper
    from lib.discord_utils import strip_discord_mentions
    clean_user = strip_discord_mentions(user_text)
    normalized_user_text = str(clean_user if clean_user else (user_text or "")).strip()
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

    context_named_terms: list[str] = []
    if normalized_context:
        _speaker_re = re.compile(
            r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?([^\[\(:：\n]+?)\s*(?:\[[^\]]*\]|\([^)]*\))?\s*[:：]",
            re.M,
        )
        context_speakers = {m.group(1).strip() for m in _speaker_re.finditer(normalized_context) if m.group(1).strip()}
        # 本文中の相手呼びかけ（例: 「マーモンさん」等）からも話者・相手呼称を抽出して除外
        _honorific_re = re.compile(r"([A-Za-z0-9ァ-ヶー一-龥]{2,})(?:さん|くん|君|ちゃん|様|氏)")
        for m in _honorific_re.finditer(normalized_context):
            h_name = m.group(1).strip()
            if h_name:
                context_speakers.add(h_name)

        context_bodies_only = _strip_context_speaker_and_meta_labels(normalized_context)
        raw_context_terms = extract_named_term_candidates(context_bodies_only)
        user_terms_lower = {ut.lower() for ut in named_terms}
        _role_stop = {"assistant", "user", "system", "bot"}
        for ct in raw_context_terms:
            ct_clean = ct.strip()
            if (
                not ct_clean
                or ct_clean.lower() in _role_stop
                or any(ct_clean == sp or sp.startswith(ct_clean) or ct_clean in sp for sp in context_speakers)
            ):
                continue
            if ct_clean.lower() not in user_terms_lower and ct_clean not in context_named_terms:
                context_named_terms.append(ct_clean)
        context_named_terms = context_named_terms[:3]

    conversational_followup = _looks_like_conversational_preference_followup(
        normalized_user_text,
        normalized_context,
    )

    user_urls = extract_urls(normalized_user_text)
    
    # 内部システムメモ（System: や 内心のメモ:）に記載されたURLはユーザーが指示したものではないため除外
    clean_context_lines = [
        line for line in normalized_context.splitlines()
        if not (line.strip().startswith("System:") or "内心のメモ:" in line or line.strip().startswith("system:"))
    ]
    clean_context_text = "\n".join(clean_context_lines)
    clean_combined = "\n".join(part for part in (normalized_user_text, clean_context_text) if part).strip()
    context_urls = extract_urls(clean_context_text)

    context_url_reference = (
        bool(context_urls)
        and not user_urls
        and _looks_like_context_url_reference(normalized_user_text, is_conversational=conversational_followup)
    )
    urls = user_urls or (context_urls if context_url_reference else [])
    url_present = bool(urls)
    x_timeline_flag = bool(xcom.is_x_timeline_request(normalized_user_text))
    x_search_flag = bool(hasattr(xcom, "is_x_search_request") and xcom.is_x_search_request(normalized_user_text))
    img_top5_flag = bool(img_helper.is_img_top_request(normalized_user_text))
    user_img_thread = bool(hasattr(img_helper, "is_img_thread_url") and img_helper.is_img_thread_url(normalized_user_text))
    context_img_thread = bool(context_url_reference and hasattr(img_helper, "is_img_thread_url") and img_helper.is_img_thread_url(clean_combined))
    img_thread_flag = user_img_thread or context_img_thread
    source_request = _contains_any_keyword(normalized_user_text, _SOURCE_REQUEST_KEYWORDS)
    simulation_impact_flag = looks_like_simulation_or_impact_verification(normalized_user_text, normalized_context)
    image_search_req = looks_like_image_search_request(normalized_user_text)
    image_context_present = has_image_context(normalized_user_text, normalized_context)
    image_search_flag = bool(image_search_req and image_context_present)
    research_request = (
        source_request
        or _contains_any_keyword(normalized_user_text, _RESEARCH_REQUEST_KEYWORDS)
        or simulation_impact_flag
        or image_search_flag
    )
    recency_flag = (
        x_timeline_flag
        or x_search_flag
        or _contains_any_keyword(normalized_user_text, _RECENCY_KEYWORDS)
        or bool(_YEAR_OR_DATE_RE.search(normalized_user_text))
    )
    # 「まだ〜してないよ」等の疑問符を含まない個人の過去・未消化報告で、他の時事語（最新・今日・現在・ニュース等）がない場合は recency_flag を立てない
    if recency_flag and "まだ" in normalized_user_text:
        other_recency_kws = tuple(kw for kw in _RECENCY_KEYWORDS if kw not in ("まだ", "配布"))
        has_other_recency = any(kw in normalized_user_text for kw in other_recency_kws)
        has_question = "?" in normalized_user_text or "？" in normalized_user_text or bool(_QUESTION_PATTERNS.search(normalized_user_text))
        if not has_other_recency and not has_question:
            recency_flag = False
    news_flag = _contains_any_keyword(normalized_user_text, _NEWS_KEYWORDS)
    product_spec_flag = _contains_any_keyword(normalized_user_text, _PRODUCT_SPEC_KEYWORDS)
    
    teaching_statement = looks_like_user_teaching_or_definition(normalized_user_text)
    explanation_request = False if teaching_statement else (_contains_any_keyword(normalized_user_text, _EXPLANATION_KEYWORDS) or bool(explicit_term))
    
    known_term_cached = bool(candidate_term) and _is_known_term_cached(candidate_term)
    is_term_definition_request = (
        bool(explicit_term)
        or any(marker in normalized_user_text for marker in ("って何", "ってなに", "とは何", "とはなに", "何それ", "なにそれ", "何者", "誰", "だれ", "の意味"))
        or (explanation_request and bool(candidate_term) and len(normalized_user_text) <= len(candidate_term) + 6)
    )
    unknown_term_flag = (
        bool(candidate_term)
        and is_term_definition_request
        and not conversational_followup
        and not known_term_cached
        and _looks_like_specialized_unknown_term(candidate_term)
    )
    question_like = any(marker in normalized_user_text for marker in ("?", "？", "って", "とは", "教えて", "知ってる"))

    topic_inquiry = detect_topic_opinion_inquiry(normalized_user_text)
    topic_opinion_flag = bool(topic_inquiry.get("is_opinion_inquiry"))

    consultation_inquiry = detect_consultation_or_problem_solving_inquiry(
        normalized_user_text,
        normalized_context,
    )
    consultation_flag = bool(consultation_inquiry.get("is_consultation_inquiry"))

    matched_fc_pattern = ""
    if is_fact_check is True:
        matched_fc_pattern = "explicit_is_fact_check"
    elif is_fact_check is False:
        matched_fc_pattern = ""
    elif str(mode or "").strip().lower() in {"fact_check", "factcheck", "deep", "deep_research"}:
        matched_fc_pattern = f"mode:{mode}"
    else:
        matched_fc_pattern = get_matched_fact_check_pattern(normalized_user_text, normalized_context)

    fact_check_flag = bool(matched_fc_pattern)

    mode_res: ResearchDispatchMode = "simple_search"
    if image_search_flag:
        mode_res = "image_search"
    elif img_thread_flag:
        mode_res = "img_thread"
    elif img_top5_flag:
        mode_res = "img_top5"
    elif x_timeline_flag:
        mode_res = "x_timeline"
    elif url_present:
        mode_res = "browser_read_url"
    elif x_search_flag:
        mode_res = "x_search"
    elif fact_check_flag or simulation_impact_flag:
        mode_res = "browser_search" if cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True) else "simple_search"
    elif source_request or (research_request and (recency_flag or product_spec_flag or news_flag)):
        mode_res = "x_search" if (recency_flag and _web_research_recency_prefer_x_search()) else "browser_search"
    elif topic_opinion_flag or consultation_flag:
        mode_res = "browser_search" if cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True) else "simple_search"

    # カタカナ語・未知語候補（スラング・倒語・造語含む）の存在チェック
    math_or_scientific = looks_like_scientific_notation_or_math(normalized_user_text)
    puzzle_or_riddle = looks_like_puzzle_or_riddle(normalized_user_text)
    daily_action_monologue = looks_like_daily_action_or_monologue(normalized_user_text)
    explicit_search_or_url = bool(
        url_present
        or research_request
        or source_request
        or img_thread_flag
        or img_top5_flag
        or x_timeline_flag
        or x_search_flag
    )
    puzzle_or_riddle_suppressed = bool(puzzle_or_riddle and not explicit_search_or_url)
    daily_action_suppressed = bool(daily_action_monologue and not explicit_search_or_url)

    raw_term_candidates = list(dict.fromkeys(([candidate_term] if candidate_term else []) + named_terms))
    meaningful_unknown_candidates: list[str] = []
    generic_stopwords_lower = {s.lower() for s in get_generic_term_stopwords()}
    for t in raw_term_candidates:
        if not t:
            continue
        t_norm = _normalize_candidate_term(t)
        if (
            len(t_norm) >= 2
            and t_norm.lower() not in generic_stopwords_lower
            and t_norm not in _COMMON_KATAKANA_STOPWORDS
            and not _is_known_term_cached(t_norm)
        ):
            meaningful_unknown_candidates.append(t_norm)

    has_slang_or_unknown_candidate = (
        bool(meaningful_unknown_candidates)
        and not _is_greeting_or_pure_chitchat(normalized_user_text)
        and not daily_action_suppressed
    )

    slang_or_unknown_trigger = bool(
        has_slang_or_unknown_candidate
        and not teaching_statement
        and not math_or_scientific
        and not puzzle_or_riddle_suppressed
        and not daily_action_suppressed
        and not conversational_followup
        and not known_term_cached
    )

    # キャラ台詞・引用ネタおよび文脈共起固有名詞トリガーの検出
    has_dialogue_meme, dialogue_terms = _has_character_dialogue_meme(normalized_user_text)
    meaningful_unknown_katakana = list(dict.fromkeys(
        t for t in meaningful_unknown_candidates if _KATAKANA_TERM_RE.search(t)
    ))

    character_or_context_entity_trigger = bool(
        (
            has_dialogue_meme
            or (len(meaningful_unknown_katakana) >= 2 and bool(context_named_terms))
        )
        and not _is_greeting_or_pure_chitchat(normalized_user_text)
        and not teaching_statement
        and not math_or_scientific
        and not puzzle_or_riddle_suppressed
        and not daily_action_suppressed
    )

    if character_or_context_entity_trigger:
        slang_or_unknown_trigger = True

    rule_reasons: list[str] = []
    if image_search_flag:
        rule_reasons.append("image_search_request")
    if img_thread_flag:
        rule_reasons.append("img_thread")
    if img_top5_flag:
        rule_reasons.append("img_top5")
    if x_timeline_flag:
        rule_reasons.append("x_timeline")
    if x_search_flag:
        rule_reasons.append("x_search")
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
    if topic_opinion_flag:
        rule_reasons.append("topic_opinion_inquiry")
    if consultation_flag:
        rule_reasons.append("consultation_problem_solving")
    if fact_check_flag:
        rule_reasons.append("fact_check")
    if simulation_impact_flag:
        rule_reasons.append("simulation_impact_verification")
    if slang_or_unknown_trigger:
        rule_reasons.append("slang_or_unknown_candidate")
    if character_or_context_entity_trigger:
        rule_reasons.append("Character_Dialogue_Or_Context_Entity_Research")

    soft_score = sum(
        1
        for flag in (
            recency_flag,
            news_flag,
            product_spec_flag,
            simulation_impact_flag,
            explanation_request and bool(candidate_term) and not conversational_followup,
        )
        if flag
    )
    hard_trigger = (
        image_search_flag
        or img_thread_flag
        or img_top5_flag
        or x_timeline_flag
        or x_search_flag
        or url_present
        or source_request
        or research_request
        or topic_opinion_flag
        or consultation_flag
        or fact_check_flag
        or simulation_impact_flag
    )
    needs_research = (
        hard_trigger
        or character_or_context_entity_trigger
        or (not conversational_followup and (unknown_term_flag or soft_score >= 2))
    )
    if teaching_statement and not (image_search_flag or url_present or img_thread_flag or img_top5_flag or x_timeline_flag or x_search_flag or fact_check_flag or simulation_impact_flag):
        needs_research = False

    if math_or_scientific and not (image_search_flag or url_present or img_thread_flag or img_top5_flag or x_timeline_flag or x_search_flag or fact_check_flag or simulation_impact_flag):
        needs_research = False

    if puzzle_or_riddle_suppressed:
        needs_research = False

    if daily_action_suppressed:
        needs_research = False

    should_consult_model = bool(
        not needs_research
        and not teaching_statement
        and not math_or_scientific
        and not puzzle_or_riddle_suppressed
        and not daily_action_suppressed
        and (
            unknown_term_flag
            or slang_or_unknown_trigger
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

    enriched_query = ""
    if (
        character_or_context_entity_trigger
        or (
            (has_dialogue_meme or (1 <= len(named_terms) <= 3 and bool(meaningful_unknown_candidates)))
            and bool(context_named_terms)
        )
    ):
        enriched_query = _build_context_enriched_query(
            normalized_user_text,
            normalized_context,
            named_terms=named_terms,
            context_named_terms=context_named_terms,
            has_dialogue_meme=has_dialogue_meme,
            dialogue_terms=dialogue_terms,
        )

    is_howto_question = any(marker in normalized_user_text for marker in ("方法", "やり方", "やりかた", "仕方", "しかた", "使い方", "つかいかた", "ダウンロード", "保存"))
    if image_search_flag:
        from ..adapters.image_search import extract_image_url_from_context

        ctx_img_url = extract_image_url_from_context(
            {"user_text": normalized_user_text, "context_text": normalized_context}
        )
        query = ctx_img_url or _choose_query(normalized_user_text, normalized_context)
    elif img_thread_flag:
        full_urls = (
            getattr(img_helper, "_extract_img_thread_urls_full", lambda t: [])(clean_combined)
            if hasattr(img_helper, "_extract_img_thread_urls_full")
            else [u for u in urls if ("img.2chan.net" in u or "may.2chan.net" in u)]
        )
        query = full_urls[0] if full_urls else (urls[0] if urls else "")
    elif img_top5_flag:
        query = "https://img.2chan.net/b/futaba.php?mode=cat&sort=6"
    elif x_timeline_flag:
        query = str(getattr(xcom, "X_TIMELINE_HOME_URL", "https://x.com/home") or "https://x.com/home")
    elif url_present:
        query = urls[0] if urls else ""
    elif x_search_flag or mode_res == "x_search":
        query = _choose_query(normalized_user_text, normalized_context)
    elif topic_opinion_flag and topic_inquiry.get("search_query"):
        query = str(topic_inquiry.get("search_query") or "").strip()
    elif consultation_flag and consultation_inquiry.get("search_query"):
        query = str(consultation_inquiry.get("search_query") or "").strip()
    elif enriched_query and (character_or_context_entity_trigger or slang_or_unknown_trigger):
        query = enriched_query
    elif recency_flag or news_flag or product_spec_flag or research_request or is_howto_question or fact_check_flag:
        query = _choose_query(normalized_user_text, normalized_context)
    elif explicit_term and (unknown_term_flag or len(normalized_user_text) <= len(explicit_term) + 10):
        query = explicit_term
    else:
        query = _choose_query(normalized_user_text, normalized_context)

    log.info(
        "[research-rules] Rule matching details: reasons=%r, candidate_term=%r, named_terms=%r, context_named_terms=%r, has_dialogue_meme=%s, matched_fc_pattern=%r, daily_action_monologue=%s",
        rule_reasons,
        candidate_term,
        named_terms,
        context_named_terms,
        has_dialogue_meme,
        matched_fc_pattern,
        daily_action_monologue,
    )

    return {
        "needs_research": bool(needs_research),
        "mode": mode_res,
        "query": query,
        "image_search_flag": bool(image_search_flag),
        "candidate_term": candidate_term,
        "named_terms": named_terms,
        "context_named_terms": context_named_terms,
        "has_character_dialogue_quote": bool(has_dialogue_meme),
        "topic_opinion_flag": topic_opinion_flag,
        "consultation_flag": consultation_flag,
        "consultation_inquiry": consultation_inquiry,
        "fact_check_flag": bool(fact_check_flag),
        "matched_fc_pattern": matched_fc_pattern,
        "simulation_impact_flag": bool(simulation_impact_flag),
        "target_subject": str(topic_inquiry.get("target_subject") or ""),
        "target_aspect": str(topic_inquiry.get("target_aspect") or ""),
        "topic_inquiry": topic_inquiry,
        "img_thread_flag": img_thread_flag,
        "x_timeline_flag": x_timeline_flag,
        "x_search_flag": x_search_flag,
        "url_present": url_present,
        "context_url_reference": context_url_reference,
        "source_request": source_request,
        "research_request": research_request,
        "recency_flag": recency_flag,
        "news_flag": news_flag,
        "product_spec_flag": product_spec_flag,
        "explanation_request": explanation_request,
        "unknown_term_flag": unknown_term_flag,
        "slang_or_unknown_trigger": slang_or_unknown_trigger,
        "puzzle_or_riddle": puzzle_or_riddle,
        "daily_action_monologue": daily_action_monologue,
        "conversational_followup": conversational_followup,
        "known_term_cached": known_term_cached,
        "question_like": question_like,
        "rule_reasons": rule_reasons,
        "soft_score": soft_score,
        "hard_trigger": hard_trigger,
        "should_consult_model": should_consult_model,
        "stripped_text": plain_text,
    }


def resolve_research_mode(
    text: str,
    context: str = "",
    mode: str = "auto",
    *,
    is_fact_check: bool | None = None,
) -> ResearchDispatchMode:
    """テキストからリサーチモードを解決する。"""
    requested = str(mode or "auto").strip().lower()
    if requested in {"simple_search", "browser_search", "browser_read_url", "x_timeline", "x_search", "img_top5", "img_thread", "image_search"}:
        return requested  # type: ignore[return-value]
    if is_fact_check is True or requested in {"fact_check", "factcheck", "deep", "deep_research"}:
        if cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
            return "browser_search"
        return "simple_search"

    rules = detect_reply_research_rules(text, context=context, mode=requested, is_fact_check=is_fact_check)
    resolved_mode = str(rules.get("mode") or "auto")

    # 入力テキスト自体にURLが含まれている場合、コメントが付随していても browser_read_url を維持し安易に browser_search へ降格させない
    if bool(extract_urls(text)) and resolved_mode not in {"x_timeline", "x_search", "img_thread", "img_top5", "image_search"}:
        return "browser_read_url"

    if resolved_mode == "browser_read_url":
        # テキスト自体にURLがない（文脈参照）かつ「URL単体」でない場合は browser_search へ
        if not looks_like_url_only_text(text):
            return "browser_search"
        return "browser_read_url"

    return resolved_mode  # type: ignore[return-value]


def classify_research_need(
    user_text: str,
    context: str = "",
    *,
    xcom: Any = None,
    img_helper: Any = None,
) -> dict[str, Any]:
    """detect_reply_research_rules のエイリアス。リサーチ要否およびモードを判定する。"""
    return detect_reply_research_rules(user_text, context=context, xcom=xcom, img_helper=img_helper)
