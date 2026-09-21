from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from .config_helpers import cfg
from .http_session import get_shared_http_session
from .json_compat import loads as json_loads

log = logging.getLogger("ollama_bot.common.fact_check")

from lib.media_utils import (
    IMAGE_EXTENSIONS as _IMAGE_EXTENSIONS,
    is_image_url as _is_image_url,
    normalize_hostname as _normalize_hostname,
    path_suffix as _path_suffix,
)

from lib.discord_utils import DISCORD_MENTION_RE as _DISCORD_MENTION_RE
from lib.text_utils import _URL_RE, _normalize_url_candidate, extract_urls

_TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_SOCIAL_REDIRECT_HOSTS = _TWITTER_HOSTS | {"t.co", "pic.x.com"}
_SOCIAL_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com", "ton.twimg.com"}
_INLINE_CONTEXT_MIN_CHARS = 30


ReplyActionType = Literal["fact_check", "summarize", "what_is_this", "simplify"]
ReplyInvestigateMode = ReplyActionType

# 明示的な真偽確認を最優先し、その次に要約を優先します。
# 「教えて」「解説して」は表現として広いので最後に回して誤爆を減らします。
ACTION_KEYWORDS: dict[ReplyActionType, tuple[str, ...]] = {
    "fact_check": (
        "ファクトチェック",
        "factcheck",
        "検証して",
        "検証",
        "裏取り",
        "裏を取",
        "本当か",
        "本当",
        "ほんと",
        "ホント",
        "真偽",
        "嘘",
        "デマ",
        "ガセ",
        "事実か",
    ),
    "summarize": (
        "要約",
        "要点",
        "まとめて",
        "まとめ",
        "短くして",
        "短めに",
        "一言で",
        "3行",
        "三行",
        "3文",
        "三文",
        "ざっくり",
        "かいつまんで",
        "あらすじ",
        "tldr",
        "tl;dr",
    ),
    "what_is_this": (
        "これ何",
        "これなに",
        "何これ",
        "なにこれ",
        "これは何",
        "これはなに",
        "って何",
        "ってなに",
        "て何",
        "てなに",
        "とは何",
        "とはなに",
        "ってなんだ",
        "てなんだ",
        "って誰",
        "て誰",
        "ってどこ",
        "てどこ",
        "ってどういうこと",
        "ってどういう意味",
        "とは",
        "何者",
        "だれ",
        "誰",
        "どこの",
        "調べて",
        "検索して",
        "検索",
        "リサーチして",
        "詳細",
        "ソース",
        "出典",
    ),
    "simplify": (
        "わかりやすく",
        "分かりやすく",
        "かみくだいて",
        "噛み砕いて",
        "やさしく",
        "優しく",
        "初心者向け",
        "簡単に説明",
        "平たく言うと",
        "どういう意味",
        "どういうこと",
        "解説して",
        "説明して",
        "詳しく教えて",
        "教えて",
        "kwsk",
    ),
}


@dataclass(slots=True)
class FactCheckTargetContext:
    original_text: str
    link_summaries: list[str]
    enriched_text: str
    search_query_source_text: str
    link_context_block: str


def looks_like_url_only_text(text: str) -> bool:
    stripped = re.sub(_URL_RE, " ", text or "")
    stripped = re.sub(r"[\s\u3000、。,.!！?？:：;；()（）「」『』【】\[\]<>]+", "", stripped)
    return bool(text and extract_urls(text)) and not stripped


