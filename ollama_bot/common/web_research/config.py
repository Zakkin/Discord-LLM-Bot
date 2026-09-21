"""
web_research/config.py

このファイルはウェブリサーチ機能全体で使用される設定値・定数・設定取得関数を管理します。
- デフォルト値・上限値などの定数定義
- キーワードリスト（調査要求・時事性・製品仕様など）
- 正規表現パターン
- 設定値を環境変数から読み込むゲッター関数

他のモジュールはこのファイルから設定をインポートして使用します。
AIエージェントが新しい設定を追加する場合はここに追記してください。
"""

from __future__ import annotations

import re
from typing import Any

from ..config_helpers import cfg, cfg_bool, cfg_float, cfg_int


# ---------------------------------------------------------------------------
# Research dispatch mode type alias (str literal union)
# ---------------------------------------------------------------------------

from typing import Literal

ResearchDispatchMode = Literal[
    "auto",
    "simple_search",
    "browser_search",
    "browser_read_url",
    "x_timeline",
    "img_top5",
    "img_thread",
]

# ---------------------------------------------------------------------------
# Protocol / error constants
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2025-03-26"
_NO_RESULTS_TEXT = "検索結果が見つかりませんでした。"

# ---------------------------------------------------------------------------
# X.com / login-wall fallback constants
# ---------------------------------------------------------------------------

_FALLBACK_XCOM_DYNAMIC_BROWSER_HOSTS: tuple[str, ...] = ("x.com", "twitter.com")
_FALLBACK_XCOM_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don\u2019t miss what\u2019s happening",
    "people on x are the first to know",
    "something went wrong",
    "please log in",
    "please sign in",
    "sign in to",
    "log in to",
    "you need to sign in",
    "login required",
    "access denied",
    "403 forbidden",
    "page not found",
    "404 not found",
    "this content isn't available",
    "this tweet is from an account that no longer exists",
    "something went wrong, but don't fret",
    "something went wrong, but don\u2019t fret",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_STRONG_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don\u2019t miss what\u2019s happening",
    "people on x are the first to know",
    "some privacy related extensions may cause issues on x.com",
)
_FALLBACK_XCOM_LOGIN_WALL_MAX_CONTENT_LEN = 600

# ---------------------------------------------------------------------------
# Keyword lists for research detection
# ---------------------------------------------------------------------------

