# URL detection, thread fetching, HTML parsing, summaries, and top-request detection.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")

from lib.html_utils import clean_html
from front.hutaba.utils import _HUTABA_THREAD_URL_PATTERN, _IMG_THREAD_URL_PATTERN, default_browser_headers as _default_headers, normalize_img_thread_url
from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.http_session import get_shared_http_session
from ollama_bot.common.ollama_helpers import call_ollama, release_ollama_model
_IMG_THREAD_POST_CONTEXT_LIMIT = 10


def is_img_thread_url(text: str) -> bool:
    """テキストにふたば（img/may）スレッドURL（*.2chan.net/b/res/*.htm）が含まれるか判定する。"""
    return bool(_HUTABA_THREAD_URL_PATTERN.search(str(text or "")))


def extract_img_thread_urls(text: str) -> list[str]:
    """テキストからふたば（img/may）スレッドURLをすべて抽出して返す。"""
    return _extract_img_thread_urls_full(text)


def _extract_img_thread_urls_full(text: str) -> list[str]:
    """テキストからふたば（img/may）スレッドURL（フルURL）をすべて抽出して返す。"""
    return [m.group(0) for m in _HUTABA_THREAD_URL_PATTERN.finditer(str(text or ""))]


def _html_class_attr_pattern(class_name: str) -> str:
    escaped = re.escape(class_name)
    return (
        rf'class\s*=\s*(?:'
        rf'"[^"]*\b{escaped}\b[^"]*"'
        rf"|'[^']*\b{escaped}\b[^']*'"
        rf"|{escaped}\b)"
    )


_THREAD_IMAGE_CAPTION_CACHE: dict[str, str] = {}
_MAX_CAPTION_CACHE_ENTRIES = 128


def clear_thread_image_caption_cache() -> None:
    """スレ画キャプションキャッシュをクリアする（テスト用・保守用）。"""
    _THREAD_IMAGE_CAPTION_CACHE.clear()


async def caption_hutaba_thread_image(
    image_url: str,
    *,
    vision_model: str | None = None,
    timeout_sec: float | None = None,
) -> str:
    """ふたばのスレ画URLをダウンロードし、Qwen3-VL-8Bで客観的キャプションを生成する。
    完了後は必ず release_ollama_model で VRAM を即時解放する（BAN-170準拠）。
    同一画像URLに対する重複推論を防ぐため、インメモリLRUキャッシュ（最大128件）を保持する（BAN-94, BAN-170）。
    """
    if not image_url:
        return ""

    # キャッシュヒット時は推論・VRAMロードを完全にスキップ（LRU順を更新）
    if image_url in _THREAD_IMAGE_CAPTION_CACHE:
        log.debug("[hutaba] スレ画キャプションキャッシュヒット: url=%s", image_url)
        caption = _THREAD_IMAGE_CAPTION_CACHE.pop(image_url)
        _THREAD_IMAGE_CAPTION_CACHE[image_url] = caption
        return caption

    target_model = (vision_model or str(cfg("OLLAMA_VISION_MODEL", "qwen3-vl:8b") or "qwen3-vl:8b")).strip()
    describe_timeout = timeout_sec or float(cfg("OLLAMA_VISION_DESCRIBE_TIMEOUT_SEC", 60.0) or 60.0)
    num_ctx = int(cfg("OLLAMA_VISION_NUM_CTX", 4096) or 4096)

    # 1. 画像の非同期ダウンロード
    img_b64 = None
    try:
        session = await get_shared_http_session(timeout_sec=describe_timeout)
        m_host = re.match(r"https?://([^/]+)", image_url)
        ref_host = f"https://{m_host.group(1)}/b/" if m_host else "https://img.2chan.net/b/"
        headers = _default_headers(referer=ref_host)
        async with session.get(image_url, headers=headers, timeout=aiohttp.ClientTimeout(total=describe_timeout)) as resp:
            if resp.status == 200:
                data = await resp.read()
                if data and len(data) <= 5 * 1024 * 1024:
                    img_b64 = base64.b64encode(data).decode("ascii")
    except Exception as e:
        log.warning("[hutaba] Failed to download thread image url=%s: %r", image_url, e)
        return ""

    if not img_b64:
        return ""

    # 2. Visionモデルによるキャプション生成
    system_prompt = (
        "あなたは画像掲示板の画像分析アシスタントです。\n"
        "提供された画像（スレッド添付画像）の内容を、客観的かつ具体的に日本語で短く説明してください。\n"
        "【出力要件】\n"
        "1. 描かれているキャラクター、人物、物体、情景を簡潔に描写してください。\n"
        "2. 画像内に文字（セリフ、見出し、テロップ等）があれば書き起こしてください。\n"
        "3. 前置きや挨拶、主観的な感想は不要です。説明文のみを出力してください。"
    )
    user_prompt = "このスレ画に何が写っているか客観的に説明してください。文字があれば書き起こしてください。"

    caption = ""
    try:
        ollama_resp = await call_ollama(
            user_prompt,
            system_prompt=system_prompt,
            think=False,
            model=target_model,
            images=[img_b64],
            temperature=float(cfg("OLLAMA_VISION_TEMPERATURE", 0.1) or 0.1),
            num_predict=int(cfg("OLLAMA_VISION_NUM_PREDICT", 256) or 256),
            num_ctx=num_ctx,
            timeout_sec=describe_timeout,
            retries=1,
        )
        caption = (ollama_resp or "").strip()
    except Exception as e:
        log.warning("[hutaba] Vision captioning failed for url=%s: %r", image_url, e)
    finally:
        # VRAM即時解放保証（BAN-170）
        try:
            await release_ollama_model(target_model, timeout_sec=5.0)
        except Exception as e:
            log.debug("[hutaba] release_ollama_model failed after vision: %r", e)

    if caption:
        if len(_THREAD_IMAGE_CAPTION_CACHE) >= _MAX_CAPTION_CACHE_ENTRIES:
            _THREAD_IMAGE_CAPTION_CACHE.pop(next(iter(_THREAD_IMAGE_CAPTION_CACHE)))
        _THREAD_IMAGE_CAPTION_CACHE[image_url] = caption

    return caption


