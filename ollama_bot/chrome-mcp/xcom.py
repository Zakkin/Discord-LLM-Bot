from __future__ import annotations

import re
from urllib.parse import urlparse

from lib.text_utils import compact_whitespace as _compact_whitespace




DEFAULT_DYNAMIC_BROWSER_HOSTS = ("x.com", "twitter.com")
X_TIMELINE_HOME_URL = "https://x.com/home"
X_TIMELINE_TWEET_SELECTOR = 'div[data-testid="primaryColumn"] article[data-testid="tweet"]'
DEFAULT_X_TIMELINE_LIMIT = 10

LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don’t miss what’s happening",
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
    "something went wrong, but don’t fret",
    "some privacy related extensions may cause issues on x.com",
)
STRONG_LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "don't miss what's happening",
    "don’t miss what’s happening",
    "people on x are the first to know",
    "some privacy related extensions may cause issues on x.com",
)
LOGIN_WALL_MAX_CONTENT_LEN = 600

_ASCII_X_RE = re.compile(r"(?<![a-z0-9])(?:x|twitter)(?![a-z0-9])", re.IGNORECASE)
_ASCII_TL_RE = re.compile(r"(?<![a-z0-9])(?:tl|timeline|feed)(?![a-z0-9])", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_]{1,15}$")
_HANDLE_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{1,15}(?![A-Za-z0-9_])")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_COUNT_RE = re.compile(
    r"(?:直近|最新|最近)?\s*([0-9０-９]{1,2})\s*(?:件|個|ツイート|ポスト|tweet|tweets|post|posts)"
)
_TIMESTAMP_RE = re.compile(
    r"^(?:·|[0-9０-９]+(?:秒|分|時間|日|週間|ヶ月|か月|年|s|m|h|d|w|mo|y)|"
    r"[0-9０-９]{1,2}月[0-9０-９]{1,2}日|昨日|今日|今|now|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*[0-9]{1,2}(?:,\s*[0-9]{4})?|"
    r"[0-9]{1,2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s*[0-9]{4})?)$",
    re.IGNORECASE,
)
_METRIC_RE = re.compile(r"^[0-9０-９,.，万億kmbKMB]+$")
_ACCESSIBILITY_NODE_TAIL_RE = re.compile(
    r"(?:^|\s)(?:overflow\s+)?"
    r"(?:generic|button|heading|link|navigation|main|tablist|tab|textbox|banner|presentation|article|img|image|list|listitem|paragraph|text|statictext)"
    r"\b(?:\s+\"[^\"]*\")?\s+\[ref=[^\]]+\].*$",
    re.IGNORECASE,
)
_ACCESSIBILITY_NODE_QUOTED_RE = re.compile(
    r"^\s*(?:overflow\s+)?"
    r"(?:generic|button|heading|link|navigation|main|tablist|tab|textbox|banner|presentation|article|img|image|list|listitem|paragraph|text|statictext)"
    r"\b\s+\"((?:\\.|[^\"])*)\"\s+\[ref=[^\]]+\].*$",
    re.IGNORECASE,
)
_ACCESSIBILITY_REF_RE = re.compile(r"\[ref=[^\]]+\](?:\s*\([^)]*\))?")
_ACCESSIBILITY_COORD_RE = re.compile(r"\s*\(x=\d+\s*,\s*y=\d+\)")
_MAX_TIMELINE_ENTRY_TEXT_CHARS = 420
_INLINE_ENTRY_STOP_MARKERS = (
    "いまどうしてる？",
    "ホームタイムライン",
    "おすすめフォロー中",
    "キーボードショートカット",
    "ホームタイムラインに移動",
    "トレンドに移動",
    "メインメニュー",
    "新しいポストを表示",
    "プレミアムサブスクライブ",
    "本日のニュース",
    "いまを見つけよう",
    "おすすめユーザー",
    "おすすめユーザーを表示",
    "Subscribe to Premium",
    "What’s happening",
    "What's happening",
    "Who to follow",
)
_INLINE_ENTRY_DROP_IF_PREFIX_MARKERS = (
    "いまどうしてる？",
    "ホームタイムライン",
    "おすすめフォロー中",
    "キーボードショートカット",
    "ホームタイムラインに移動",
    "トレンドに移動",
    "メインメニュー",
    "新しいポストを表示",
    "プレミアムサブスクライブ",
    "本日のニュース",
    "いまを見つけよう",
    "おすすめユーザー",
    "おすすめユーザーを表示",
    "Subscribe to Premium",
    "What’s happening",
    "What's happening",
    "Who to follow",
)
_SIDE_COLUMN_START_LINES = {
    "subscribe to premium",
    "today's news",
    "today’s news",
    "trends for you",
    "what's happening",
    "what’s happening",
    "who to follow",
    "いまを見つけよう",
    "おすすめトレンド",
    "おすすめユーザー",
    "おすすめユーザーを表示",
    "プレミアムサブスクライブ",
    "本日のニュース",
}
_SIDE_COLUMN_INLINE_START_MARKERS = (
    "プレミアムサブスクライブ",
    "本日のニュース",
    "いまを見つけよう",
    "おすすめトレンド",
    "おすすめユーザー",
    "おすすめユーザーを表示",
    "Subscribe to Premium",
    "Today's News",
    "Today’s News",
    "Trends for you",
    "What’s happening",
    "What's happening",
    "Who to follow",
)