_RESEARCH_REQUEST_KEYWORDS = (
    "調べて",
    "調べろ",
    "調査して",
    "調査しろ",
    "検索して",
    "検索しろ",
    "検索",
    "ググって",
    "ググれ",
    "確認して",
    "確認しろ",
    "見てきて",
    "見てきてほしい",
)
_SOURCE_REQUEST_KEYWORDS = (
    "ソース",
    "出典",
    "根拠",
    "情報源",
    "参照",
    "source",
)
_RECENCY_KEYWORDS = (
    "今",
    "いま",
    "現在",
    "最新",
    "最近",
    "今日",
    "きょう",
    "昨日",
    "明日",
    "今年",
    "いまどう",
    "今どう",
    "アップデート",
    "更新",
)
_NEWS_KEYWORDS = (
    "ニュース",
    "報道",
    "速報",
    "話題",
)
_PRODUCT_SPEC_KEYWORDS = (
    "仕様",
    "スペック",
    "spec",
    "価格",
    "値段",
    "料金",
    "発売日",
    "対応",
    "互換",
    "ベンチ",
    "性能",
    "モデル",
)
_EXPLANATION_KEYWORDS = (
    "って何",
    "ってなに",
    "とは",
    "とは何",
    "とはなに",
    "何それ",
    "なにそれ",
    "何者",
    "誰",
    "だれ",
    "意味",
    "詳しく",
    "教えて",
    "教えろ",
    "説明して",
    "説明しろ",
    "何があった",
    "何があったのか",
    "何が起きた",
    "どういうこと",
    "何の話",
    "どういう話",
    "kwsk",
    "詳細教えて",
)
_CONVERSATIONAL_FOLLOWUP_MARKERS = (
    "強いて",
    "しいて",
    "あえて",
    "じゃあ",
    "なら",
    "だったら",
    "で言う",
    "でいう",
    "誰？",
    "誰?",
    "だれ？",
    "だれ?",
    "どれ",
    "どっち",
)
_PREFERENCE_OR_OPINION_MARKERS = (
    "好き",
    "推し",
    "お気に入り",
    "好み",
    "興味",
    "気になる",
    "おすすめ",
    "オススメ",
)
_CONTEXT_URL_TARGET_KEYWORDS = (
    "url",
    "リンク",
    "記事",
    "ページ",
    "投稿",
    "ポスト",
    "ツイート",
    "スレ",
    "スレッド",
    "先",
    "中身",
    "内容",
    "本文",
    "これ",
    "それ",
    "あれ",
)
_CONTEXT_URL_ACTION_KEYWORDS = (
    "見て",
    "読んで",
    "開いて",
    "確認",
    "調べ",
    "検索",
    "要約",
    "解説",
    "教えて",
    "まとめ",
    "本当",
    "ほんと",
    "真偽",
    "根拠",
    "ソース",
    "何",
    "なに",
    "どういう",
    "どう思う",
    "詳しく",
)
_CONTEXT_URL_STRONG_ACTION_KEYWORDS = (
    "見て",
    "読んで",
    "開いて",
    "確認",
    "調べ",
    "検索",
    "要約",
    "解説",
    "まとめ",
)
_GENERIC_TERM_STOPWORDS = {
    "これ",
    "それ",
    "あれ",
    "今日",
    "昨日",
    "明日",
    "意味",
    "詳細",
    "仕様",
    "スペック",
    "ニュース",
    "ソース",
    "出典",
    "情報",
    "こと",
    "やつ",
    "もの",
    "なん",
    "なに",
    "何",
    "誰",
    "だれ",
    "教えて",
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_YEAR_OR_DATE_RE = re.compile(
    r"(?:19|20)\d{2}年?|(?:19|20)\d{2}|[01]?\d/[0-3]?\d|[01]?\d月[0-3]?\d日|[0-3]?\d日|[01]?\d:[0-5]\d"
)
_ASCII_TERM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+._/-]{2,}\b")
_KATAKANA_TERM_RE = re.compile(r"[ァ-ヶー]{3,}")
_KANJI_TERM_RE = re.compile(r"[一-龥]{2,12}")
_QUERY_TERM_RE = re.compile(r"[A-Za-z0-9+._/-]{2,}|[ァ-ヶー]{2,}|[一-龥]{2,}")
_BROWSER_BOILERPLATE_PATTERNS = (
    "cookie",
    "クッキー",
    "プライバシー",
    "利用規約",
    "ログイン",
    "会員登録",
    "メニュー",
    "ナビゲーション",
    "広告",
    "スポンサー",
    "共有",
    "シェア",
    "フォロー",
)

# ---------------------------------------------------------------------------
# Deep-dive followup detection constants
# ---------------------------------------------------------------------------

# img_top5 結果への深掘りをトリガーするキーワード
_IMG_DEEPDIVE_FOLLOWUP_KEYWORDS: tuple[str, ...] = (
    "その中で", "その中の", "その中から", "その中",
    "もっと詳しく", "詳しく教えて", "詳しく",
    "一番上", "一番目", "一番", "1番目", "1番", "1位",
    "2番目", "2番", "二番目", "二番", "2位",
    "3番目", "3番", "三番目", "三番", "3位",
    "4番目", "4番", "四番目", "四番", "4位",
    "5番目", "5番", "五番目", "五番", "5位",
    "読んで", "見て", "スレ読んで", "内容教えて", "スレの中身",
    "面白そうなの", "面白いの", "面白そうな", "気になるの",
    "教えろ", "教えてくれ", "見せて", "見せろ",
    "何があった", "何があったのか", "何が起きた", "どういうこと", "何の話", "どういう話", "kwsk",
    "気になる", "気に入った", "それ",
)