_HUTABA_JST = ZoneInfo("Asia/Tokyo")
_HUTABA_OLD_THREAD_WARNING_RE = re.compile(
    r"このスレは古いので[、,\s]*もうすぐ消えます",
    re.IGNORECASE,
)
_HUTABA_EXPIRE_TIME_RE = re.compile(
    r"(?:(?P<month>\d{1,2})月)?(?:(?P<day>\d{1,2})日)?\s*(?P<hour>\d{1,2})(?:時|:)(?P<minute>\d{1,2})分?頃消えます"
)


def parse_hutaba_thread_expiration(
    html_text: str,
    *,
    now: datetime | None = None,
    min_remaining_sec: float = 600.0,
) -> dict[str, Any]:
    """ふたばスレッドHTMLから消滅予定時刻と残り秒数・消滅間近フラグを抽出する。"""
    raw_html = str(html_text or "")
    plain_text = re.sub(r"<[^>]+>", " ", raw_html)
    has_old_warning = bool(_HUTABA_OLD_THREAD_WARNING_RE.search(plain_text))

    ref_now = now
    if ref_now is None:
        ref_now = datetime.now(_HUTABA_JST)
    elif ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=_HUTABA_JST)
    else:
        ref_now = ref_now.astimezone(_HUTABA_JST)

    expire_text = ""
    remaining_sec: float | None = None

    match = _HUTABA_EXPIRE_TIME_RE.search(plain_text)
    if match:
        expire_text = match.group(0).strip()
        with contextlib.suppress(ValueError):
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            month_str = match.group("month")
            day_str = match.group("day")
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                if day_str:
                    month = int(month_str) if month_str else ref_now.month
                    day = int(day_str)
                    expire_dt = ref_now.replace(
                        month=month,
                        day=day,
                        hour=hour,
                        minute=minute,
                        second=0,
                        microsecond=0,
                    )
                    if month_str and (expire_dt - ref_now).total_seconds() < -180 * 86400:
                        expire_dt = expire_dt.replace(year=ref_now.year + 1)
                else:
                    expire_dt = ref_now.replace(
                        hour=hour,
                        minute=minute,
                        second=0,
                        microsecond=0,
                    )
                    diff_sec = (expire_dt - ref_now).total_seconds()
                    # 現在時刻より6時間以上前なら翌日への日付またぎとみなす
                    if diff_sec < -6 * 3600:
                        expire_dt += timedelta(days=1)
                    # 現在時刻より18時間以上先なら前日分の未削除スレッドとみなす
                    elif diff_sec > 18 * 3600:
                        expire_dt -= timedelta(days=1)
                remaining_sec = round((expire_dt - ref_now).total_seconds(), 1)

    threshold = float(min_remaining_sec)
    is_expiring_soon = bool(
        has_old_warning
        or (remaining_sec is not None and remaining_sec <= threshold)
    )
    return {
        "expire_text": expire_text,
        "remaining_sec": remaining_sec,
        "has_old_warning": has_old_warning,
        "is_expiring_soon": is_expiring_soon,
    }


