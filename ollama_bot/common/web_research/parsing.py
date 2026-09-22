"""
web_research/parsing.py

このファイルはMCPツール結果やHTMLからテキスト・検索エントリを抽出するパーシング関数群を提供します。
主な責務:
- MCP tool_result の content/structuredContent からテキストを抽出
- DuckDuckGo等の検索結果HTMLをパースしてURL+タイトル一覧を取得
- 検索エントリの正規化・重複排除
- ブラウザページ情報（タイトル・本文抜粋）の構造化

AIエージェントが新しい検索エンジンやパーサーを追加する場合はこのファイルを修正してください。
"""

from __future__ import annotations

import contextlib
import logging
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from ..json_compat import loads as json_loads
from ..ollama_helpers import truncate_text
from .config import (
    _DEFAULT_ALLOWED_URL_SCHEMES,
    _DEFAULT_SEARCH_URL_TEMPLATE,
    _safe_browser_content_char_limit,
)
from .helpers import (
    _cleanup_browser_text,
    _compact_whitespace,
    _is_safe_public_browser_url,
    _select_relevant_excerpt,
    _utc_now_iso,
)
from ..config_helpers import cfg

log = logging.getLogger("ollama_bot.common.web_research")


# ---------------------------------------------------------------------------
# Source / result builders
# ---------------------------------------------------------------------------


def _build_source(
    *,
    source_type: str,
    title: str = "",
    url: str = "",
    snippet: str = "",
    content_excerpt: str = "",
    content_text: str = "",
    used_in_answer: bool = False,
    fetched_at: str = "",
    **extra: Any,
) -> dict[str, Any]:
    source = {
        "source_type": str(source_type or "").strip(),
        "title": str(title or "").strip(),
        "url": str(url or "").strip(),
        "snippet": str(snippet or "").strip(),
        "content_excerpt": str(content_excerpt or "").strip(),
        "content_text": str(content_text or "").strip(),
        "used_in_answer": bool(used_in_answer),
        "fetched_at": str(fetched_at or _utc_now_iso()).strip(),
    }
    for key, value in extra.items():
        source[key] = value
    return source


def _build_result(
    *,
    used_web: bool,
    used_browser: bool,
    summary: str,
    sources: list[dict[str, Any]] | None = None,
    confidence: float = 0.0,
    mode: str = "simple_search",
    query: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "used_web": bool(used_web),
        "used_browser": bool(used_browser),
        "summary": str(summary or "").strip(),
        "sources": list(sources or []),
        "confidence": max(0.0, min(float(confidence or 0.0), 1.0)),
        "mode": mode,
        "query": str(query or "").strip(),
        "error": str(error or "").strip(),
    }


# ---------------------------------------------------------------------------
# Source formatting / deduplication
# ---------------------------------------------------------------------------


def _format_sources_summary(sources: list[dict[str, Any]], *, no_results_text: str) -> str:
    if not sources:
        return no_results_text

    lines: list[str] = []
    for source in sources:
        title = str(source.get("title") or "").strip()
        snippet = str(source.get("snippet") or "").strip()
        excerpt = str(source.get("content_excerpt") or "").strip()
        url = str(source.get("url") or "").strip()

        parts: list[str] = []
        if title:
            parts.append(title)
        if snippet:
            parts.append(snippet)
        if excerpt:
            parts.append(f"本文抜粋: {excerpt}")
        if url:
            parts.append(f"URL: {url}")
        if parts:
            lines.append("- " + " / ".join(parts))

    return "\n".join(lines) if lines else no_results_text


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _priority(source: dict[str, Any]) -> tuple[int, int]:
        source_type = str(source.get("source_type") or "").strip()
        type_score = {
            "browser": 3,
            "link_summary": 2,
            "search": 1,
        }.get(source_type, 0)
        richness = len(str(source.get("content_text") or source.get("content_excerpt") or source.get("snippet") or ""))
        return (type_score, richness)

    def _identity(source: dict[str, Any]) -> tuple[str, str]:
        url = str(source.get("url") or "").strip()
        if url:
            return ("url", url)
        title = str(source.get("title") or "").strip()
        preview = str(source.get("content_excerpt") or source.get("snippet") or "").strip()
        return ("text", f"{title}\n{preview}")

    merged_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for source in sources:
        key = _identity(source)
        current = merged_by_key.get(key)
        if current is None:
            merged_by_key[key] = dict(source)
            order.append(key)
            continue

        preferred = dict(current)
        candidate = dict(source)
        if _priority(candidate) > _priority(preferred):
            preferred, candidate = candidate, preferred

        merged = dict(preferred)
        for field in (
            "title",
            "url",
            "snippet",
            "content_excerpt",
            "content_text",
            "fetched_at",
            "source_type",
        ):
            if not str(merged.get(field) or "").strip() and str(candidate.get(field) or "").strip():
                merged[field] = candidate.get(field)
        merged["used_in_answer"] = bool(current.get("used_in_answer") or source.get("used_in_answer"))
        if "quality" in current or "quality" in source:
            merged["quality"] = max(int(current.get("quality") or 0), int(source.get("quality") or 0))
        merged_by_key[key] = merged

    return [merged_by_key[key] for key in order]