# x_timeline 結果への深掘りをトリガーするキーワード
_X_DEEPDIVE_FOLLOWUP_KEYWORDS: tuple[str, ...] = (
    "その中で", "その中の", "その中から", "それについて",
    "もっと詳しく", "詳しく",
    "どれが", "何が", "一番", "気になる", "面白い", "面白そう",
    "教えろ", "教えて", "見せて",
    "何があった", "何があったのか", "何が起きた", "どういうこと", "何の話", "どういう話", "kwsk",
)

# 深掘りキャッシュの有効期限（秒）
_DEEPDIVE_CACHE_TTL_SEC: float = 180.0

# 序数→インデックスのマッピング
_ORDINAL_TO_INDEX: dict[str, int] = {
    "一番目": 0, "一番": 0, "1番目": 0, "1番": 0, "1位": 0, "上から1": 0,
    "二番目": 1, "二番": 1, "2番目": 1, "2番": 1, "2位": 1,
    "三番目": 2, "三番": 2, "3番目": 2, "3番": 2, "3位": 2,
    "四番目": 3, "四番": 3, "4番目": 3, "4番": 3, "4位": 3,
    "五番目": 4, "五番": 4, "5番目": 4, "5番": 4, "5位": 4,
    "一番上": 0,
}

# ---------------------------------------------------------------------------
# LLM schemas and prompts
# ---------------------------------------------------------------------------

_BROWSER_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "open_top_n": {"type": "integer"},
        "recency_priority": {"type": "boolean"},
    },
    "required": ["query", "open_top_n", "recency_priority"],
    "additionalProperties": False,
}
_BROWSER_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "insufficient": {"type": "boolean"},
        "insufficiency_reason": {"type": "string"},
    },
    "required": ["summary", "confidence", "insufficient", "insufficiency_reason"],
    "additionalProperties": False,
}
_BROWSER_QUERY_SYSTEM_PROMPT = (
    "あなたはWeb調査用の検索語生成器です。"
    "ユーザーの質問文と補助情報から、検索エンジンに投げる短い検索語だけを作ってください。"
    "ニュースや最新情報なら時点性を意識した語を残し、固有名詞説明なら対象名を優先してください。"
    "出力は必ずJSONだけにしてください。"
)
_BROWSER_SUMMARY_SYSTEM_PROMPT = (
    "あなたはWeb調査の観測メモ要約器です。"
    "複数ページから抜いた本文と検索抜粋を見て、後段の会話モデルが使いやすい研究メモを作ってください。"
    "事実の断定は観測できた範囲に限り、不足がある場合は不足理由も明示してください。"
    "出力は必ずJSONだけにしてください。"
)

# ---------------------------------------------------------------------------
# MCP tool allow/block lists
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_MCP_TOOLS = (
    "chrome_navigate",
    "chrome_get_web_content",
    "chrome_read_page",
    "chrome_close_tabs",
    "chrome_get_page_metadata",
    "chrome_get_page_title",
    "chrome_screenshot",
    "chrome_capture_screenshot",
    "chrome_take_screenshot",
)
_DEFAULT_BLOCKED_MCP_TOOL_KEYWORDS = (
    "click",
    "press",
    "type",
    "fill",
    "submit",
    "download",
    "upload",
    "delete",
    "remove",
    "purchase",
    "buy",
    "checkout",
    "cart",
    "payment",
)
_DEFAULT_ALLOWED_URL_SCHEMES = {"http", "https"}

