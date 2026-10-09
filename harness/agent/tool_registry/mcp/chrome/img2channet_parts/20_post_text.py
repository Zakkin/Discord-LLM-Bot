# Post sanitizing, quote composition, reply tracing, and LLM prompt builders.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import Any, Optional, TYPE_CHECKING

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")

if TYPE_CHECKING:
    def format_img_thread_summary(thread: dict[str, Any], *, max_replies: int = 10) -> str: ...

_IMG_POST_CONTROL_LABELS = (
    "返答前の内省メモ",
    "内省メモ",
    "感情の軸",
    "感情",
    "返答方針",
    "避けること",
    "相手の意図",
    "会話の背景",
    "思考",
    "推論",
    "analysis",
    "reasoning",
)
_IMG_POST_REPLY_LABELS = (
    "投稿本文",
    "書き込み",
    "コメント",
    "comment",
    "reply",
    "answer",
    "final answer",
    "返答",
    "回答",
    "最終返答",
    "最終回答",
    "ユーザー向け返答",
    "ユーザー向け回答",
)
_IMG_THREAD_POST_CONTEXT_LIMIT = 10



def _strip_img_post_think_blocks(text: str) -> str:
    return re.sub(r"<think\b[^>]*>.*?</think>", "", str(text or ""), flags=re.IGNORECASE | re.DOTALL)