def _normalize_search_source(entry: dict[str, Any]) -> dict[str, Any]:
    return _build_source(
        source_type="search",
        title=str(entry.get("title") or "").strip(),
        url=str(entry.get("href") or entry.get("url") or "").strip(),
        snippet=str(entry.get("body") or entry.get("snippet") or "").strip(),
        content_excerpt=str(entry.get("content_excerpt") or "").strip(),
        used_in_answer=False,
        source_backend=str(entry.get("source_backend") or "").strip(),
    )


# ---------------------------------------------------------------------------
# MCP result parsing utilities
# ---------------------------------------------------------------------------


def _deep_collect_named_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                found.append(child)
            found.extend(_deep_collect_named_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_deep_collect_named_values(child, keys))
    return found


def _first_non_empty_string(values: list[Any]) -> str:
    for value in values:
        text = _compact_whitespace(value)
        if text:
            return text
    return ""


def _json_payload_from_text(text: Any) -> Any | None:
    raw = str(text or "").strip()
    if not raw or raw[0] not in "[{":
        return None
    with contextlib.suppress(Exception):
        parsed = json_loads(raw)
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _iter_tool_result_payloads(result: dict[str, Any]) -> list[Any]:
    payloads: list[Any] = []
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        payloads.append(structured)

    for block in list(result.get("content") or []):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "json":
            json_value = block.get("json")
            if isinstance(json_value, (dict, list)):
                payloads.append(json_value)
        elif block_type == "text":
            parsed = _json_payload_from_text(block.get("text"))
            if isinstance(parsed, (dict, list)):
                payloads.append(parsed)
    return payloads


def _extract_text_from_tool_result(result: dict[str, Any]) -> str:
    _TEXT_KEYS = {
        "text",
        "content",
        "textContent",
        "innerText",
        "markdown",
        "body",
        "html",
        "htmlContent",
        "pageContent",
    }
    chunks: list[str] = []
    for block in list(result.get("content") or []):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            parsed = _json_payload_from_text(block.get("text"))
            if isinstance(parsed, (dict, list)):
                extracted = _first_non_empty_string(
                    _deep_collect_named_values(parsed, _TEXT_KEYS)
                )
                if extracted:
                    chunks.append(extracted)
            else:
                text = _compact_whitespace(block.get("text"))
                if text:
                    chunks.append(text)
        elif block_type == "json":
            json_value = block.get("json")
            if json_value is not None:
                chunks.append(_compact_whitespace(json_value))
    for structured in _iter_tool_result_payloads(result):
        chunks.append(
            _first_non_empty_string(
                _deep_collect_named_values(structured, _TEXT_KEYS)
            )
        )
    return _compact_whitespace(" ".join(chunk for chunk in chunks if chunk))