# ---------------------------------------------------------------------------
# Numeric defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RESULTS_HARD_LIMIT = 6
_DEFAULT_BROWSER_PAGES_HARD_LIMIT = 5
_DEFAULT_BROWSER_CONTENT_CHARS_HARD_LIMIT = 2400
_DEFAULT_BROWSER_LOCK_PATH = "/tmp/discord-ai-bot-browser.lock"
_DEFAULT_MCP_SESSION_PATH = "/tmp/discord-ai-bot-chrome-mcp-session"
_DEFAULT_KNOWN_TERM_CACHE_TTL_SEC = 12 * 60 * 60
_DEFAULT_KNOWN_TERM_CACHE_MAX_SIZE = 256
_DEFAULT_SEARCH_URL_TEMPLATE = "https://duckduckgo.com/html/?q={query}&kl=jp-jp"
_DEFAULT_BROWSER_POST_NAVIGATION_DELAY_SEC = 0.75
_DEFAULT_BROWSER_DYNAMIC_SITE_DELAY_SEC = 3.0
_DEFAULT_BROWSER_READ_RETRIES = 3
_DEFAULT_BROWSER_READ_RETRY_DELAY_SEC = 1.0
_DEFAULT_X_TIMELINE_LIMIT = 10  # overridden after xcom helper load
_DEFAULT_X_TIMELINE_HARD_LIMIT = 20
_DEFAULT_X_TIMELINE_EXTRA_WAIT_SEC = 3.0

# NOTE: _DEFAULT_DYNAMIC_BROWSER_HOSTS is set in __init__.py after loading the xcom helper,
# because it depends on the helper's DEFAULT_DYNAMIC_BROWSER_HOSTS attribute.


# ---------------------------------------------------------------------------
# Config getter functions
# ---------------------------------------------------------------------------

