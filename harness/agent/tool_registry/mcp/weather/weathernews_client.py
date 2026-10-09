"""ウェザーニュース (Weathernews: https://weathernews.jp) データ取得クライアント

公式サイト: https://weathernews.jp
最新の気象トピックス、天気概況ニュース速報、地域気象解説を取得する。
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .types import WNITopicItem

log = logging.getLogger("harness.agent.tool_registry.mcp.weather.wni")

WNI_BASE_URL = "https://weathernews.jp"
WNI_TOPICS_URL = "https://weathernews.jp/s/topics/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _SimpleHTMLTopicExtractor(HTMLParser):
    """軽量HTMLパーサー（HTMLからトピックリンクとタイトル、テキストを抽出）"""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.topics: list[WNITopicItem] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._in_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for attr_name, attr_val in attrs:
                if attr_name == "href" and attr_val:
                    if "/s/topics/" in attr_val or "/topics/" in attr_val:
                        self._current_href = urljoin(self.base_url, attr_val)
                        self._in_anchor = True
                        self._current_text = []
                        break

    def handle_data(self, data: str) -> None:
        if self._in_anchor and data.strip():
            self._current_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_anchor:
            title = " ".join(self._current_text).strip()
            if self._current_href and title and len(title) >= 6:
                # 重複チェック
                if not any(t.url == self._current_href or t.title == title for t in self.topics):
                    self.topics.append(
                        WNITopicItem(
                            title=title,
                            summary="",
                            published_at="",
                            url=self._current_href,
                        )
                    )
            self._in_anchor = False
            self._current_href = None
            self._current_text = []


class WeathernewsClient:
    """ウェザーニュース (Weathernews) 連携クライアント"""

    def __init__(self, timeout_sec: float = 12.0) -> None:
        self.timeout_sec = timeout_sec

    async def _fetch_html(self, url: str) -> str | None:
        """指定URLのHTMLを安全に取得する"""
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("[tool_registry] WNI fetch failed url=%s status=%d", url, resp.status)
                        return None
                    return await resp.text()
        except Exception as e:
            log.warning("[tool_registry] WNI fetch error url=%s: %s", url, e)
            return None

    async def get_latest_topics(self, limit: int = 5) -> list[WNITopicItem]:
        """最新の気象トピックス・ニュース速報一覧を取得する"""
        log.info("[tool_registry] Fetching Weathernews topics limit=%d", limit)
        html = await self._fetch_html(WNI_TOPICS_URL)
        if not html:
            # トップページからフォールバック取得
            html = await self._fetch_html(WNI_BASE_URL)

        topics: list[WNITopicItem] = []
        if html:
            try:
                parser = _SimpleHTMLTopicExtractor(WNI_BASE_URL)
                parser.feed(html)
                topics = parser.topics
            except Exception as e:
                log.warning("[tool_registry] WNI HTML parse error: %s", e)

        # 正規表現による追加補完（もしHTMLParserで取れなかった場合）
        if not topics and html:
            pattern = re.compile(r'<a[^>]+href=["\'](/s/topics/\d+/[^"\']*)["\'][^>]*>(.*?)</a>', re.DOTALL)
            for match in pattern.finditer(html):
                path, raw_text = match.groups()
                clean_title = re.sub(r"<[^>]+>", "", raw_text).strip()
                if clean_title and len(clean_title) >= 6:
                    full_url = urljoin(WNI_BASE_URL, path)
                    if not any(t.url == full_url for t in topics):
                        topics.append(WNITopicItem(title=clean_title, url=full_url))

        # 取得できた記事を整形
        results = topics[: max(1, limit)]

        # 取得できなかった場合の安全なフォールバック
        if not results:
            results = [
                WNITopicItem(
                    title="全国の気象トピックス速報 (Weathernews)",
                    summary="最新の雨雲の動きや台風・低気圧情報についてはウェザーニュース公式をご参照ください。",
                    url=WNI_TOPICS_URL,
                )
            ]

        return results

    async def get_area_weather_summary(self, area: str) -> str:
        """ウェザーニュースに基づく指定地域の気象解説テキストを取得する"""
        log.info("[tool_registry] Fetching Weathernews area summary for %s", area)
        # トピックス記事から該当地域に関連するトピックを検索
        topics = await self.get_latest_topics(limit=10)
        matched = [t for t in topics if area in t.title or (t.summary and area in t.summary)]

        lines = [
            f"【ウェザーニュース (https://weathernews.jp) 気象解説】",
            f"対象地域: {area}",
        ]

        if matched:
            lines.append("■ 関連する気象トピックス:")
            for m in matched[:3]:
                lines.append(f"- {m.title} ({m.url})")
        else:
            lines.append("■ 全国の主要トピックス:")
            for t in topics[:3]:
                lines.append(f"- {t.title} ({t.url})")

        return "\n".join(lines)
