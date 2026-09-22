# URL detection, thread fetching, HTML parsing, summaries, and top-request detection.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")

from lib.html_utils import clean_html
from lib.img2chan_utils import (
    _IMG_THREAD_URL_PATTERN,
    default_browser_headers as _default_headers,
    normalize_img_thread_url,
)
_IMG_THREAD_POST_CONTEXT_LIMIT = 10


def is_img_thread_url(text: str) -> bool:
    """テキストに img.2chan.net/b/res/*.htm の形式のURLが含まれるか判定する。"""
    return bool(_IMG_THREAD_URL_PATTERN.search(str(text or "")))


def extract_img_thread_urls(text: str) -> list[str]:
    """テキストから img.2chan.net スレッドURLをすべて抽出して返す。"""
    return _extract_img_thread_urls_full(text)


def _extract_img_thread_urls_full(text: str) -> list[str]:
    """テキストから img.2chan.net スレッドURL（フルURL）をすべて抽出して返す。"""
    return [m.group(0) for m in _IMG_THREAD_URL_PATTERN.finditer(str(text or ""))]


def _html_class_attr_pattern(class_name: str) -> str:
    escaped = re.escape(class_name)
    return (
        rf'class\s*=\s*(?:'
        rf'"[^"]*\b{escaped}\b[^"]*"'
        rf"|'[^']*\b{escaped}\b[^']*'"
        rf"|{escaped}\b)"
    )


def parse_img_thread_html(
    url: str,
    html_text: str,
    *,
    max_replies: int = 30,
) -> dict[str, Any]:
    """ふたば IMG のスレHTMLを OP・レス一覧へ変換する。"""
    url = normalize_img_thread_url(url) or str(url or "").strip()
    thread_no_match = _IMG_THREAD_URL_PATTERN.search(url)
    thread_no = thread_no_match.group(1) if thread_no_match else ""

    result: dict[str, Any] = {
        "url": url,
        "thread_no": thread_no,
        "op_text": "",
        "replies": [],
        "total_reply_count": 0,
        "fetched_reply_count": 0,
        "error": None,
    }

    raw_html = str(html_text or "")

    # ---- OP 本文抽出 ----
    # OP は <div class="thre"> 内の最初の <blockquote>
    op_match = re.search(
        rf'<div[^>]+{_html_class_attr_pattern("thre")}[^>]*>.*?<blockquote[^>]*>(.*?)</blockquote>',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )
    if op_match:
        result["op_text"] = clean_html(op_match.group(1))
    else:
        # フォールバック: ページ全体の最初の blockquote
        fb = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', raw_html, re.IGNORECASE | re.DOTALL)
        if fb:
            result["op_text"] = clean_html(fb.group(1))

    op_no_match = re.search(
        rf'<div[^>]+{_html_class_attr_pattern("thre")}[^>]*>.*?<span[^>]+{_html_class_attr_pattern("cno")}[^>]*>\s*No\.(\d+)\s*</span>',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )
    if op_no_match:
        result["op_post_no"] = op_no_match.group(1)

    # ---- レス抽出 ----
    # 各レスは <td class="rtd"> 内にある
    # 表示番号: <span class="rsc">N</span>
    # 記事番号: <span class="cno">No.1422...</span>
    # そうだね: その td 内の <a ...>そうだねxN</a> またはテキスト内の「そうだね X」
    reply_blocks = re.findall(
        rf'<td[^>]+{_html_class_attr_pattern("rtd")}[^>]*>(.*?)</td>',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )

    replies: list[dict[str, Any]] = []
    for block in reply_blocks:
        # レス表示番号
        num_match = re.search(
            rf'<span[^>]+{_html_class_attr_pattern("rsc")}[^>]*>(\d+)</span>',
            block,
            re.IGNORECASE,
        )
        if not num_match:
            # フォールバック: <a name="N">
            num_match = re.search(r'<a[^>]+name=["\'](\d+)["\']', block, re.IGNORECASE)
        reply_no = int(num_match.group(1)) if num_match else 0

        post_no_match = re.search(
            rf'<span[^>]+{_html_class_attr_pattern("cno")}[^>]*>\s*No\.(\d+)\s*</span>',
            block,
            re.IGNORECASE,
        )
        post_no = post_no_match.group(1) if post_no_match else ""

        # そうだね数
        soudane_match = re.search(r'そうだね[^\d]*(\d+)', block)
        soudane = int(soudane_match.group(1)) if soudane_match else 0

        # 本文
        bq_match = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', block, re.IGNORECASE | re.DOTALL)
        reply_text = clean_html(bq_match.group(1)) if bq_match else ""

        if reply_text and reply_no:
            replies.append({
                "number": reply_no,
                "post_no": post_no,
                "text": reply_text,
                "soudane": soudane,
            })

    result["total_reply_count"] = len(replies)
    result["replies"] = replies[-max_replies:] if max_replies > 0 else replies
    result["fetched_reply_count"] = len(result["replies"])
    return result