_TIMELINE_CONTROL_LINES = {
    "ad",
    "bookmark",
    "bookmarks",
    "follow",
    "following",
    "for you",
    "from",
    "gif",
    "home",
    "image",
    "like",
    "likes",
    "post",
    "posts",
    "promoted",
    "quote",
    "quotes",
    "reply",
    "replies",
    "repost",
    "reposts",
    "share",
    "show more",
    "show replies",
    "show this thread",
    "translate post",
    "video",
    "view post analytics",
    "views",
    "おすすめ",
    "さらに表示",
    "タイムライン",
    "フォロー",
    "フォロー中",
    "ブックマーク",
    "ホーム",
    "ポスト",
    "もっと見る",
    "リポスト",
    "いいね",
    "共有",
    "広告",
    "返信",
    "表示",
    "画像",
    "翻訳",
    "動画",
    "いまどうしてる？",
    "ホームタイムライン",
    "おすすめフォロー中",
    "キーボードショートカットを表示",
    "ダイレクトメッセージ",
    "トレンド",
    "ニュース",
    "プロフィール",
    "ポスト本文",
    "メインメニュー",
    "前",
    "次へ",
    "調べたいものを検索",
    "通知",
    "本日のニュース",
    "人気の画像",
    "その他のメニュー項目",
    "アカウントメニュー",
    "いまを見つけよう",
    "おすすめトレンド",
    "おすすめユーザー",
    "おすすめユーザーを表示",
    "プレミアムサブスクライブ",
    "subscribe to premium",
    "today's news",
    "today’s news",
    "trends for you",
    "what's happening",
    "what’s happening",
    "who to follow",
}