def parse_img_thread_html(
    url: str,
    html_text: str,
    *,
    max_replies: int = 30,
) -> dict[str, Any]:
    """ふたば IMG のスレHTMLを OP・レス一覧へ変換する。"""
    url = normalize_img_thread_url(url) or str(url or "").strip()
    thread_no_match = _HUTABA_THREAD_URL_PATTERN.search(url)
    thread_no = thread_no_match.group(2) if thread_no_match else ""

    result: dict[str, Any] = {
        "url": url,
        "board": "may" if "may.2chan.net" in url else "img",
        "thread_no": thread_no,
        "op_post_no": thread_no,
        "op_text": "",
        "op_image_url": "",
        "op_thumb_url": "",
        "op_image_caption": "",
        "replies": [],
        "early_replies": [],
        "all_replies": [],
        "total_reply_count": 0,
        "fetched_reply_count": 0,
        "expire_text": "",
        "remaining_sec": None,
        "has_old_warning": False,
        "is_expiring_soon": False,
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

    # ---- OP 画像抽出 ----
    img_match = re.search(
        r'<a[^>]+href=["\']((?:https?://[^/]+)?/b/src/\d+\.\w+)["\'][^>]*>.*?<img[^>]+src=["\']((?:https?://[^/]+)?/b/thumb/\d+s?\.\w+)["\']',
        raw_html,
        re.IGNORECASE | re.DOTALL,
    )
    if img_match:
        full_href = img_match.group(1)
        thumb_src = img_match.group(2)
        base_host = f"https://{m.group(1)}" if (m := re.match(r"https?://([^/]+)", url)) else "https://img.2chan.net"
        if not full_href.startswith("http"):
            full_href = base_host + (full_href if full_href.startswith("/") else "/" + full_href)
        if not thumb_src.startswith("http"):
            thumb_src = base_host + (thumb_src if thumb_src.startswith("/") else "/" + thumb_src)
        result["op_image_url"] = full_href
        result["op_thumb_url"] = thumb_src
    else:
        thumb_fb = re.search(r'<img[^>]+src=["\']((?:https?://[^/]+)?/b/thumb/\d+s?\.\w+)["\']', raw_html, re.IGNORECASE)
        if thumb_fb:
            src = thumb_fb.group(1)
            base_host = f"https://{m.group(1)}" if (m := re.match(r"https?://([^/]+)", url)) else "https://img.2chan.net"
            result["op_thumb_url"] = src if src.startswith("http") else base_host + (src if src.startswith("/") else "/" + src)

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
    result["all_replies"] = list(replies)  # 全件レスを常に保持（中盤ドロップ防止）
    if max_replies > 0 and len(replies) > max_replies:
        cutoff = len(replies) - max_replies
        result["early_replies"] = replies[: min(30, cutoff)]
        result["replies"] = replies[-max_replies:]
    else:
        result["early_replies"] = []
        result["replies"] = list(replies)
    result["fetched_reply_count"] = len(result["replies"])

    exp_info = parse_hutaba_thread_expiration(raw_html)
    result["expire_text"] = exp_info["expire_text"]
    result["remaining_sec"] = exp_info["remaining_sec"]
    result["has_old_warning"] = exp_info["has_old_warning"]
    result["is_expiring_soon"] = exp_info["is_expiring_soon"]
    return result


async def fetch_img_thread(
    url: str,
    max_replies: int = 30,
    timeout_sec: float = 15.0,
    cached_caption: str | None = None,
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
    thread_no_match = _HUTABA_THREAD_URL_PATTERN.search(url)
    thread_no = thread_no_match.group(2) if thread_no_match else ""

    headers = _default_headers()

    result: dict[str, Any] = {
        "url": url,
        "board": "may" if "may.2chan.net" in url else "img",
        "thread_no": thread_no,
        "op_post_no": thread_no,
        "op_text": "",
        "op_image_url": "",
        "op_thumb_url": "",
        "op_image_caption": "",
        "replies": [],
        "early_replies": [],
        "all_replies": [],
        "total_reply_count": 0,
        "fetched_reply_count": 0,
        "expire_text": "",
        "remaining_sec": None,
        "has_old_warning": False,
        "is_expiring_soon": False,
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

        # スレ画キャプションの自動取得（サムネイル優先で取得）
        pipeline_mode = str(cfg("VISION_PIPELINE_MODE", "caption_then_main") or "").strip().lower()
        thumb_or_img_url = result.get("op_thumb_url") or result.get("op_image_url")
        if thumb_or_img_url and pipeline_mode != "direct":
            if cached_caption:
                result["op_image_caption"] = cached_caption
                # LRUキャッシュにも記憶
                if thumb_or_img_url not in _THREAD_IMAGE_CAPTION_CACHE:
                    if len(_THREAD_IMAGE_CAPTION_CACHE) >= _MAX_CAPTION_CACHE_ENTRIES:
                        _THREAD_IMAGE_CAPTION_CACHE.pop(next(iter(_THREAD_IMAGE_CAPTION_CACHE)))
                    _THREAD_IMAGE_CAPTION_CACHE[thumb_or_img_url] = cached_caption
                else:
                    _THREAD_IMAGE_CAPTION_CACHE[thumb_or_img_url] = cached_caption
            else:
                result["op_image_caption"] = await caption_hutaba_thread_image(thumb_or_img_url)

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
    op_caption = str(thread.get("op_image_caption") or "").strip()
    op_line = op_text or "(本文なし)"
    if op_caption:
        op_line += f" (スレ画: {op_caption})"
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
        op_line,
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