async def fetch_img_thread(
    url: str,
    max_replies: int = 30,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    """ふたば☆ちゃんねる IMG の特定スレッドを取得し、OP・レスを解析して返す。

    Returns:
        {
            "url": str,
            "thread_no": str,         # スレッド番号
            "op_text": str,           # OP 本文
            "replies": [              # レス一覧
                {"number": int, "post_no": str, "text": str, "soudane": int},
                ...
            ],
            "total_reply_count": int, # 実際のレス総数
            "fetched_reply_count": int,
            "error": str | None,
        }
    """
    url = str(url or "").strip()
    thread_no_match = _IMG_THREAD_URL_PATTERN.search(url)
    thread_no = thread_no_match.group(1) if thread_no_match else ""

    headers = _default_headers()

    result: dict[str, Any] = {
        "url": url,
        "thread_no": thread_no,
        "op_text": "",
        "replies": [],
        "total_reply_count": 0,
        "fetched_reply_count": 0,
        "error": None,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    result["error"] = f"HTTP {resp.status}"
                    return result
                html = await resp.text(encoding="Shift_JIS", errors="ignore")

        result = parse_img_thread_html(url, html, max_replies=max_replies)

    except asyncio.TimeoutError:
        result["error"] = "タイムアウト"
    except Exception as e:
        result["error"] = str(e)

    return result


def format_img_thread_summary(
    thread: dict[str, Any],
    max_replies: int = 30,
) -> str:
    """fetch_img_thread() の結果を LLM に渡すテキスト形式に変換する。"""
    if not thread:
        return "スレッドの取得に失敗しました。"

    error = str(thread.get("error") or "").strip()
    if error:
        return f"スレッドの取得に失敗しました: {error}"

    url = str(thread.get("url") or "")
    op_text = str(thread.get("op_text") or "").strip()
    replies: list[dict[str, Any]] = list(thread.get("replies") or [])
    total = int(thread.get("total_reply_count") or len(replies))
    fetched = int(thread.get("fetched_reply_count") or len(replies))
    display_limit = max(int(max_replies or 0), 0)
    display_replies = replies[-display_limit:] if display_limit > 0 else replies
    displayed = len(display_replies)

    lines: list[str] = [
        "【ふたば☆ちゃんねる IMG スレッド】",
        f"URL: {url}",
        "",
        "--- OP ---",
        op_text or "(本文なし)",
        "",
    ]

    if display_replies:
        reply_header = "--- レス"
        if displayed < len(replies) or displayed < total or fetched < total:
            reply_header = "--- 直近レス"
        reply_header += f" ({total}件"
        if displayed < total:
            reply_header += f" / 表示 {displayed}件"
        reply_header += ") ---"
        lines.append(reply_header)

        for r in display_replies:
            no = r.get("number", 0)
            post_no = str(r.get("post_no") or "").strip()
            text = str(r.get("text") or "").strip()
            soudane = int(r.get("soudane") or 0)
            header = f">>{no}"
            if post_no:
                header += f" No.{post_no}"
            if soudane > 0:
                header += f" ★そうだね:{soudane}"
            lines.append(header)
            lines.append(text)
            lines.append("")
    else:
        lines.append("(レスなし)")

    return "\n".join(lines)


def _compact_img_thread_log_text(text: object, *, max_chars: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip()).strip()
    if len(value) > max_chars:
        value = value[:max_chars].rstrip() + "..."
    return value


def format_img_thread_log_summary(
    thread: dict[str, Any],
    *,
    max_replies: int = 5,
    max_chars: int = 1400,
) -> str:
    """ログ向けに、取得済み IMG スレの解析結果を短く整形する。"""
    if not thread:
        return "IMG thread parse: empty thread"

    error = str(thread.get("error") or "").strip()
    url = str(thread.get("url") or "").strip()
    thread_no = str(thread.get("thread_no") or "").strip()
    if error:
        return f"IMG thread parse failed url={url or '(unknown)'} thread_no={thread_no or '-'} error={error}"

    replies: list[dict[str, Any]] = list(thread.get("replies") or [])
    total = int(thread.get("total_reply_count") or len(replies))
    fetched = int(thread.get("fetched_reply_count") or len(replies))
    display_limit = max(int(max_replies or 0), 0)
    display_replies = replies[-display_limit:] if display_limit > 0 else []
    op_text = _compact_img_thread_log_text(thread.get("op_text"), max_chars=240) or "(本文なし)"

    lines = [
        f"IMG thread parse url={url or '(unknown)'} thread_no={thread_no or '-'} replies={total} fetched={fetched}",
        f"OP: {op_text}",
    ]
    if display_replies:
        lines.append(f"recent_replies({len(display_replies)}):")
        for reply in display_replies:
            number = int(reply.get("number") or 0)
            post_no = str(reply.get("post_no") or "").strip()
            soudane = int(reply.get("soudane") or 0)
            text = _compact_img_thread_log_text(reply.get("text"), max_chars=180) or "(本文なし)"
            header = f">>{number}" if number > 0 else ">>(番号不明)"
            if post_no:
                header += f" No.{post_no}"
            if soudane > 0:
                header += f" soudane={soudane}"
            lines.append(f"- {header}: {text}")
    else:
        lines.append("recent_replies: (レスなし)")

    summary = "\n".join(lines)
    limit = max(int(max_chars or 0), 300)
    if len(summary) > limit:
        summary = summary[:limit].rstrip() + "..."
    return summary

_IMG_TOP_KEYWORDS = (
    "今のimg", "今のいもげ", "今の二次裏", "いまのimg", "いまのいもげ", "いまの二次裏",
    "現在のimg", "現在のいもげ", "imgの現在", "いもげの現在",
    "imgの状況", "いもげの状況", "二次裏の状況",
    "imgの上位", "いもげの上位", "二次裏の上位",
    "勢い順", "勢いのあるスレ", "imgの勢い", "いもげの勢い",
    "img教えて", "いもげ教えて"
)

def is_img_top_request(text: str) -> bool:
    text_lower = text.lower()
    # Check direct keywords
    if any(kw in text_lower for kw in _IMG_TOP_KEYWORDS):
        return True
    
    # Check combined keywords
    has_img = any(k in text_lower for k in ("img", "いもげ", "二次裏", "ふたば"))
    has_top = any(k in text_lower for k in ("勢い", "上位", "トップ", "人気", "スレ", "状況", "どう", "教えて"))
    
    if has_img and has_top:
        return True
    return False