def hostname_from_url(url: str) -> str:
    try:
        return str(urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return ""


def is_dynamic_browser_site(
    url: str,
    *,
    dynamic_hosts: tuple[str, ...] | list[str] | set[str] | None = None,
) -> bool:
    hostname = hostname_from_url(url)
    if not hostname:
        return False

    for raw_host in dynamic_hosts or DEFAULT_DYNAMIC_BROWSER_HOSTS:
        host = str(raw_host or "").strip().lower()
        if host and (hostname == host or hostname.endswith(f".{host}")):
            return True
    return False


def is_login_wall_or_error_page(content_text: str) -> bool:
    stripped = str(content_text or "").strip()
    if not stripped:
        return True

    lowered = stripped.lower()
    if any(phrase in lowered for phrase in STRONG_LOGIN_WALL_PHRASES):
        return True
    if len(stripped) <= LOGIN_WALL_MAX_CONTENT_LEN:
        return any(phrase in lowered for phrase in LOGIN_WALL_PHRASES)
    return False



def _strip_accessibility_snapshot_tail(text: object) -> str:
    value = str(text or "")
    quoted_node = _ACCESSIBILITY_NODE_QUOTED_RE.match(value)
    if quoted_node:
        return str(quoted_node.group(1) or "").replace(r"\"", '"').strip()
    value = _ACCESSIBILITY_NODE_TAIL_RE.sub("", value)
    value = _ACCESSIBILITY_REF_RE.sub("", value)
    value = _ACCESSIBILITY_COORD_RE.sub("", value)
    return value


def _matchable_text(text: str) -> str:
    lowered = str(text or "").replace("　", " ").lower()
    return re.sub(r"\s+", "", lowered)


def _has_x_reference(text: str) -> bool:
    lowered = str(text or "").lower()
    compacted = _matchable_text(text)
    return bool(
        _ASCII_X_RE.search(lowered)
        or "ｘ" in compacted
        or "ツイッター" in compacted
        or "twitter" in compacted
        or "旧twitter" in compacted
        or "旧ツイッター" in compacted
    )


def _has_timeline_reference(text: str) -> bool:
    lowered = str(text or "").lower()
    compacted = _matchable_text(text)
    return bool(
        _ASCII_TL_RE.search(lowered)
        or "タイムライン" in compacted
        or "フィード" in compacted
        or "ホームタイムライン" in compacted
        or "ホームtl" in compacted
    )


def is_x_timeline_request(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False

    compacted = _matchable_text(raw)
    if not compacted:
        return False

    live_or_action = any(
        marker in compacted
        for marker in (
            "今",
            "いま",
            "現在",
            "最新",
            "直近",
            "最近",
            "流れて",
            "流れる",
            "流れてる",
            "流れている",
            "どうなって",
            "見て",
            "みて",
            "読んで",
            "拾って",
            "取得",
            "確認",
            "教えて",
        )
    )
    current_or_action = live_or_action or any(marker in compacted for marker in ("何", "なに"))
    explanation_only = any(
        marker in compacted
        for marker in (
            "タイムラインとは",
            "tlとは",
            "timelineとは",
            "タイムラインの意味",
            "タイムライン機能",
            "tlの意味",
        )
    ) and not live_or_action

    home_reference = (
        ("ホーム" in compacted and "ホームページ" not in compacted)
        or bool(re.search(r"(?<![a-z0-9])home(?![a-z0-9])", raw, flags=re.IGNORECASE))
    )

    if _has_x_reference(raw) and (_has_timeline_reference(raw) or home_reference) and not explanation_only:
        return True

    direct_tl_request = _has_timeline_reference(raw) and current_or_action
    direct_tweet_flow = (
        any(marker in compacted for marker in ("今", "いま", "現在", "最新", "直近"))
        and any(marker in compacted for marker in ("流れて", "流れてる", "流れている", "流れる"))
        and any(marker in compacted for marker in ("ツイート", "tweet", "ポスト", "post"))
    )
    x_flow_request = (
        _has_x_reference(raw)
        and any(marker in compacted for marker in ("流れて", "流れてる", "流れている", "流れる"))
        and any(marker in compacted for marker in ("今", "いま", "現在", "最新", "直近", "何が", "なにが"))
    )
    return bool((direct_tl_request or direct_tweet_flow or x_flow_request) and not explanation_only)


def extract_x_timeline_limit(
    text: str,
    *,
    default: int = DEFAULT_X_TIMELINE_LIMIT,
    max_limit: int = DEFAULT_X_TIMELINE_LIMIT,
) -> int:
    fallback = max(int(default or DEFAULT_X_TIMELINE_LIMIT), 1)
    hard_limit = max(int(max_limit or fallback), 1)
    for match in _COUNT_RE.finditer(str(text or "")):
        raw_count = str(match.group(1) or "").translate(_FULLWIDTH_DIGITS)
        try:
            count = int(raw_count)
        except ValueError:
            continue
        return max(1, min(count, hard_limit))
    return max(1, min(fallback, hard_limit))


def _clean_timeline_line(line: object) -> str:
    value = str(line or "").replace("\u200b", " ").replace("\ufeff", " ")
    value = _strip_accessibility_snapshot_tail(value)
    value = re.sub(r"^[\s│├└─>*+-]+", "", value)
    for _ in range(3):
        value = re.sub(r"^(?:\[[^\]]+\]|ref_[A-Za-z0-9_-]+[:)]?)\s*", "", value)
        value = re.sub(r"^[\s│├└─>*+-]+", "", value)
    value = value.strip("\"'“”")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip(" \t\r\n")


def _is_timeline_noise_line(line: str) -> bool:
    value = _clean_timeline_line(line)
    if not value:
        return True
    lowered = value.lower()
    if lowered in _TIMELINE_CONTROL_LINES or value in _TIMELINE_CONTROL_LINES:
        return True
    if _TIMESTAMP_RE.fullmatch(value):
        return True
    if _METRIC_RE.fullmatch(value):
        return True
    if _matchable_text(value) in {"ホームタイムライン", "おすすめフォロー中", "キーボードショートカットを表示"}:
        return True
    if lowered.startswith("replying to "):
        return True
    if lowered.endswith(" reposted") or value.endswith("さんがリポスト"):
        return True
    if "認証済みアカウント" in value or "verified account" in lowered:
        return True
    if _ACCESSIBILITY_REF_RE.search(str(line or "")):
        return True
    return False


def _is_side_column_start_line(line: str) -> bool:
    value = _clean_timeline_line(line)
    if not value:
        return False
    lowered = value.lower()
    compacted = _matchable_text(value)
    if lowered in _SIDE_COLUMN_START_LINES or value in _SIDE_COLUMN_START_LINES:
        return True
    return compacted in {
        "いまを見つけよう",
        "おすすめトレンド",
        "おすすめユーザー",
        "おすすめユーザーを表示",
        "プレミアムサブスクライブ",
        "本日のニュース",
    }


def _trim_timeline_post_text(text: object) -> str:
    value = _compact_whitespace(_strip_accessibility_snapshot_tail(text))
    if not value:
        return ""
    for marker in _INLINE_ENTRY_STOP_MARKERS:
        index = value.find(marker)
        if index < 0:
            continue
        if index == 0 and marker in _INLINE_ENTRY_DROP_IF_PREFIX_MARKERS:
            return ""
        if index > 0:
            value = value[:index].strip()
    value = value.strip(" -:：|/　")
    if len(value) > _MAX_TIMELINE_ENTRY_TEXT_CHARS:
        value = value[:_MAX_TIMELINE_ENTRY_TEXT_CHARS].rstrip(" -:：|/　") + "..."
    return value


def _split_timeline_lines(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_timeline_line(line) for line in normalized.split("\n")]
    return [line for line in lines if line]


def _trim_side_column_suffix_text(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    end_index = len(value)
    for marker in _SIDE_COLUMN_INLINE_START_MARKERS:
        marker_index = lowered.find(str(marker).lower())
        if marker_index >= 0:
            end_index = min(end_index, marker_index)
    return value[:end_index].strip()


def _select_timeline_candidate_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []

    first_handle_index = next(
        (index for index, line in enumerate(lines) if _HANDLE_RE.fullmatch(line)),
        None,
    )

    start_index = 0
    candidate_start_limit = first_handle_index if first_handle_index is not None else len(lines)
    for index, line in enumerate(lines[:candidate_start_limit]):
        lowered = line.lower()
        if line in {"フォロー中", "Following"} or lowered == "following":
            start_index = index + 1

    stop_index = len(lines)
    search_start = first_handle_index if first_handle_index is not None else start_index
    for index in range(search_start, len(lines)):
        if _is_side_column_start_line(lines[index]):
            stop_index = index
            break

    return lines[start_index:stop_index]


def _timeline_entry_from_block(
    lines: list[str],
    *,
    handle_index: int,
) -> dict[str, str] | None:
    if handle_index < 0 or handle_index >= len(lines):
        return None

    handle = lines[handle_index]
    if not _HANDLE_RE.fullmatch(handle):
        return None

    author = ""
    if handle_index > 0 and not _is_timeline_noise_line(lines[handle_index - 1]):
        author = lines[handle_index - 1]

    text_lines: list[str] = []
    for line in lines[handle_index + 1:]:
        if _is_side_column_start_line(line):
            break
        if line == author or line == handle:
            continue
        if _HANDLE_RE.fullmatch(line) or _is_timeline_noise_line(line):
            continue
        text_lines.append(line)

    text = _trim_timeline_post_text(" ".join(text_lines))
    if not text:
        return None

    return {
        "author": author,
        "handle": handle,
        "text": text,
        "raw": "\n".join(lines),
    }


def _parse_inline_timeline_entries(text: str, *, limit: int) -> list[dict[str, str]]:
    flattened = _trim_side_column_suffix_text(_compact_whitespace(" ".join(_split_timeline_lines(text))))
    if not flattened:
        return []

    matches = list(_HANDLE_IN_TEXT_RE.finditer(flattened))
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = max(int(limit or DEFAULT_X_TIMELINE_LIMIT), 1)

    for index, match in enumerate(matches):
        segment_start = max(matches[index - 1].end() if index > 0 else 0, 0)
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(flattened)
        before = flattened[segment_start:match.start()].strip(" -:|/　")
        after = flattened[match.end():segment_end].strip(" -:|/　")
        if not after:
            continue

        cleaned_after = _trim_timeline_post_text(after)
        words = [word for word in re.split(r"\s+", cleaned_after) if word]
        text_words = [word for word in words if not _is_timeline_noise_line(word)]
        post_text = _trim_timeline_post_text(" ".join(text_words))
        if not post_text:
            continue

        author_words = [word for word in re.split(r"\s+", before) if word and not _is_timeline_noise_line(word)]
        author = re.sub(r"^[0-9０-９]+[.)．、]\s*", "", author_words[-1]).strip() if author_words else ""
        handle = match.group(0)
        identity = f"{handle.lower()}\n{post_text.lower()}"
        if identity in seen:
            continue
        seen.add(identity)
        entries.append(
            {
                "author": author,
                "handle": handle,
                "text": post_text,
                "raw": flattened[segment_start:segment_end].strip(),
            }
        )
        if len(entries) >= max_items:
            break
    return entries


def parse_x_timeline_entries(text: str, *, limit: int = DEFAULT_X_TIMELINE_LIMIT) -> list[dict[str, str]]:
    lines = _select_timeline_candidate_lines(_split_timeline_lines(text))
    if not lines:
        return []

    handle_indexes = [idx for idx, line in enumerate(lines) if _HANDLE_RE.fullmatch(line)]
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = max(int(limit or DEFAULT_X_TIMELINE_LIMIT), 1)

    for position, handle_index in enumerate(handle_indexes):
        start = max(handle_index - 1, 0)
        end = len(lines)
        if position + 1 < len(handle_indexes):
            next_handle_index = handle_indexes[position + 1]
            end = next_handle_index - 1 if next_handle_index - handle_index > 2 else next_handle_index
        block = lines[start:end]
        entry = _timeline_entry_from_block(block, handle_index=handle_index - start)
        if not entry:
            continue

        identity = f"{entry.get('handle', '').lower()}\n{entry.get('text', '').lower()}"
        if identity in seen:
            continue
        seen.add(identity)
        entries.append(entry)
        if len(entries) >= max_items:
            break

    if not entries:
        seen = set()
        for line in lines:
            if len(list(_HANDLE_IN_TEXT_RE.finditer(line))) != 1:
                continue
            for entry in _parse_inline_timeline_entries(line, limit=1):
                identity = f"{entry.get('handle', '').lower()}\n{entry.get('text', '').lower()}"
                if identity in seen:
                    continue
                seen.add(identity)
                entries.append(entry)
                if len(entries) >= max_items:
                    break
            if len(entries) >= max_items:
                break

    if not entries:
        seen = set()
        for entry in _parse_inline_timeline_entries("\n".join(lines), limit=max_items):
            identity = f"{entry.get('handle', '').lower()}\n{entry.get('text', '').lower()}"
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)
            if len(entries) >= max_items:
                break

    return entries


def format_x_timeline_summary(entries: list[dict[str, str]], *, limit: int = DEFAULT_X_TIMELINE_LIMIT) -> str:
    if not entries:
        return "Xホームタイムラインの投稿を抽出できませんでした。ログイン状態や表示状態を確認してください。"

    lines = [f"Xホームタイムラインでブラウザから観測できた直近{len(entries[:limit])}件:"]
    for index, entry in enumerate(entries[:limit], start=1):
        text = _compact_whitespace(entry.get("text") or "")
        handle = _compact_whitespace(entry.get("handle") or "")
        author = _compact_whitespace(entry.get("author") or "")
        lines.append(f"{index}. 本文: {text}")
        metadata = []
        if handle:
            metadata.append(f"ID={handle}")
        if author:
            metadata.append(f"表示名={author}")
        if metadata:
            lines.append("   補助情報: " + " / ".join(metadata))
    return "\n".join(lines)


__all__ = [
    "DEFAULT_DYNAMIC_BROWSER_HOSTS",
    "DEFAULT_X_TIMELINE_LIMIT",
    "X_TIMELINE_HOME_URL",
    "X_TIMELINE_TWEET_SELECTOR",
    "extract_x_timeline_limit",
    "format_x_timeline_summary",
    "hostname_from_url",
    "is_dynamic_browser_site",
    "is_login_wall_or_error_page",
    "is_x_timeline_request",
    "parse_x_timeline_entries",
]