def _extract_raw_text_from_tool_result(result: dict[str, Any]) -> str:
    raw_text_keys = {
        "text",
        "content",
        "textContent",
        "innerText",
        "markdown",
        "body",
        "pageContent",
    }
    chunks: list[str] = []
    for block in list(result.get("content") or []):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            text = str(block.get("text") or "").strip()
            parsed = _json_payload_from_text(text)
            if isinstance(parsed, (dict, list)):
                for value in _deep_collect_named_values(parsed, raw_text_keys):
                    extracted = str(value or "").strip()
                    if extracted:
                        chunks.append(extracted)
                continue
            if text:
                chunks.append(text)
        elif block_type == "json" and block.get("json") is not None:
            json_value = block.get("json")
            if isinstance(json_value, (dict, list)):
                for value in _deep_collect_named_values(json_value, raw_text_keys):
                    extracted = str(value or "").strip()
                    if extracted:
                        chunks.append(extracted)
            else:
                chunks.append(str(json_value or "").strip())

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        for value in _deep_collect_named_values(structured, raw_text_keys):
            text = str(value or "").strip()
            if text:
                chunks.append(text)
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def _extract_browser_page(source_url: str, result: dict[str, Any], *, query: str = "") -> dict[str, Any]:
    title = ""
    page_url = source_url
    for structured in _iter_tool_result_payloads(result):
        title = _first_non_empty_string(
            _deep_collect_named_values(structured, {"title", "pageTitle", "tabTitle"})
        ) or title
        page_url = _first_non_empty_string(
            _deep_collect_named_values(structured, {"url", "pageUrl", "currentUrl"})
        ) or source_url
        if title and page_url != source_url:
            break

    combined_text = _cleanup_browser_text(_extract_text_from_tool_result(result))
    content_limit = _safe_browser_content_char_limit(cfg("WEB_RESEARCH_BROWSER_MAX_CONTENT_CHARS", 1600))
    content_text = truncate_text(combined_text, content_limit * 3)
    return {
        "title": truncate_text(title, 180),
        "url": page_url,
        "content_text": content_text,
        "content_excerpt": _select_relevant_excerpt(
            content_text,
            query or title or source_url,
            max_chars=content_limit,
        ),
    }


# ---------------------------------------------------------------------------
# Tab ID extraction
# ---------------------------------------------------------------------------


def _extract_tab_id(result: dict[str, Any]) -> int | None:
    candidates = _deep_collect_named_values(result, {"tabId", "tab_id", "activeTabId"})
    for payload in _iter_tool_result_payloads(result):
        candidates.extend(_deep_collect_named_values(payload, {"tabId", "tab_id", "activeTabId"}))
    if not candidates:
        try:
            text = _extract_text_from_tool_result(result)
            if text.startswith("{") and text.endswith("}"):
                parsed = json_loads(text)
                if isinstance(parsed, dict):
                    candidates.extend(_deep_collect_named_values(parsed, {"tabId", "tab_id", "activeTabId"}))
        except Exception:
            pass

    for value in candidates:
        try:
            return int(value)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Navigation result helpers
# ---------------------------------------------------------------------------


def _navigation_result_message(result: dict[str, Any]) -> str:
    messages = _deep_collect_named_values(result, {"message"})
    for payload in _iter_tool_result_payloads(result):
        messages.extend(_deep_collect_named_values(payload, {"message"}))
    return _first_non_empty_string(messages)


def _should_close_browser_tab_after_navigation(result: dict[str, Any]) -> bool:
    message = _navigation_result_message(result).lower()
    if "activated existing tab" in message or "already open" in message:
        return False
    return True


# ---------------------------------------------------------------------------
# Search result HTML parsing
# ---------------------------------------------------------------------------


class _SearchResultHTMLParser(HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []
        self._anchor_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if self._current_href:
            if normalized_tag not in self._VOID_TAGS:
                self._anchor_depth += 1
            return
        if normalized_tag != "a":
            return
        attr_map = {str(key or "").lower(): str(value or "") for key, value in attrs}
        href = _unwrap_search_result_url(attr_map.get("href", ""))
        if not _looks_like_public_search_result_url(href):
            return
        self._current_href = href
        self._current_text = []
        self._anchor_depth = 1

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._current_href:
            return
        self._anchor_depth -= 1
        if self._anchor_depth > 0:
            return
        title = _compact_whitespace(" ".join(self._current_text))
        if title:
            self.links.append({"title": title, "href": self._current_href})
        self._current_href = ""
        self._current_text = []
        self._anchor_depth = 0


def _search_url_for_query(query: str) -> str:
    encoded_query = quote_plus(str(query or "").strip())
    template = str(cfg("WEB_RESEARCH_BROWSER_SEARCH_URL_TEMPLATE", _DEFAULT_SEARCH_URL_TEMPLATE) or "").strip()
    if not template:
        template = _DEFAULT_SEARCH_URL_TEMPLATE
    if "{query}" in template:
        return template.format(query=encoded_query)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}q={encoded_query}"


