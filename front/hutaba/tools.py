"""*.2chan.net（ふたば☆ちゃんねる）調査ディスパッチ・ドメインヘルパーローダー。

`harness.agent.tool_registry.web_research.dispatch` のパッチ可能属性を動的に参照し、
既存のテストとの後方互換性を保ちます。
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from front.registry import load_domain_helper

log = logging.getLogger("ollama_bot.common.web_research")


def _dispatch_mod() -> Any:
    from harness.agent.tool_registry.web_research import dispatch

    return dispatch


class _MissingImgHelpers:
    @staticmethod
    def is_img_top_request(text: str) -> bool:
        return False


def _load_chrome_mcp_img_helpers(helper_path: Path | None = None) -> Any:
    if helper_path is None:
        try:
            return load_domain_helper("hutaba")
        except Exception:
            pass
        log.warning("harness Chrome MCP img helper not found; using built-in fallback")
        return _MissingImgHelpers

    helper_path = Path(helper_path)
    if not helper_path.is_file():
        log.warning("chrome-mcp img2channet.py not found at %s", helper_path)
        return _MissingImgHelpers
    spec = importlib.util.spec_from_file_location("discord_ai_bot_chrome_mcp_img", helper_path)
    if spec is None or spec.loader is None:
        log.warning("unable to load chrome-mcp img2channet.py")
        return _MissingImgHelpers
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        log.warning("chrome-mcp img2channet load failed: %r", e)
        return _MissingImgHelpers
    return module


async def img_top5_dispatch(user_text: str, context: str = "") -> dict[str, Any]:
    d = _dispatch_mod()
    try:
        if not hasattr(d._img_helper, "fetch_img_top5_threads"):
            raise ValueError("img2channet.py is not properly loaded or fetch_img_top5_threads not found.")
        threads = await getattr(d._img_helper, "fetch_img_top5_threads")(limit=5)
    except Exception as e:
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="IMGカタログの取得に失敗しました。",
            confidence=0.0,
            mode="img_top5",
            error=str(e),
        )

    summary = ""
    if hasattr(d._img_helper, "format_img_top5_summary"):
        summary = getattr(d._img_helper, "format_img_top5_summary")(threads)

    sources = [
        d._build_source(
            source_type="browser",
            title=f"IMG勢い上位{index}位",
            url=str(thread.get("url") or "").strip(),
            snippet=d.truncate_text(str(thread.get("text") or "").strip(), 240),
            content_excerpt=d.truncate_text(str(thread.get("text") or "").strip(), 500),
            content_text=str(thread.get("text") or "").strip(),
            used_in_answer=True,
            source_backend="img_top5",
            rank=index,
            res_count=int(thread.get("res_count") or 0),
        )
        for index, thread in enumerate(list(threads or [])[:5], start=1)
    ]
    result = d._build_result(
        used_web=True,
        used_browser=True,
        summary=summary,
        sources=sources,
        confidence=0.8 if threads else 0.0,
        mode="img_top5",
        query="https://img.2chan.net/b/futaba.php?mode=cat&sort=6",
    )
    result["threads"] = list(threads or [])[:5]
    return result


async def img_thread_dispatch(user_text: str, context: str = "") -> dict[str, Any]:
    d = _dispatch_mod()
    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()

    if not hasattr(d._img_helper, "_extract_img_thread_urls_full"):
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッド取得機能が利用できません。",
            confidence=0.0,
            mode="img_thread",
            error="img2channet._extract_img_thread_urls_full が見つかりません。",
        )

    thread_urls: list[str] = getattr(d._img_helper, "_extract_img_thread_urls_full")(combined)
    if not thread_urls:
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッドのURLが見つかりませんでした。",
            confidence=0.0,
            mode="img_thread",
            error="img.2chan.net スレッドURLが含まれていません。",
        )

    target_url = thread_urls[0]
    max_replies = int(d.cfg("IMG_THREAD_MAX_REPLIES", 30) or 30)
    timeout_sec = float(d.cfg("IMG_THREAD_TIMEOUT_SEC", 15.0) or 15.0)

    try:
        thread = await getattr(d._img_helper, "fetch_img_thread")(
            target_url, max_replies=max_replies, timeout_sec=timeout_sec
        )
    except Exception as e:
        d.log.warning("img_thread_dispatch fetch failed url=%s err=%r", target_url, e)
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary="IMGスレッドの取得中にエラーが発生しました。",
            confidence=0.0,
            mode="img_thread",
            query=target_url,
            error=str(e),
        )

    error = str(thread.get("error") or "").strip()
    if error:
        return d._build_result(
            used_web=False,
            used_browser=False,
            summary=f"IMGスレッドの取得に失敗しました: {error}",
            confidence=0.0,
            mode="img_thread",
            query=target_url,
            error=error,
        )

    summary = ""
    summary_max_replies = max(1, min(max_replies, d.cfg_int("IMG_THREAD_SUMMARY_MAX_REPLIES", 18)))
    if hasattr(d._img_helper, "format_img_thread_summary"):
        summary = getattr(d._img_helper, "format_img_thread_summary")(thread, max_replies=summary_max_replies)
    else:
        op_text = str(thread.get("op_text") or "").strip()
        summary = f"【スレッド】{target_url}\nOP: {op_text[:400]}"
    summary = d.truncate_text(summary, max(d.cfg_int("IMG_THREAD_SUMMARY_MAX_CHARS", 2400), 600))

    log_formatter = getattr(d._img_helper, "format_img_thread_log_summary", None)
    if callable(log_formatter):
        d.log.info(
            "img_thread_dispatch parsed thread\n%s",
            log_formatter(
                thread,
                max_replies=max(d.cfg_int("IMG_THREAD_LOG_MAX_REPLIES", 5), 0),
                max_chars=max(d.cfg_int("IMG_THREAD_LOG_MAX_CHARS", 1400), 300),
            ),
        )
    else:
        d.log.info(
            "img_thread_dispatch parsed url=%s thread_no=%s replies=%s fetched=%s op=%r",
            target_url,
            str(thread.get("thread_no") or ""),
            int(thread.get("total_reply_count") or 0),
            int(thread.get("fetched_reply_count") or 0),
            d.truncate_text(str(thread.get("op_text") or "").strip(), 160),
        )

    source = d._build_source(
        source_type="browser",
        title=f"IMGスレッド {thread.get('thread_no', '')}",
        url=target_url,
        snippet=str(thread.get("op_text") or "")[:200],
        content_text=summary,
        content_excerpt=summary[:500],
        used_in_answer=True,
        source_backend="img_thread",
        thread_no=str(thread.get("thread_no") or ""),
    )
    result = d._build_result(
        used_web=True,
        used_browser=True,
        summary=summary,
        sources=[source],
        confidence=0.85 if not error else 0.0,
        mode="img_thread",
        query=target_url,
    )
    result["thread"] = thread
    return result


__all__ = [
    "_MissingImgHelpers",
    "_load_chrome_mcp_img_helpers",
    "img_thread_dispatch",
    "img_top5_dispatch",
]
