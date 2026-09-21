"""画像スレッド（img_thread）の調査結果の整形と直接返信テキスト生成を担当するモジュール。"""
from __future__ import annotations

import re
from typing import Any

from ..config_helpers import cfg
from ..ollama_helpers import extract_first_user_facing_reply, _normalize_compare_text, truncate_text


def clean_img_thread_fragment(text: object, *, max_chars: int = 80) -> str:
    """スレッド内のテキスト断片をサニタイズして指定長に切り詰める。"""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"No\.\d+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" 　-・>「」\"'")
    if not value:
        return ""
    return truncate_text(value, max(max_chars, 20)).strip(" 　-・>「」\"'")


def build_img_thread_direct_reply(research_result: dict[str, Any] | None) -> str:
    """画像スレッド調査結果から直接返信用のテキストを生成する。"""
    result = dict(research_result or {})
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    op_text = clean_img_thread_fragment(
        (thread or {}).get("op_text") or str(result.get("summary") or ""),
        max_chars=90,
    )
    if not op_text:
        op_text = "本文なし"

    replies = list((thread or {}).get("replies") or [])
    ranked_replies = sorted(
        replies,
        key=lambda item: int(item.get("soudane") or 0) if isinstance(item, dict) else 0,
        reverse=True,
    )
    if not any(int(item.get("soudane") or 0) > 0 for item in ranked_replies if isinstance(item, dict)):
        ranked_replies = replies

    reply_fragments: list[str] = []
    seen: set[str] = set()
    for item in ranked_replies:
        if not isinstance(item, dict):
            continue
        fragment = clean_img_thread_fragment(item.get("text"), max_chars=64)
        if len(fragment) < 3:
            continue
        normalized = _normalize_compare_text(fragment)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        reply_fragments.append(f"「{fragment}」")
        if len(reply_fragments) >= 3:
            break

    reply_topics = "、".join(reply_fragments) if reply_fragments else "目立つレスはまだ少ない"
    total = int((thread or {}).get("total_reply_count") or 0)
    fetched = int((thread or {}).get("fetched_reply_count") or len(replies) or 0)
    count_text = f"{total}件" if total else f"{fetched}件"
    if total and fetched and fetched < total:
        count_text = f"{total}件中{fetched}件"

    template = str(
        cfg(
            "REPLY_RESEARCH_IMG_THREAD_DIRECT_TEMPLATE",
            "ざっと見たけど、スレタイは「{op}」。レスは{reply_topics}あたりが流れてて、確認できた範囲では{count}読めた。まとまった議論というより、内輪ネタと雑談寄りのスレだな。",
        )
        or ""
    ).strip()
    if not template:
        template = "ざっと見たけど、スレタイは「{op}」。レスは{reply_topics}あたりが流れてるスレだな。"
    try:
        reply = template.format(op=op_text, reply_topics=reply_topics, count=count_text)
    except Exception:
        reply = f"ざっと見たけど、スレタイは「{op_text}」。レスは{reply_topics}あたりが流れてるスレだな。"
    return truncate_text(extract_first_user_facing_reply(reply) or reply, 500)