def _unwrap_search_result_url(href: str) -> str:
    value = unescape(str(href or "").strip())
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    if not parsed.scheme and value.startswith("/"):
        parsed = urlparse(f"https://duckduckgo.com{value}")

    hostname = str(parsed.hostname or "").lower()
    if hostname.endswith("duckduckgo.com"):
        query = parse_qs(parsed.query)
        uddg = query.get("uddg") or query.get("u")
        if uddg:
            value = unquote(str(uddg[0] or "").strip())
            parsed = urlparse(value)

    if parsed.scheme.lower() not in _DEFAULT_ALLOWED_URL_SCHEMES:
        return ""
    return value


def _looks_like_public_search_result_url(url: str) -> bool:
    if not _is_safe_public_browser_url(url):
        return False
    hostname = str(urlparse(url).hostname or "").lower()
    blocked_hosts = (
        "duckduckgo.com",
        "www.duckduckgo.com",
        "help.duckduckgo.com",
        "google.com",
        "www.google.com",
        "bing.com",
        "www.bing.com",
    )
    return hostname not in blocked_hosts


def _normalize_mcp_search_entry(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(entry.get("title") or "").strip(),
        "body": str(entry.get("body") or entry.get("snippet") or entry.get("content_excerpt") or "").strip(),
        "href": str(entry.get("href") or entry.get("url") or "").strip(),
        "source_backend": "chrome_mcp",
    }


def _extract_search_entries_from_html(html_text: str, *, max_results: int) -> list[dict[str, str]]:
    parser = _SearchResultHTMLParser()
    with contextlib.suppress(Exception):
        parser.feed(str(html_text or ""))

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parser.links:
        href = _unwrap_search_result_url(link.get("href", ""))
        title = truncate_text(_compact_whitespace(link.get("title")), 180)
        if not href or not title or href in seen:
            continue
        seen.add(href)
        entries.append({
            "title": title,
            "body": "",
            "href": href,
            "source_backend": "chrome_mcp",
        })
        if len(entries) >= max_results:
            break
    return entries