def _strip_urls_for_context(text: str) -> str:
    stripped = re.sub(_URL_RE, " ", text or "")
    stripped = re.sub(r"[\s\u3000、。,.!！?？:：;；()（）「」『』【】\[\]<>|/]+", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def has_enough_inline_context(text: str, *, min_chars: int = _INLINE_CONTEXT_MIN_CHARS) -> bool:
    return len(_strip_urls_for_context(text)) >= max(int(min_chars), 1)


def should_build_link_summaries(text: str, *, min_chars: int = _INLINE_CONTEXT_MIN_CHARS) -> bool:
    if not extract_urls(text):
        return False
    if looks_like_url_only_text(text):
        return True
    return not has_enough_inline_context(text, min_chars=min_chars)


def _normalize_reply_action_text(text: str) -> str:
    normalized = _DISCORD_MENTION_RE.sub(" ", str(text or ""))
    normalized = re.sub(r"[@＠][^\s\u3000]+", " ", normalized)
    normalized = normalized.replace("　", " ").lower()
    return re.sub(r"\s+", "", normalized)


def detect_reply_action(text: str) -> ReplyActionType | None:
    normalized = _normalize_reply_action_text(text)
    if not normalized:
        return None

    for action, keywords in ACTION_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return action
    return None


def classify_reply_investigate_mode(text: str) -> ReplyInvestigateMode | None:
    return detect_reply_action(text)


def _extract_twitter_status_id(url: str) -> str | None:

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if _normalize_hostname(url) not in _TWITTER_HOSTS:
        return None

    m = re.search(r"/status/(\d+)", parsed.path or "")
    if not m:
        return None
    return m.group(1)


def _looks_like_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", str(value or "").strip(), flags=re.IGNORECASE))


def _is_external_article_url(url: str) -> bool:
    normalized = _normalize_url_candidate(str(url or "").strip())
    if not _looks_like_http_url(normalized):
        return False

    host = _normalize_hostname(normalized)
    if not host or host in _SOCIAL_REDIRECT_HOSTS or host in _SOCIAL_MEDIA_HOSTS:
        return False
    return not _is_image_url(normalized)


def _is_resolvable_payload_url(url: str) -> bool:
    normalized = _normalize_url_candidate(str(url or "").strip())
    if not _looks_like_http_url(normalized):
        return False

    host = _normalize_hostname(normalized)
    if not host or host in _SOCIAL_MEDIA_HOSTS:
        return False
    return not _is_image_url(normalized)


def _collect_external_urls_from_payload(value: Any, *, seen: set[str], out: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str):
                candidate = _normalize_url_candidate(child.strip())
                if key.lower().endswith("url") and _is_resolvable_payload_url(candidate) and candidate not in seen:
                    seen.add(candidate)
                    out.append(candidate)
                    continue
            _collect_external_urls_from_payload(child, seen=seen, out=out)
        return

    if isinstance(value, list):
        for child in value:
            _collect_external_urls_from_payload(child, seen=seen, out=out)


def _clean_text(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_meta_content(html_text: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html_text, flags=re.IGNORECASE)
        if m:
            return _clean_text(m.group(1))
    return ""


def _extract_title(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _clean_text(m.group(1))


async def _resolve_short_url(url: str, *, timeout_sec: float) -> str | None:
    headers = {"User-Agent": "discord-ai-bot/1.0"}
    try:
        session = await get_shared_http_session(timeout_sec=timeout_sec, headers=headers)
        async with session.get(url, allow_redirects=True) as resp:
            resolved = str(resp.url or "").strip()
            if resolved and resolved != url and _is_external_article_url(resolved):
                return resolved
    except Exception as e:
        log.debug("short url resolve failed: %s", e)
    return None


async def _fetch_twitter_status_context(url: str, *, timeout_sec: float) -> dict[str, Any] | None:
    status_id = _extract_twitter_status_id(url)
    if not status_id:
        return None

    headers = {"User-Agent": "discord-ai-bot/1.0"}
    payload: dict[str, Any] | None = None
    endpoints = (
        f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&lang=ja",
        f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&lang=en",
    )

    for endpoint in endpoints:
        try:
            session = await get_shared_http_session(timeout_sec=timeout_sec, headers=headers)
            async with session.get(endpoint, allow_redirects=True) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None, loads=json_loads)
                if isinstance(data, dict):
                    payload = data
                    break
        except Exception as e:
            log.debug("twitter status context fetch failed: %s", e)

    if not payload:
        return None

    tweet_text = _clean_text(str(payload.get("text") or "").strip())

    external_urls: list[str] = []
    _collect_external_urls_from_payload(payload, seen=set(), out=external_urls)

    resolved_urls: list[str] = []
    seen_urls: set[str] = set()
    for candidate in external_urls:
        resolved = candidate
        if _normalize_hostname(candidate) in {"t.co", "pic.x.com"}:
            resolved = await _resolve_short_url(candidate, timeout_sec=timeout_sec) or candidate
        if _is_external_article_url(resolved) and resolved not in seen_urls:
            seen_urls.add(resolved)
            resolved_urls.append(resolved)

    return {
        "tweet_text": tweet_text,
        "external_urls": resolved_urls,
    }


async def _fetch_link_summary(url: str, *, timeout_sec: float, max_chars: int) -> str | None:
    headers = {"User-Agent": "discord-ai-bot/1.0"}
    try:
        session = await get_shared_http_session(timeout_sec=timeout_sec, headers=headers)
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            content_type = str(resp.headers.get("Content-Type", "") or "").lower()
            if content_type.startswith("image/"):
                return None
            text = await resp.text(errors="ignore")
    except Exception as e:
        log.debug("link summary fetch failed: %s", e)
        return None

    title = (
        _extract_meta_content(text, "og:title")
        or _extract_meta_content(text, "twitter:title")
        or _extract_title(text)
    )
    description = (
        _extract_meta_content(text, "og:description")
        or _extract_meta_content(text, "twitter:description")
        or _extract_meta_content(text, "description")
    )
    if not description:
        description = _clean_text(text)[:max_chars]
    else:
        description = description[:max_chars]

    pieces: list[str] = [url]
    if title:
        pieces.append(f"タイトル: {title[:120]}")
    if description:
        pieces.append(f"要約: {description}")
    if len(pieces) <= 1:
        return None
    return " | ".join(pieces)


async def _build_url_summary_candidates(url: str, *, timeout_sec: float, max_chars: int) -> list[str]:
    status_id = _extract_twitter_status_id(url)
    if status_id:
        context = await _fetch_twitter_status_context(url, timeout_sec=timeout_sec)
        if context:
            summaries: list[str] = []
            tweet_text = str(context.get("tweet_text") or "").strip()
            if tweet_text:
                summaries.append(f"X投稿: {tweet_text[:max_chars]}")

            external_urls = list(context.get("external_urls") or [])
            for external_url in external_urls[:2]:
                summary = await _fetch_link_summary(external_url, timeout_sec=timeout_sec, max_chars=max_chars)
                if summary:
                    summaries.append(f"投稿内リンク: {summary}")

            if summaries:
                return summaries

    summary = await _fetch_link_summary(url, timeout_sec=timeout_sec, max_chars=max_chars)
    return [summary] if summary else []


async def build_link_summaries_from_text(
    text: str,
    *,
    max_links: int | None = None,
    timeout_sec: float | None = None,
    max_summary_chars: int | None = None,
) -> list[str]:
    resolved_max_links = max(int(max_links if max_links is not None else (cfg("OLLAMA_MAX_LINK_SUMMARIES", 2) or 2)), 0)
    resolved_timeout_sec = float(timeout_sec if timeout_sec is not None else (cfg("OLLAMA_MEDIA_FETCH_TIMEOUT_SEC", 5) or 5))
    resolved_max_summary_chars = max(
        int(max_summary_chars if max_summary_chars is not None else (cfg("OLLAMA_MAX_LINK_SUMMARY_CHARS", 240) or 240)),
        60,
    )

    if resolved_max_links <= 0 or not should_build_link_summaries(text):
        return []

    urls = [u for u in extract_urls(text) if not _is_image_url(u)]
    summaries: list[str] = []
    seen: set[str] = set()

    for url in urls[:resolved_max_links]:
        for summary in await _build_url_summary_candidates(
            url,
            timeout_sec=resolved_timeout_sec,
            max_chars=resolved_max_summary_chars,
        ):
            normalized = str(summary or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            summaries.append(normalized)

    return summaries


def build_search_query_source_text(target_text: str, link_summaries: list[str]) -> str:
    source_text = "\n".join(link_summaries) if link_summaries else str(target_text or "")
    source_text = re.sub(r"https?://\S+", " ", source_text)
    source_text = re.sub(r"\s+", " ", source_text).strip()
    return source_text or str(target_text or "").strip()


def build_link_context_block(link_summaries: list[str]) -> str:
    if not link_summaries:
        return ""
    return "【URL解決結果】\n" + "\n".join(f"- {item}" for item in link_summaries)


async def build_factcheck_target_context(target_text: str) -> FactCheckTargetContext:
    normalized_target_text = str(target_text or "").strip()
    if not normalized_target_text:
        return FactCheckTargetContext(
            original_text="",
            link_summaries=[],
            enriched_text="",
            search_query_source_text="",
            link_context_block="",
        )

    try:
        link_summaries = await build_link_summaries_from_text(normalized_target_text)
    except Exception as e:
        log.warning("link context build failed: %s", e)
        link_summaries = []

    link_context_block = build_link_context_block(link_summaries)
    enriched_text = normalized_target_text
    if link_context_block:
        enriched_text = f"{normalized_target_text}\n\n{link_context_block}"

    return FactCheckTargetContext(
        original_text=normalized_target_text,
        link_summaries=link_summaries,
        enriched_text=enriched_text,
        search_query_source_text=build_search_query_source_text(normalized_target_text, link_summaries),
        link_context_block=link_context_block,
    )


def extract_embed_text_from_message(message: Any) -> str:
    embeds = getattr(message, "embeds", None) or []
    embed_texts: list[str] = []
    for embed in embeds:
        title = str(getattr(embed, "title", "") or "").strip()
        description = str(getattr(embed, "description", "") or "").strip()
        if title:
            embed_texts.append(title)
        if description:
            embed_texts.append(description)
        for field in getattr(embed, "fields", []) or []:
            field_name = str(getattr(field, "name", "") or "").strip()
            field_value = str(getattr(field, "value", "") or "").strip()
            if field_name:
                embed_texts.append(field_name)
            if field_value:
                embed_texts.append(field_value)
    return "\n".join(embed_texts).strip()


def extract_target_text_from_message(message: Any) -> str:
    target_text = str(getattr(message, "content", "") or "").strip()
    if target_text and not looks_like_url_only_text(target_text):
        return target_text

    embed_text = extract_embed_text_from_message(message)
    parts = [part for part in (target_text, embed_text) if part]
    return "\n".join(parts).strip()


async def search_web(query: str, max_results: int = 3) -> str:
    try:
        results = await search_web_entries(query, max_results=max_results)
    except asyncio.TimeoutError:
        return "検索がタイムアウトしました。"
    except Exception as e:
        return f"検索中にエラーが発生しました: {e}"

    if not results:
        return "検索結果が見つかりませんでした。"

    lines: list[str] = []
    for r in results:
        title = str(r.get("title") or "").strip()
        body = str(r.get("body") or "").strip()
        href = str(r.get("href") or "").strip()

        parts = []
        if title:
            parts.append(title)
        if body:
            parts.append(body)
        if href:
            parts.append(f"URL: {href}")
        if parts:
            lines.append("- " + " / ".join(parts))

    return "\n".join(lines) if lines else "検索結果が見つかりませんでした。"


async def _fallback_http_search(query: str, max_results: int = 3) -> list[dict[str, str]]:
    """外部ライブラリがない場合の aiohttp による軽量 DuckDuckGo HTML 検索フォールバック"""
    try:
        import aiohttp
        from urllib.parse import quote_plus
        from html import unescape
        from .http_session import get_shared_http_session

        search_query = query
        if not re.search(r"[あ-んア-ン\u4e00-\u9fff]", query) and not query.startswith("site:"):
            search_query += " 日本語"

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        session = await get_shared_http_session()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
            if resp.status == 200:
                html = await resp.text()
                # DuckDuckGo HTML 版のスニペット・タイトル・URLリンクを正規表現で抽出
                snippets = re.findall(r'<a class="result__snippet[^\"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, flags=re.DOTALL)
                if not snippets:
                    raw_snippets = re.findall(r'<a class="result__snippet[^\"]*"[^>]*>(.*?)</a>', html, flags=re.DOTALL)
                    snippets = [("", s) for s in raw_snippets]

                title_matches = re.findall(r'<h2 class="result__title"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, flags=re.DOTALL)
                if not title_matches:
                    title_matches = re.findall(r'<a class="result__url"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, flags=re.DOTALL)

                from urllib.parse import unquote
                results: list[dict[str, str]] = []
                for i in range(min(len(snippets), max_results)):
                    snip_href, snip_text = snippets[i]
                    title_href = title_matches[i][0] if i < len(title_matches) else ""
                    title_text = title_matches[i][1] if i < len(title_matches) else ""

                    raw_href = title_href or snip_href
                    href = raw_href
                    m = re.search(r'uddg=([^&]+)', raw_href)
                    if m:
                        href = unquote(m.group(1))
                    elif href.startswith("//"):
                        href = "https:" + href

                    clean_snip = unescape(re.sub(r'<[^>]+>', '', snip_text)).strip()
                    clean_title = unescape(re.sub(r'<[^>]+>', '', title_text)).strip()
                    if clean_snip or clean_title:
                        results.append({
                            "title": clean_title or "検索結果",
                            "body": clean_snip,
                            "href": href,
                        })
                if results:
                    log.info("fallback http search succeeded: query=%r results=%d", search_query, len(results))
                    return results
    except Exception as e:
        log.warning("fallback http search failed: %r", e)
    return []


async def search_web_entries(query: str, max_results: int = 3) -> list[dict[str, str]]:
    def _search() -> list[dict[str, str]]:
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return []

        search_query = query
        if not re.search(r"[あ-んア-ン\u4e00-\u9fff]", query) and not query.startswith("site:"):
            search_query += " 日本語"

        try:
            with DDGS() as ddgs:
                raw_results = list(
                    ddgs.text(
                        search_query,
                        region="jp-jp",
                        safesearch="off",
                        max_results=max_results,
                    )
                )
        except Exception as e:
            log.warning("DDGS query execution failed: %r", e)
            return []

        normalized_results: list[dict[str, str]] = []
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            normalized_results.append({
                "title": str(result.get("title") or "").strip(),
                "body": str(result.get("body") or "").strip(),
                "href": str(result.get("href") or "").strip(),
            })
        return normalized_results

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_search),
            timeout=4.0,
        )
        if results:
            return results
    except Exception as e:
        log.warning("DDGS thread search failed: %r", e)

    # ライブラリ未導入または検索失敗時の HTTP 直接検索フォールバック
    return await _fallback_http_search(query, max_results=max_results)