def _cfg_csv_values(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = cfg(name, default)
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = [str(item or "").strip() for item in default]
    return tuple(value for value in values if value)


def _browser_allowed_tool_names() -> set[str]:
    return {
        str(name or "").strip()
        for name in _cfg_csv_values("WEB_RESEARCH_MCP_ALLOWED_TOOLS", _DEFAULT_ALLOWED_MCP_TOOLS)
        if str(name or "").strip()
    }


def _browser_blocked_tool_keywords() -> tuple[str, ...]:
    return tuple(
        keyword.lower()
        for keyword in _cfg_csv_values(
            "WEB_RESEARCH_MCP_BLOCKED_TOOL_KEYWORDS",
            _DEFAULT_BLOCKED_MCP_TOOL_KEYWORDS,
        )
        if str(keyword or "").strip()
    )


def _browser_debug_enabled() -> bool:
    return cfg_bool("WEB_RESEARCH_BROWSER_DEBUG", False)


def _browser_debug_preview_chars() -> int:
    return max(cfg_int("WEB_RESEARCH_BROWSER_DEBUG_PREVIEW_CHARS", 0), 0)


def _safe_result_limit(requested: int) -> int:
    hard_limit = max(cfg_int("WEB_RESEARCH_MAX_RESULTS_HARD_LIMIT", _DEFAULT_MAX_RESULTS_HARD_LIMIT), 1)
    return max(1, min(int(requested or 1), hard_limit))


def _safe_browser_page_limit(requested: int) -> int:
    hard_limit = max(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES_HARD_LIMIT", _DEFAULT_BROWSER_PAGES_HARD_LIMIT), 1)
    return max(1, min(int(requested or 1), hard_limit))


def _safe_browser_content_char_limit(requested: int) -> int:
    hard_limit = max(
        cfg_int(
            "WEB_RESEARCH_BROWSER_MAX_CONTENT_CHARS_HARD_LIMIT",
            _DEFAULT_BROWSER_CONTENT_CHARS_HARD_LIMIT,
        ),
        400,
    )
    return max(200, min(int(requested or 200), hard_limit))


def _safe_x_timeline_limit(requested: int | None = None) -> int:
    default_limit = max(cfg_int("WEB_RESEARCH_X_TIMELINE_DEFAULT_POSTS", _DEFAULT_X_TIMELINE_LIMIT), 1)
    hard_limit = max(cfg_int("WEB_RESEARCH_X_TIMELINE_MAX_POSTS", _DEFAULT_X_TIMELINE_HARD_LIMIT), 1)
    value = default_limit if requested is None else int(requested or default_limit)
    return max(1, min(value, hard_limit))


def _x_timeline_extra_wait_sec() -> float:
    return min(
        max(cfg_float("WEB_RESEARCH_X_TIMELINE_EXTRA_WAIT_SEC", _DEFAULT_X_TIMELINE_EXTRA_WAIT_SEC), 0.0),
        15.0,
    )


def _browser_read_retries() -> int:
    return max(cfg_int("WEB_RESEARCH_BROWSER_READ_RETRIES", _DEFAULT_BROWSER_READ_RETRIES), 1)


def _browser_read_retry_delay_sec() -> float:
    return max(cfg_float("WEB_RESEARCH_BROWSER_READ_RETRY_DELAY_SEC", _DEFAULT_BROWSER_READ_RETRY_DELAY_SEC), 0.0)


def _known_term_cache_ttl_sec() -> float:
    return max(cfg_float("WEB_RESEARCH_KNOWN_TERM_CACHE_TTL_SEC", _DEFAULT_KNOWN_TERM_CACHE_TTL_SEC), 60.0)


def _known_term_cache_max_size() -> int:
    return max(cfg_int("WEB_RESEARCH_KNOWN_TERM_CACHE_MAX_SIZE", _DEFAULT_KNOWN_TERM_CACHE_MAX_SIZE), 16)


def _browser_lock_file_path() -> str:
    path = str(cfg("WEB_RESEARCH_BROWSER_LOCK_PATH", _DEFAULT_BROWSER_LOCK_PATH) or "").strip()
    return path or _DEFAULT_BROWSER_LOCK_PATH


def _mcp_session_file_path() -> str:
    path = str(cfg("WEB_RESEARCH_MCP_SESSION_PATH", _DEFAULT_MCP_SESSION_PATH) or "").strip()
    return path or _DEFAULT_MCP_SESSION_PATH


def _browser_dynamic_host_keywords(default_hosts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        keyword.lower()
        for keyword in _cfg_csv_values("WEB_RESEARCH_BROWSER_DYNAMIC_HOSTS", default_hosts)
        if str(keyword or "").strip()
    )


def _is_dynamic_browser_site(url: str, *, xcom: object | None = None) -> bool:
    """URLが動的ブラウザアクセスを必要とするサイト（X.com等）かどうかを判定する。"""
    lowered = str(url or "").lower()
    default_hosts = ("x.com", "twitter.com", "img.2chan.net")
    if xcom is not None:
        dynamic_hosts = _browser_dynamic_host_keywords(
            tuple(getattr(xcom, "DEFAULT_DYNAMIC_BROWSER_HOSTS", default_hosts))
        )
        if hasattr(xcom, "is_dynamic_browser_site"):
            return bool(xcom.is_dynamic_browser_site(url, dynamic_hosts=dynamic_hosts))
    
    dynamic_hosts = _browser_dynamic_host_keywords(default_hosts)
    return any(h in lowered for h in dynamic_hosts)


def _should_use_background_browser_access(url: str, *, xcom: object | None = None) -> bool:
    if not cfg_bool("WEB_RESEARCH_BROWSER_BACKGROUND_NAVIGATION", True):
        return False
    if _is_dynamic_browser_site(url, xcom=xcom) and cfg_bool("WEB_RESEARCH_BROWSER_FOREGROUND_DYNAMIC_SITES", True):
        return False
    return True


def _browser_post_navigation_delay_sec(url: str, *, xcom: object | None = None) -> float:
    default_delay = cfg_float(
        "WEB_RESEARCH_BROWSER_POST_NAVIGATION_DELAY_SEC",
        _DEFAULT_BROWSER_POST_NAVIGATION_DELAY_SEC,
    )
    delay = max(float(default_delay or 0.0), 0.0)
    if _is_dynamic_browser_site(url, xcom=xcom):
        dynamic_delay = cfg_float("WEB_RESEARCH_BROWSER_DYNAMIC_SITE_DELAY_SEC", _DEFAULT_BROWSER_DYNAMIC_SITE_DELAY_SEC)
        delay = max(delay, float(dynamic_delay or 0.0))
    return min(delay, 15.0)