def _extract_search_entries_from_text(text: str, *, max_results: int) -> list[dict[str, str]]:
    from ..fact_check import extract_urls
    lines = [_compact_whitespace(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        for raw_url in extract_urls(line):
            href = _unwrap_search_result_url(raw_url.strip("()[]{}<>。、，,"))
            if not href or href in seen or not _looks_like_public_search_result_url(href):
                continue
            title = ""
            for previous in reversed(lines[max(0, index - 3):index]):
                if not extract_urls(previous):
                    title = previous
                    break
            if not title:
                title = _compact_whitespace(line.replace(raw_url, " "))
            entries.append({
                "title": truncate_text(title or href, 180),
                "body": "",
                "href": href,
                "source_backend": "chrome_mcp",
            })
            seen.add(href)
            if len(entries) >= max_results:
                return entries
    return entries


def _extract_search_entries_from_tool_result(result: dict[str, Any], *, max_results: int) -> list[dict[str, str]]:
    structured = result.get("structuredContent")
    raw_results: Any = []
    if isinstance(structured, dict):
        raw_results = structured.get("results") or structured.get("items") or structured.get("entries") or []
    elif isinstance(structured, list):
        raw_results = structured

    entries: list[dict[str, str]] = []
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            entry = _normalize_mcp_search_entry(item)
            if entry["title"] or entry["body"] or entry["href"]:
                entries.append(entry)

    if not entries:
        for payload in _iter_tool_result_payloads(result):
            nested_values = _deep_collect_named_values(payload, {"results", "items", "entries"})
            for nested in nested_values:
                if not isinstance(nested, list):
                    continue
                for item in nested:
                    if isinstance(item, dict):
                        entry = _normalize_mcp_search_entry(item)
                        if entry["title"] or entry["body"] or entry["href"]:
                            entries.append(entry)
                if entries:
                    break
            if entries:
                break

    if not entries:
        text = _extract_text_from_tool_result(result)
        with contextlib.suppress(Exception):
            parsed = json_loads(text)
            if isinstance(parsed, dict):
                nested = parsed.get("results") or parsed.get("items") or []
                if isinstance(nested, list):
                    for item in nested:
                        if isinstance(item, dict):
                            entry = _normalize_mcp_search_entry(item)
                            if entry["title"] or entry["body"] or entry["href"]:
                                entries.append(entry)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        entry = _normalize_mcp_search_entry(item)
                        if entry["title"] or entry["body"] or entry["href"]:
                            entries.append(entry)

    if not entries:
        text = _extract_text_from_tool_result(result)
        if "<a" in text.lower() or "<html" in text.lower():
            entries.extend(_extract_search_entries_from_html(text, max_results=max_results))
        if not entries:
            entries.extend(_extract_search_entries_from_text(text, max_results=max_results))

    seen_urls: set[str] = set()
    deduped: list[dict[str, str]] = []
    for entry in entries:
        identity = entry["href"] or f"{entry['title']}\n{entry['body']}"
        if not identity or identity in seen_urls:
            continue
        seen_urls.add(identity)
        deduped.append(entry)
        if len(deduped) >= max_results:
            break
    return deduped


# ---------------------------------------------------------------------------
# MCP tool argument builders (used by client.py)
# ---------------------------------------------------------------------------


def _mcp_tool_schema_properties(tool_map: dict[str, dict[str, Any]], tool_name: str) -> dict[str, Any]:
    input_schema = tool_map.get(tool_name, {}).get("inputSchema")
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    return properties if isinstance(properties, dict) else {}


def _build_get_web_content_args(
    tool_map: dict[str, dict[str, Any]],
    *,
    url: str,
    tab_id: int | None,
    preferred_format: str,
    selector: str = "",
    should_use_background_fn=None,
) -> dict[str, Any]:
    properties = _mcp_tool_schema_properties(tool_map, "chrome_get_web_content")
    args: dict[str, Any] = {}
    if tab_id is not None and "tabId" in properties:
        args["tabId"] = tab_id
    elif "url" in properties:
        args["url"] = url
    if "format" in properties:
        args["format"] = preferred_format
    use_bg = False
    if should_use_background_fn:
        use_bg = should_use_background_fn(url)
    else:
        from .config import _should_use_background_browser_access
        use_bg = _should_use_background_browser_access(url)

    if "background" in properties and use_bg:
        args["background"] = True
    if "htmlContent" in properties:
        args["htmlContent"] = preferred_format == "html"
    if "textContent" in properties:
        args["textContent"] = preferred_format != "html"
    if selector and "selector" in properties:
        args["selector"] = selector
    for key, value in (
        ("includeText", True),
        ("extractText", True),
        ("markdown", preferred_format == "markdown"),
    ):
        if key in properties:
            args[key] = value
    return args


def _build_read_page_args(tool_map: dict[str, dict[str, Any]], *, tab_id: int | None) -> dict[str, Any]:
    properties = _mcp_tool_schema_properties(tool_map, "chrome_read_page")
    args: dict[str, Any] = {}
    if tab_id is not None and "tabId" in properties:
        args["tabId"] = tab_id
    return args


def _web_content_arg_variants(
    tool_map: dict[str, dict[str, Any]],
    *,
    url: str,
    tab_id: int | None,
    preferred_format: str,
    selector: str = "",
    should_use_background_fn=None,
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    default_args = _build_get_web_content_args(
        tool_map,
        url=url,
        tab_id=tab_id,
        preferred_format=preferred_format,
        selector=selector,
        should_use_background_fn=should_use_background_fn,
    )
    if default_args:
        variants.append(default_args)
    variants.append({})

    properties = _mcp_tool_schema_properties(tool_map, "chrome_get_web_content")
    use_bg = False
    if should_use_background_fn:
        use_bg = should_use_background_fn(url)
    else:
        from .config import _should_use_background_browser_access
        use_bg = _should_use_background_browser_access(url)
    if tab_id is not None:
        tab_args: dict[str, Any] = {"tabId": tab_id}
        if "format" in properties:
            tab_args["format"] = preferred_format
        if "background" in properties and use_bg:
            tab_args["background"] = True
        if "htmlContent" in properties:
            tab_args["htmlContent"] = preferred_format == "html"
        if "textContent" in properties:
            tab_args["textContent"] = preferred_format != "html"
        if selector and "selector" in properties:
            tab_args["selector"] = selector
        variants.append(tab_args)
    if "url" in properties:
        url_args: dict[str, Any] = {"url": url}
        if "format" in properties:
            url_args["format"] = preferred_format
        if "background" in properties and use_bg:
            url_args["background"] = True
        if "htmlContent" in properties:
            url_args["htmlContent"] = preferred_format == "html"
        if "textContent" in properties:
            url_args["textContent"] = preferred_format != "html"
        if selector and "selector" in properties:
            url_args["selector"] = selector
        variants.append(url_args)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for args in variants:
        key = tuple(sorted((str(k), repr(v)) for k, v in args.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(args)
    return deduped