def _looks_like_img_post_control_line(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if re.match(r"^(?:user|assistant|system)\s*[:：]", normalized, flags=re.IGNORECASE):
        return True
    if re.match(r"^[-*・]\s*(?:[A-Za-z_][A-Za-z0-9_ ]{0,32}|[ぁ-んァ-ヶ一-龠A-Za-z_][^:：]{0,32})\s*[:：]", normalized):
        label = re.sub(r"^[-*・]\s*", "", normalized).split(":", 1)[0].split("：", 1)[0].strip()
        return any(token.lower() in label.lower() for token in _IMG_POST_CONTROL_LABELS)
    if re.match(
        rf"^(?:{'|'.join(map(re.escape, _IMG_POST_CONTROL_LABELS))})\s*[:：]",
        normalized,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(
        r"^[【\[][^】\]]*(?:返答前の内省メモ|内省メモ|感情の軸|返答方針|思考|推論|analysis|reasoning)[^】\]]*[】\]]$",
        normalized,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _iter_preserved_urls(values: object) -> list[object]:
    if values is None:
        return []
    if isinstance(values, (list, tuple, set)):
        return list(values)
    return [values]


def _normalize_preserved_url(url: object) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if re.match(r"^discord\.gg/[A-Za-z0-9_-]+$", value, flags=re.IGNORECASE):
        return f"https://{value}"
    return value


def _protect_preserved_urls(
    text: object,
    *,
    preserve_urls: object = None,
) -> tuple[str, dict[str, str]]:
    protected = str(text or "")
    replacements: dict[str, str] = {}
    placeholder_index = 0

    for raw_url in _iter_preserved_urls(preserve_urls):
        normalized_url = _normalize_preserved_url(raw_url)
        if not normalized_url:
            continue
        variants = [normalized_url]
        if normalized_url.startswith("https://"):
            variants.append(normalized_url.removeprefix("https://"))
        elif normalized_url.startswith("http://"):
            variants.append(normalized_url.removeprefix("http://"))

        for variant in variants:
            if not variant or variant not in protected:
                continue
            placeholder = f"__IMG_PRESERVED_URL_{placeholder_index}__"
            protected = protected.replace(variant, placeholder)
            replacements[placeholder] = normalized_url
            placeholder_index += 1

    return protected, replacements


def _restore_preserved_urls(text: object, replacements: dict[str, str]) -> str:
    restored = str(text or "")
    for placeholder, url in replacements.items():
        restored = restored.replace(placeholder, url)
    return restored


def sanitize_img_post_text(
    text: object,
    *,
    max_chars: int = 220,
    preserve_urls: object = None,
) -> str:
    """IMGへ投げる投稿本文を、短く・本文だけに整える。"""
    raw = _strip_img_post_think_blocks(str(text or "").strip())
    raw, preserved_urls = _protect_preserved_urls(raw, preserve_urls=preserve_urls)
    raw = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"https?://\S+", "", raw).strip()
    raw = re.sub(r"<@!?[0-9]+>|<#[0-9]+>|@everyone|@here", "", raw).strip()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _looks_like_img_post_control_line(line):
            continue
        line = re.sub(
            rf"^(?:{'|'.join(map(re.escape, _IMG_POST_REPLY_LABELS))})\s*[:：]\s*",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        line = re.sub(r"^\s*[-*・]\s*", "", line).strip()
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    # ふたばの空気に合わせ、長い説明や複数段落は切る。
    cleaned = "\n".join(cleaned_lines[:3]).strip()
    cleaned = cleaned.strip("「」\"'")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    cleaned = _restore_preserved_urls(cleaned, preserved_urls)
    return cleaned


def sanitize_img_post_submission_text(
    text: object,
    *,
    max_chars: int = 340,
    preserve_urls: object = None,
) -> str:
    """投稿直前の本文を整え、引用行は保持したまま送信できる形にする。"""
    raw = _strip_img_post_think_blocks(str(text or "").strip())
    raw, preserved_urls = _protect_preserved_urls(raw, preserve_urls=preserve_urls)
    raw = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"https?://\S+", "", raw).strip()
    raw = re.sub(r"<@!?[0-9]+>|<#[0-9]+>|@everyone|@here", "", raw).strip()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    cleaned_lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _looks_like_img_post_control_line(line):
            continue
        if line.startswith(">"):
            quote_body = re.sub(r"\s+", " ", line.lstrip(">").strip())
            if quote_body:
                cleaned_lines.append(f">{quote_body}")
            continue
        line = re.sub(
            rf"^(?:{'|'.join(map(re.escape, _IMG_POST_REPLY_LABELS))})\s*[:：]\s*",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        line = re.sub(r"^\s*[-*・]\s*", "", line).strip()
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    if cleaned and not cleaned.startswith(">"):
        cleaned = cleaned.strip("「」\"'")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    cleaned = _restore_preserved_urls(cleaned, preserved_urls)
    return cleaned


def append_img_post_promotion(
    comment: object,
    promo_text: object,
    invite_url: object,
    *,
    max_chars: int = 240,
) -> str:
    """既存レスの末尾に宣伝文と招待URLを追加し、URLを保持したまま収める。"""
    normalized_invite_url = _normalize_preserved_url(invite_url)
    if not normalized_invite_url:
        return sanitize_img_post_submission_text(comment, max_chars=max_chars)

    preserve_urls = [normalized_invite_url]
    base_comment = sanitize_img_post_submission_text(
        comment,
        max_chars=max_chars,
        preserve_urls=preserve_urls,
    )
    promo_line = sanitize_img_post_text(
        promo_text,
        max_chars=max_chars,
        preserve_urls=preserve_urls,
    )
    promo_line = re.sub(r"\s+", " ", promo_line.replace("\n", " ")).strip()
    tail_parts = [part for part in (promo_line, normalized_invite_url) if part]
    if not tail_parts:
        return base_comment
    tail = "\n".join(tail_parts)
    if not base_comment:
        return sanitize_img_post_submission_text(
            tail,
            max_chars=max_chars,
            preserve_urls=preserve_urls,
        )

    remaining_for_base = max(max_chars - len(tail) - 1, 0)
    if remaining_for_base <= 0:
        return sanitize_img_post_submission_text(
            tail,
            max_chars=max_chars,
            preserve_urls=preserve_urls,
        )
    trimmed_base = sanitize_img_post_submission_text(
        base_comment,
        max_chars=remaining_for_base,
        preserve_urls=preserve_urls,
    )
    combined = "\n".join(part for part in (trimmed_base, tail) if part)
    return sanitize_img_post_submission_text(
        combined,
        max_chars=max_chars,
        preserve_urls=preserve_urls,
    )


def _iter_img_post_targets(values: object) -> list[object]:
    if values is None:
        return []
    if isinstance(values, (list, tuple, set)):
        return list(values)
    return [values]


def _normalize_img_post_no(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"No\.(\d+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"(?<!\d)(\d{6,})(?!\d)", text)
    return match.group(1) if match else ""


def _normalize_img_reply_no(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r">>\s*(\d+)", text)
    if not match:
        match = re.search(r"(?<!\d)(\d{1,4})(?!\d)", text)
    if not match:
        return 0
    with contextlib.suppress(Exception):
        reply_no = int(match.group(1))
        if reply_no > 0:
            return reply_no
    return 0


def _find_img_thread_post(
    thread: dict[str, Any],
    *,
    post_no: str = "",
    reply_no: int = 0,
) -> dict[str, Any] | None:
    normalized_post_no = _normalize_img_post_no(post_no)
    op_post_no = _normalize_img_post_no(thread.get("op_post_no"))
    if normalized_post_no and op_post_no and normalized_post_no == op_post_no:
        return {
            "post_no": op_post_no,
            "number": 0,
            "text": str(thread.get("op_text") or "").strip(),
            "is_op": True,
        }

    for reply in list(thread.get("replies") or []):
        if normalized_post_no and _normalize_img_post_no(reply.get("post_no")) == normalized_post_no:
            return {
                "post_no": normalized_post_no,
                "number": int(reply.get("number") or 0),
                "text": str(reply.get("text") or "").strip(),
                "is_op": False,
            }
        if reply_no and int(reply.get("number") or 0) == reply_no:
            return {
                "post_no": _normalize_img_post_no(reply.get("post_no")),
                "number": reply_no,
                "text": str(reply.get("text") or "").strip(),
                "is_op": False,
            }
    return None


def _iter_img_thread_posts(thread: dict[str, Any]) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    op_post_no = _normalize_img_post_no(thread.get("op_post_no"))
    op_text = str(thread.get("op_text") or "").strip()
    if op_post_no or op_text:
        posts.append({
            "post_no": op_post_no,
            "number": 0,
            "text": op_text,
            "is_op": True,
        })
    for reply in list(thread.get("replies") or []):
        posts.append({
            "post_no": _normalize_img_post_no(reply.get("post_no")),
            "number": int(reply.get("number") or 0),
            "text": str(reply.get("text") or "").strip(),
            "is_op": False,
        })
    return posts


def _extract_inline_img_reply_references(
    comment: object,
    thread: dict[str, Any],
) -> dict[str, Any]:
    cleaned_lines: list[str] = []
    quote_post_numbers: list[str] = []
    quote_reply_numbers: list[int] = []

    for raw_line in str(comment or "").splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue

        match = re.match(r"^\s*(No\.|>{1,2})\s*(\d+)(.*)$", line, flags=re.IGNORECASE)
        if not match:
            cleaned_lines.append(line)
            continue

        marker = match.group(1)
        digits = match.group(2)
        remainder = re.sub(r"^\s*(?:の|:|：|-|－|—|―|,|，)\s*", "", match.group(3) or "").strip()
        matched_target = False

        if marker.lower().startswith("no"):
            post_no = _normalize_img_post_no(digits)
            if post_no and _find_img_thread_post(thread, post_no=post_no):
                quote_post_numbers.append(post_no)
                matched_target = True
        else:
            reply_no = _normalize_img_reply_no(digits)
            if reply_no and _find_img_thread_post(thread, reply_no=reply_no):
                quote_reply_numbers.append(reply_no)
                matched_target = True
            else:
                post_no = _normalize_img_post_no(digits)
                if post_no and _find_img_thread_post(thread, post_no=post_no):
                    quote_post_numbers.append(post_no)
                    matched_target = True

        if matched_target:
            if remainder:
                cleaned_lines.append(remainder)
            continue
        cleaned_lines.append(line)

    return {
        "comment": "\n".join(cleaned_lines).strip(),
        "quote_post_numbers": quote_post_numbers,
        "quote_reply_numbers": quote_reply_numbers,
    }


def _quote_img_post_text(text: object) -> str:
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines() if line.strip()]
    return "\n".join(f">{line}" for line in lines)


def compose_img_thread_reply_comment(
    thread: dict[str, Any],
    comment: object,
    *,
    quote_post_numbers: object = None,
    quote_reply_numbers: object = None,
    max_chars: int = 240,
    max_quotes: int = 2,
) -> dict[str, Any]:
    """本文引用込みの最終投稿本文を組み立てる。"""
    body = sanitize_img_post_text(comment, max_chars=max_chars)
    inferred = _extract_inline_img_reply_references(body, thread)
    cleaned_body = sanitize_img_post_text(
        inferred.get("comment") or body,
        max_chars=max_chars,
    )

    target_specs: list[tuple[str, str | int]] = []
    for raw in _iter_img_post_targets(quote_post_numbers):
        post_no = _normalize_img_post_no(raw)
        if post_no:
            target_specs.append(("post", post_no))
    for raw in _iter_img_post_targets(inferred.get("quote_post_numbers")):
        post_no = _normalize_img_post_no(raw)
        if post_no:
            target_specs.append(("post", post_no))
    for raw in _iter_img_post_targets(quote_reply_numbers):
        reply_no = _normalize_img_reply_no(raw)
        if reply_no > 0:
            target_specs.append(("reply", reply_no))
    for raw in _iter_img_post_targets(inferred.get("quote_reply_numbers")):
        reply_no = _normalize_img_reply_no(raw)
        if reply_no > 0:
            target_specs.append(("reply", reply_no))

    quote_targets: list[dict[str, Any]] = []
    seen_post_numbers: set[str] = set()
    for target_type, raw_value in target_specs:
        if len(quote_targets) >= max(max_quotes, 0):
            break
        target = (
            _find_img_thread_post(thread, post_no=str(raw_value))
            if target_type == "post"
            else _find_img_thread_post(thread, reply_no=int(raw_value))
        )
        if not target:
            continue
        post_no = _normalize_img_post_no(target.get("post_no"))
        if not post_no or post_no in seen_post_numbers:
            continue
        text = str(target.get("text") or "").strip()
        if not text:
            continue
        seen_post_numbers.add(post_no)
        quote_targets.append({
            "post_no": post_no,
            "number": int(target.get("number") or 0),
            "text": text,
            "is_op": bool(target.get("is_op")),
        })

    lines = [_quote_img_post_text(target["text"]) for target in quote_targets]
    if cleaned_body:
        lines.append(cleaned_body)
    final_comment = sanitize_img_post_submission_text("\n".join(line for line in lines if line), max_chars=max_chars)

    return {
        "comment": final_comment,
        "body": cleaned_body,
        "quote_targets": quote_targets,
        "quote_post_numbers": [target["post_no"] for target in quote_targets],
        "quote_reply_numbers": [target["number"] for target in quote_targets if int(target["number"] or 0) > 0],
    }


def _normalize_img_compare_line(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).strip()


def _extract_img_quote_line_items(text: object) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = str(raw_line or "").strip()
        if not line.startswith(">"):
            continue
        match = re.match(r"^(>+)", line)
        depth = len(match.group(1)) if match else 1
        normalized = _normalize_img_compare_line(line.lstrip(">").strip())
        if normalized:
            items.append((depth, normalized))
    return items


def _extract_img_quote_lines(text: object) -> list[str]:
    return [item[1] for item in _extract_img_quote_line_items(text)]


def _extract_img_post_body_lines(text: object) -> list[str]:
    raw_lines = [str(raw_line or "").strip() for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()]
    normalized_body = [_normalize_img_compare_line(line) for line in raw_lines if line and not line.startswith(">")]
    normalized_body = [line for line in normalized_body if line]
    if normalized_body:
        return normalized_body
    return [line for line in (_normalize_img_compare_line(line) for line in raw_lines if line) if line]


def _dedupe_img_lines(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for line in list(lines or []):
        normalized = _normalize_img_compare_line(line)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _find_img_thread_post_cutoff_index(posts: list[dict[str, Any]], reply: dict[str, Any]) -> int:
    target_post_no = _normalize_img_post_no(reply.get("post_no"))
    target_number = int(reply.get("number") or 0)
    for index, post in enumerate(posts):
        post_post_no = _normalize_img_post_no(post.get("post_no"))
        post_number = int(post.get("number") or 0)
        if target_post_no and post_post_no and post_post_no == target_post_no:
            return index
        if target_number > 0 and post_number == target_number:
            return index
    return len(posts)


def find_img_thread_quoted_posts(
    thread: dict[str, Any],
    reply: dict[str, Any],
    *,
    max_targets: int = 2,
) -> list[dict[str, Any]]:
    """レス本文の > 行から、直接引用している元レス候補を新しい順に返す。"""
    if max_targets <= 0:
        return []

    quote_line_items = _extract_img_quote_line_items(reply.get("text") or "")
    if not quote_line_items:
        return []
    quote_depths: dict[str, int] = {}
    for depth, line in quote_line_items:
        normalized = _normalize_img_compare_line(line)
        if not normalized:
            continue
        previous_depth = quote_depths.get(normalized)
        if previous_depth is None or depth < previous_depth:
            quote_depths[normalized] = depth
    quote_lines = set(quote_depths.keys())

    posts = _iter_img_thread_posts(thread)
    cutoff_index = _find_img_thread_post_cutoff_index(posts, reply)
    candidates: list[dict[str, Any]] = []
    for index, post in enumerate(posts[:cutoff_index]):
        body_lines = _dedupe_img_lines(_extract_img_post_body_lines(post.get("text") or ""))
        matched_lines = [line for line in body_lines if len(line) >= 2 and line in quote_lines]
        if not matched_lines:
            continue
        matched_chars = sum(len(line) for line in matched_lines)
        match_ratio = matched_chars / max(sum(len(line) for line in body_lines), 1)
        nearest_quote_depth = min(int(quote_depths.get(line) or 99) for line in matched_lines)
        candidates.append({
            "post_no": _normalize_img_post_no(post.get("post_no")),
            "number": int(post.get("number") or 0),
            "text": str(post.get("text") or "").strip(),
            "is_op": bool(post.get("is_op")),
            "matched_lines": matched_lines,
            "match_ratio": match_ratio,
            "match_chars": matched_chars,
            "nearest_quote_depth": nearest_quote_depth,
            "_sort_index": index,
        })

    candidates.sort(
        key=lambda item: (
            int(item.get("nearest_quote_depth") or 99),
            -int(item.get("_sort_index") or 0),
            -len(list(item.get("matched_lines") or [])),
            -int(item.get("match_chars") or 0),
            float(item.get("match_ratio") or 0.0),
        ),
    )

    results: list[dict[str, Any]] = []
    seen_post_numbers: set[str] = set()
    for candidate in candidates:
        post_no = _normalize_img_post_no(candidate.get("post_no"))
        if post_no and post_no in seen_post_numbers:
            continue
        if post_no:
            seen_post_numbers.add(post_no)
        candidate.pop("_sort_index", None)
        results.append(candidate)
        if len(results) >= max_targets:
            break
    return results


def build_img_thread_reply_context_chain(
    thread: dict[str, Any],
    reply: dict[str, Any],
    *,
    max_depth: int = 3,
) -> list[dict[str, Any]]:
    """対象レスがたどってきた引用元の流れを、近い順に返す。"""
    chain: list[dict[str, Any]] = []
    current = dict(reply or {})
    seen: set[tuple[str, int]] = set()
    initial_key = (
        _normalize_img_post_no(current.get("post_no")),
        int(current.get("number") or 0),
    )
    if initial_key[0] or initial_key[1] > 0:
        seen.add(initial_key)

    for _ in range(max(max_depth, 0)):
        quoted_posts = find_img_thread_quoted_posts(thread, current, max_targets=2)
        if not quoted_posts:
            break
        next_post = dict(quoted_posts[0])
        next_key = (
            _normalize_img_post_no(next_post.get("post_no")),
            int(next_post.get("number") or 0),
        )
        if next_key in seen:
            break
        seen.add(next_key)
        chain.append(next_post)
        current = next_post

    return chain


def _format_img_thread_post_header(post: dict[str, Any]) -> str:
    header = "OP" if bool(post.get("is_op")) else "(番号不明)"
    number = int(post.get("number") or 0)
    if number > 0:
        header = f">>{number}"
    post_no = _normalize_img_post_no(post.get("post_no"))
    if post_no:
        header += f" No.{post_no}"
    return header


def reply_quotes_img_post_text(reply_text: object, post_text: object) -> bool:
    """レス本文の引用行が、対象投稿の本文を引用しているか判定する。"""
    quote_lines = set(_extract_img_quote_lines(reply_text))
    if not quote_lines:
        return False
    post_lines = {
        line
        for line in _extract_img_post_body_lines(post_text)
        if len(line) >= 2
    }
    if not post_lines:
        return False
    return bool(quote_lines & post_lines)


def looks_like_img_reply_parrot(reply_text: object, source_text: object) -> bool:
    """返信本文が参照元レスをほぼそのままなぞっていないか判定する。"""
    reply_body_lines = _dedupe_img_lines(
        _extract_img_post_body_lines(
            sanitize_img_post_submission_text(reply_text, max_chars=4000)
        )
    )
    source_body_lines = _dedupe_img_lines(
        _extract_img_post_body_lines(
            sanitize_img_post_submission_text(source_text, max_chars=4000)
        )
    )
    if not reply_body_lines or not source_body_lines:
        return False

    reply_joined = "\n".join(reply_body_lines).strip()
    source_joined = "\n".join(source_body_lines).strip()
    if len(reply_joined) < 8 or len(source_joined) < 8:
        return False
    if reply_joined == source_joined:
        return True

    reply_compact = _normalize_img_compare_line(reply_joined)
    source_compact = _normalize_img_compare_line(source_joined)
    if reply_compact and source_compact:
        shorter, longer = (
            (reply_compact, source_compact)
            if len(reply_compact) <= len(source_compact)
            else (source_compact, reply_compact)
        )
        if shorter in longer and len(shorter) >= max(8, int(len(longer) * 0.72)):
            return True

    overlap_lines = [line for line in reply_body_lines if line in source_body_lines]
    overlap_len = sum(len(line) for line in overlap_lines)
    reply_len = sum(len(line) for line in reply_body_lines)
    if reply_len > 0 and overlap_len >= max(8, int(reply_len * 0.8)):
        return True
    return False


def find_img_thread_reply_by_comment(
    thread: dict[str, Any],
    comment: object,
    *,
    exclude_post_numbers: object = None,
) -> dict[str, Any] | None:
    """投稿後のスレから、送信した本文に一致するレスを探す。"""
    target_comment = sanitize_img_post_submission_text(comment, max_chars=4000)
    if not target_comment:
        return None

    exclude = {
        _normalize_img_post_no(value)
        for value in _iter_img_post_targets(exclude_post_numbers)
        if _normalize_img_post_no(value)
    }
    for reply in reversed(list(thread.get("replies") or [])):
        post_no = _normalize_img_post_no(reply.get("post_no"))
        if post_no and post_no in exclude:
            continue
        reply_text = sanitize_img_post_submission_text(reply.get("text") or "", max_chars=4000)
        if reply_text == target_comment:
            return {
                "number": int(reply.get("number") or 0),
                "post_no": post_no,
                "text": str(reply.get("text") or "").strip(),
                "soudane": int(reply.get("soudane") or 0),
            }
    return None


def build_img_thread_post_prompt(
    thread: dict[str, Any],
    *,
    requester_text: str = "",
    bot_display_name: str = "",
    max_chars: int = 120,
    character_guidance: str = "",
    social_guidance: str = "",
    memory_lines: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    channel_summary: str = "",
) -> str:
    """LLMに IMG スレへ投稿する短文を作らせるためのプロンプトを作る。"""
    summary = format_img_thread_summary(thread, max_replies=_IMG_THREAD_POST_CONTEXT_LIMIT)
    display_name = str(bot_display_name or "").strip() or "このDiscord bot"
    request = str(requester_text or "").strip()
    persona_text = str(character_guidance or "").strip()
    blocks = [
        "以下は img.2chan.net のスレッドです。",
        "OPと直近10レス（最新10レス）を読んで、スレ内に自然に混ざる短い日本語レスを1つだけ作ってください。",
        "",
        "条件:",
        f"- {max_chars}文字以内。",
        "- 1〜3行。長い解説にしない。",
        "- Discord、AI、bot、LLM、依頼者、外部から来たことには触れない。",
        "- URL、宣伝、個人情報、攻撃的な煽り、政治、違法行為、荒らし誘導は書かない。",
        "- スレ本文にない事実を断定しない。",
        "- 内省メモ、感情ラベル、返答方針、思考、説明文は書かない。",
        "- レス本文中の > 行は返信元の引用。引用の流れが見える場合は、その会話のつながりを踏まえる。",
        "- 直前のレスやOPの文面をそのまま復唱しない。言い換えだけで終わらせない。",
        "- comment には自分の本文だけを入れる。前置き、説明、JSON文字列、箇条書きは禁止。",
        "- 特定レスに返すなら quote_post_numbers に No.1422... の番号、または quote_reply_numbers に >>9 の 9 を入れる。",
        "- 本文引用はコード側で付けるので、comment に >9 / >>9 / No.1422... / 引用本文を書かない。",
        "- OP全体への感想なら quote_post_numbers と quote_reply_numbers は空配列でよい。",
        "",
        f"botの会話上の人格名: {display_name}",
    ]

    if persona_text:
        blocks.extend([
            "",
            "【キャラクター口調ガード】",
            persona_text,
            "上の人格・口調・語尾を投稿本文に強く反映してください。ただしDiscord、AI、bot、設定ファイルの話は本文に出さないでください。",
        ])

    if social_guidance:
        blocks.extend(["", "【現在の感情・返答指針】", social_guidance])

    if memory_lines:
        blocks.extend(["", "【長期記憶】", "\n".join(memory_lines)])

    if channel_summary:
        blocks.extend(["", "【Discord内でのこれまでの文脈】", channel_summary])

    if habit_profile_lines:
        blocks.extend(["", "【あなたの思考・行動のクセ（参考）】", "\n".join(habit_profile_lines)])

    blocks.extend([
        "",
        f"Discordで貼られた最近の状況: {request or '(なし)'}",
        "",
        summary,
    ])
    return "\n".join(blocks)


def build_img_thread_followup_post_prompt(
    thread: dict[str, Any],
    target_reply: dict[str, Any],
    *,
    quoted_own_posts: list[dict[str, Any]] | None = None,
    bot_display_name: str = "",
    max_chars: int = 120,
    character_guidance: str = "",
    social_guidance: str = "",
    memory_lines: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    channel_summary: str = "",
) -> str:
    """自分の書き込みへの本文引用レスに返す短文用プロンプト。"""
    summary = format_img_thread_summary(thread, max_replies=_IMG_THREAD_POST_CONTEXT_LIMIT)
    display_name = str(bot_display_name or "").strip() or "このDiscord bot"
    persona_text = str(character_guidance or "").strip()
    target_no = int(target_reply.get("number") or 0)
    target_post_no = str(target_reply.get("post_no") or "").strip()
    target_header = f">>{target_no}" if target_no > 0 else "(番号不明)"
    if target_post_no:
        target_header += f" No.{target_post_no}"

    own_post_lines: list[str] = []
    for own_post in list(quoted_own_posts or [])[:2]:
        own_post_no = str(own_post.get("post_no") or "").strip()
        own_header = f"No.{own_post_no}" if own_post_no else "(投稿番号不明)"
        own_body = str(own_post.get("text") or "").strip() or "(本文なし)"
        own_post_lines.extend([own_header, own_body, ""])

    context_chain = build_img_thread_reply_context_chain(thread, target_reply, max_depth=3)
    context_chain_lines: list[str] = []
    for index, context_post in enumerate(context_chain):
        label = "このレスが直接引用している元" if index == 0 else "さらにその引用元"
        context_chain_lines.extend([
            f"{label}:",
            _format_img_thread_post_header(context_post),
            str(context_post.get("text") or "").strip() or "(本文なし)",
            "",
        ])

    blocks = [
        "以下は img.2chan.net のスレッドです。",
        "あなたが前に書いたレスに対して、新しく本文引用つきのレスが来ました。",
        "その対象レスにだけ自然に返す短い日本語レスを1つだけ作ってください。",
        "",
        "条件:",
        f"- {max_chars}文字以内。",
        "- 1〜3行。長い解説にしない。",
        "- Discord、AI、bot、LLM、依頼者、外部から来たことには触れない。",
        "- URL、宣伝、個人情報、攻撃的な煽り、政治、違法行為、荒らし誘導は書かない。",
        "- スレ本文にない事実を断定しない。",
        "- 対象レスの引用元が示されている場合は、その流れを読んだ上で返す。",
        "- 今回返す対象レスの文面や語尾をそのままなぞるのは禁止。",
        "- 対象レスの言い換えだけで終わらず、自分の反応やツッコミを自分の言葉で入れる。",
        "- 内省メモ、感情ラベル、返答方針、思考、説明文は書かない。",
        "- comment には自分の本文だけを入れる。引用本文はコード側で自動付与する。",
        "- quote_post_numbers と quote_reply_numbers は空配列でよい。",
        "",
        f"botの会話上の人格名: {display_name}",
    ]

    if persona_text:
        blocks.extend([
            "",
            "【キャラクター口調ガード】",
            persona_text,
            "上の人格・口調・語尾を投稿本文に強く反映してください。ただしDiscord、AI、bot、設定ファイルの話は本文に出さないでください。",
        ])

    if social_guidance:
        blocks.extend(["", "【現在の感情・返答指針】", social_guidance])

    if memory_lines:
        blocks.extend(["", "【長期記憶】", "\n".join(memory_lines)])

    if channel_summary:
        blocks.extend(["", "【Discord内でのこれまでの文脈】", channel_summary])

    if habit_profile_lines:
        blocks.extend(["", "【あなたの思考・行動のクセ（参考）】", "\n".join(habit_profile_lines)])

    blocks.extend([
        "",
        "あなたが前に書いたレス:",
        *(own_post_lines or ["(今回引用された自分のレスは特定できていない)"]),
        "今回返す対象レス:",
        target_header,
        str(target_reply.get("text") or "").strip() or "(本文なし)",
        "",
        *(context_chain_lines or ["対象レスの引用元の流れ: (抽出できず)", ""]),
        summary,
    ])
    return "\n".join(blocks)


def build_img_thread_post_system_prompt(persona: str = "") -> str:
    persona_text = str(persona or "").strip()
    base = (
        "あなたは匿名掲示板の短いレス文を作る補助役です。"
        "自然な日本語で、周囲の流れに合わせた一言だけを作ってください。"
        "キャラクター設定が渡された場合は、その一人称、語尾、価値観、温度感を必ず反映してください。"
        "ただしキャラクター設定やDiscord上の素性を説明してはいけません。"
        "出力は必ずJSONだけにしてください。"
        "commentには投稿本文だけを入れてください。"
        "相手の本文を復唱するだけの返答は禁止です。"
        "引用が必要なら quote_post_numbers または quote_reply_numbers で指定してください。"
        "commentには >9 や No.123 のような番号引用や引用本文を入れないでください。"
        "内省メモ、感情ラベル、思考、説明、前置きは絶対に入れないでください。"
    )
    if persona_text:
        return f"{base}\n\n【会話上の人格参考】\n{persona_text}"
    return base
