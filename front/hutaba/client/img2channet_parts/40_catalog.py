# Catalog top-thread fetching and Discord-facing catalog summary formatting.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    def parse_hutaba_thread_expiration(
        html_text: str,
        *,
        now: Any = None,
        min_remaining_sec: float = 600.0,
    ) -> dict[str, Any]: ...

import aiohttp

log = logging.getLogger("front.hutaba")

from lib.html_utils import clean_html
from front.hutaba.utils import default_browser_headers


def _decode_img_html(raw_bytes: bytes, content_type: str | None = None) -> str:
    """ふたばHTMLのデコードヘルパー（主にShift_JIS/CP932）。"""
    for enc in ("cp932", "shift_jis", "utf-8", "euc-jp"):
        try:
            return raw_bytes.decode(enc)
        except Exception:
            continue
    return raw_bytes.decode("cp932", errors="replace")


async def fetch_img_top5_threads(
    limit: int = 5,
    timeout_sec: float = 15.0,
    *,
    server: str = "img",
) -> list[dict[str, Any]]:
    """
    img.2chan.net または may.2chan.net のカタログ(sort=6: 勢い順)を取得し、
    上位スレッドのOP・最新レス・消滅予定時刻情報を抽出して返す。
    """
    srv = "may" if str(server or "").strip().lower() in ("may", "may.2chan.net") or "may.2chan.net" in str(server or "").lower() else "img"
    base_url = f"https://{srv}.2chan.net/b"
    catalog_url = f"{base_url}/futaba.php?mode=cat&sort=6"
    headers = default_browser_headers()
    headers["Referer"] = f"{base_url}/futaba.htm"

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
        try:
            async with session.get(catalog_url) as resp:
                if resp.status != 200:
                    return []
                raw_bytes = await resp.read()
                html_text = _decode_img_html(raw_bytes, resp.headers.get("Content-Type"))
        except Exception as e:
            log.warning("[hutaba] catalog fetch failed (%s): %s", srv, e)
            return []

        pattern = re.compile(
            r"<a\s+href=['\"]?(res/\d+\.htm)['\"]?[^>]*>(.*?)</a>",
            re.IGNORECASE | re.DOTALL,
        )
        matches = pattern.findall(html_text)

        thread_links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href, inner in matches:
            if href in seen:
                continue
            seen.add(href)
            cat_title = re.sub(r"<[^>]+>", "", inner).strip()
            cat_title = html.unescape(cat_title)
            thread_links.append((href, cat_title))
            if len(thread_links) >= limit:
                break

        if not thread_links:
            return []

        sem = asyncio.Semaphore(5)

        async def _fetch_thread(idx: int, href: str, title: str) -> dict[str, Any] | None:
            full_url = f"{base_url}/{href}" if not href.startswith("http") else href
            async with sem:
                try:
                    async with session.get(full_url) as t_resp:
                        if t_resp.status != 200:
                            return None
                        t_bytes = await t_resp.read()
                        t_html = _decode_img_html(t_bytes, t_resp.headers.get("Content-Type"))

                        exp_info = parse_hutaba_thread_expiration(t_html)

                        bq_pattern = re.compile(r"<blockquote[^>]*>(.*?)</blockquote>", re.IGNORECASE | re.DOTALL)
                        bqs = bq_pattern.findall(t_html)

                        blocks: list[str] = []
                        for b in bqs:
                            b_text = re.sub(r"<br\s*/?>", "\n", b, flags=re.IGNORECASE)
                            b_text = re.sub(r"<[^>]+>", "", b_text).strip()
                            b_text = html.unescape(b_text)
                            b_text = re.sub(r"\n{3,}", "\n\n", b_text)
                            if b_text:
                                blocks.append(b_text)

                        if not blocks:
                            return None

                        op_text = blocks[0]
                        recent_replies = [r[:150] for r in blocks[1:][-3:]]
                        reply_count = max(0, len(blocks) - 1)

                        return {
                            "rank": idx + 1,
                            "board": srv,
                            "url": full_url,
                            "title": title or (op_text.split("\n")[0][:30] if op_text else "(無題)"),
                            "text": op_text[:300],
                            "replies": recent_replies,
                            "reply_count": reply_count,
                            "res_count": reply_count,
                            "expire_text": exp_info["expire_text"],
                            "remaining_sec": exp_info["remaining_sec"],
                            "has_old_warning": exp_info["has_old_warning"],
                            "is_expiring_soon": exp_info["is_expiring_soon"],
                        }
                except Exception as e:
                    log.debug("[hutaba] thread fetch failed (%s): %s", full_url, e)
                    return None

        tasks = [
            _fetch_thread(idx, href, title)
            for idx, (href, title) in enumerate(thread_links)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        threads_data: list[dict[str, Any]] = []
        for res in results:
            if isinstance(res, dict):
                threads_data.append(res)

        threads_data.sort(key=lambda x: int(x.get("rank", 999)))
        return threads_data

def format_img_top5_summary(threads: list[dict[str, Any]]) -> str:
    if not threads:
        return "IMG（二次裏）のカタログからスレッドを取得できませんでした。サーバーが重いか、アクセスできない可能性があります。"
        
    lines = ["現在のIMG（二次裏）勢い順上位スレッド："]
    for i, t in enumerate(threads, 1):
        lines.append(f"【{i}位】レス数:{t.get('res_count', 0)}")
        lines.append(f"URL: {t.get('url', '')}")
        text = str(t.get("text", "")).strip()
        lines.append(f"本文:\n{text}\n")
        
    return "\n".join(lines)
