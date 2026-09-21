# Catalog top-thread fetching and Discord-facing catalog summary formatting.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import aiohttp

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")


from lib.html_utils import clean_html


async def fetch_img_top5_threads(limit: int = 5, timeout_sec: float = 15.0) -> list[dict[str, Any]]:
    catalog_url = "https://img.2chan.net/b/futaba.php?mode=cat&sort=6"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Referer": "https://img.2chan.net/b/futaba.htm"
    }

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. Fetch catalog
            async with session.get(catalog_url, headers=headers) as resp:
                resp.raise_for_status()
                # Futaba is Shift_JIS
                html_text = await resp.text(encoding='Shift_JIS', errors='ignore')
                
            # 2. Extract Top N threads from catalog
            # Look for: <a href='res/123456.htm' target='_blank'>...<font size=2>123</font></td>
            pattern = r"<a\s+href=['\"](res/\d+\.htm)['\"][^>]*>.*?<font\s+size=2>(\d+)</font></td>"
            matches = re.findall(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
            
            top_links: list[dict[str, Any]] = []
            for href, res_count in matches:
                # Deduplicate just in case
                full_url = f"https://img.2chan.net/b/{href}"
                if full_url not in [x["url"] for x in top_links]:
                    top_links.append({"url": full_url, "res_count": int(res_count)})
                if len(top_links) >= limit:
                    break
                    
            if not top_links:
                return []
                
            # 3. Fetch thread OP texts concurrently
            async def _fetch_thread(thread_info: dict) -> dict:
                try:
                    async with session.get(thread_info["url"], headers=headers) as t_resp:
                        if t_resp.status == 200:
                            t_html = await t_resp.text(encoding='Shift_JIS', errors='ignore')
                            # extract OP blockquote
                            op_match = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', t_html, re.IGNORECASE | re.DOTALL)
                            if op_match:
                                op_text = clean_html(op_match.group(1))
                                # Also grab title or first line if we can
                                first_line = op_text.split('\n')[0][:30]
                                thread_info["text"] = op_text
                                thread_info["title"] = f"{first_line}..." if len(op_text) > 30 else op_text
                            else:
                                thread_info["text"] = "(本文なし)"
                                thread_info["title"] = "(無題)"
                        else:
                            thread_info["text"] = "(取得失敗)"
                            thread_info["title"] = "(取得失敗)"
                except Exception:
                    thread_info["text"] = "(エラー)"
                    thread_info["title"] = "(エラー)"
                return thread_info

            tasks = [_fetch_thread(info) for info in top_links]
            results = await asyncio.gather(*tasks)
            return results
    except Exception:
        return []

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
