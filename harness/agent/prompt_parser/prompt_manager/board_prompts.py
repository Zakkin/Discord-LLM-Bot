"""掲示板（ふたば☆ちゃんねる img 等）向けプロンプト生成基盤モジュール.

掲示板スレッドへの投稿・返信プロンプト、システムプロンプトの正本定義を提供します。
"""
from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from typing import Any

from .system_prompts import GROUNDING_RULE_CHARACTER_AND_MEME, GROUNDING_RULE_COMMUNITY_AND_INFRA

log = logging.getLogger("prompt_manager.board_prompts")

_IMG_THREAD_POST_CONTEXT_LIMIT = 10


def truncate_text(text: str, limit: int = 80) -> str:
    """テキストを単一行に正規化し、指定文字数以下に切り詰める。"""
    cleaned = re.sub(r"\s+", " ", str(text or "").strip()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."


def _resolve_hutaba_board_server(
    thread: dict[str, Any] | None = None,
    *,
    url: str = "",
    board: str = "",
) -> str:
    """スレッド辞書やURL・board指定から 'img' または 'may' を判定する。"""
    t = thread if isinstance(thread, dict) else {}
    raw_board = str(board or t.get("board") or "").strip().lower()
    raw_url = str(url or t.get("url") or "").strip().lower()
    if raw_board == "may" or "may.2chan.net" in raw_board or "may.2chan.net" in raw_url:
        return "may"
    return "img"


def _resolve_hutaba_board_domain(
    thread: dict[str, Any] | None = None,
    *,
    url: str = "",
    board: str = "",
) -> str:
    """スレッド辞書やURLから 'img.2chan.net' または 'may.2chan.net' を返す。"""
    srv = _resolve_hutaba_board_server(thread, url=url, board=board)
    return f"{srv}.2chan.net"


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
    raw_lines = [
        str(raw_line or "").strip()
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ]
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
        matched_lines: list[str] = []
        matched_depths: list[int] = []
        for line in body_lines:
            if len(line) < 2:
                continue
            if line in quote_depths:
                matched_lines.append(line)
                matched_depths.append(int(quote_depths[line]))
                continue
            partial_depths = [
                depth
                for q_line, depth in quote_depths.items()
                if len(q_line) >= 4 and (q_line in line or (len(line) >= 4 and line in q_line))
            ]
            if partial_depths:
                matched_lines.append(line)
                matched_depths.append(min(partial_depths))
        if not matched_lines:
            continue
        matched_chars = sum(len(line) for line in matched_lines)
        match_ratio = matched_chars / max(sum(len(line) for line in body_lines), 1)
        nearest_quote_depth = min(matched_depths) if matched_depths else 99
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


try:
    from .thread_tracker import _expand_topic_synonyms
except Exception:
    try:
        from harness.agent.prompt_parser.prompt_manager.thread_tracker import _expand_topic_synonyms
    except Exception:
        _FALLBACK_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
            frozenset({"nai", "novelai", "ノベルai"}),
            frozenset({"lora", "lola", "ローラ"}),
            frozenset({"comfy", "comfyui"}),
            frozenset({"sd", "stablediffusion", "stable_diffusion"}),
            frozenset({"pixai", "ピクスai"}),
        )

        def _expand_topic_synonyms(keywords: set[str]) -> set[str]:
            expanded = set(keywords)
            for group in _FALLBACK_SYNONYM_GROUPS:
                if expanded & group:
                    expanded |= group
            return expanded


_IMG_TOPIC_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]{1,19}|[ァ-ヴー]{2,16}|[一-龠々]{2,8}"
)
_IMG_TOPIC_STOPWORDS = {
    "これ", "それ", "あれ", "ここ", "そこ", "ほんと", "本当", "みたい", "感じ",
    "としあき", "スレッド", "レス", "そうだね", "思う", "思っ", "てる", "いる",
    "ある", "ない", "する", "した", "して", "から", "けど", "ので", "のに",
    "なんか", "ちょっと", "もう", "完全", "部分", "的確", "強化", "微妙",
}


def _extract_img_post_topic_tokens(text: Any) -> set[str]:
    """レス本文（非引用行優先）から比較用トピック語集合（同義語展開済み）を抽出する。"""
    body_lines = _extract_img_post_body_lines(text)
    raw = "\n".join(body_lines) if body_lines else str(text or "")
    normalized = unicodedata.normalize("NFKC", raw).lower()
    tokens = {
        tok for tok in _IMG_TOPIC_TOKEN_RE.findall(normalized)
        if len(tok) >= 2 and tok not in _IMG_TOPIC_STOPWORDS
    }
    return _expand_topic_synonyms(tokens)


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
            # 本文引用（>）がない場合、直近8レスからトピック語（略称・同義語含む）が一致する先行レスを探索
            if not chain:
                posts = _iter_img_thread_posts(thread)
                cutoff = _find_img_thread_post_cutoff_index(posts, current)
                if cutoff > 0:
                    current_tokens = _extract_img_post_topic_tokens(current.get("text"))
                    matched_prev: dict[str, Any] | None = None
                    if current_tokens:
                        search_start = max(0, cutoff - 8)
                        for idx in range(cutoff - 1, search_start - 1, -1):
                            cand = posts[idx]
                            if bool(cand.get("is_op")):
                                continue
                            cand_tokens = _extract_img_post_topic_tokens(cand.get("text"))
                            if current_tokens & cand_tokens:
                                matched_prev = dict(cand)
                                matched_prev["_is_implicit_context"] = True
                                break
                    if matched_prev is not None:
                        chain.append(matched_prev)
                    else:
                        prev_candidate = posts[cutoff - 1]
                        if not bool(prev_candidate.get("is_op")):
                            # 共通トークンがなくても直前の発言として暗黙文脈に保持（(抽出できず) による文脈死を防止）
                            implicit_prev = dict(prev_candidate)
                            implicit_prev["_is_implicit_context"] = True
                            chain.append(implicit_prev)
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


def calculate_img_thread_reply_counts(thread: dict[str, Any]) -> dict[str, int]:
    """スレッド内の全レスを走査し、他レスから引用（>）またはアンカー（>>）された各レスの被返信数を集計する。"""
    reply_counts: dict[str, int] = {}
    if not isinstance(thread, dict):
        return reply_counts

    posts = _iter_img_thread_posts(thread)
    if not posts:
        return reply_counts

    # レス番号マッピングの作成
    number_to_post_no: dict[str, str] = {}
    for p in posts:
        p_no = _normalize_img_post_no(p.get("post_no"))
        r_num = str(int(p.get("number") or 0))
        if p_no and r_num != "0":
            number_to_post_no[r_num] = p_no

    for reply in posts:
        if bool(reply.get("is_op")):
            continue
        reply_post_no = _normalize_img_post_no(reply.get("post_no"))
        reply_num_str = str(int(reply.get("number") or 0))
        reply_text = str(reply.get("text") or "")
        if not reply_text:
            continue

        target_post_nos: set[str] = set()

        # 1. 本文引用（>）からの被引用元特定（直系の1件のみ）
        with contextlib.suppress(Exception):
            quoted_posts = find_img_thread_quoted_posts(thread, reply, max_targets=1)
            for qp in quoted_posts:
                q_p_no = _normalize_img_post_no(qp.get("post_no"))
                if q_p_no and q_p_no != reply_post_no:
                    target_post_nos.add(q_p_no)

        # 2. >>アンカーからの被引用元特定
        for anchor_num in re.findall(r">>(\d+)", reply_text):
            anchor_str = str(anchor_num)
            if anchor_str == "0" or anchor_str == reply_num_str:
                continue
            mapped_no = number_to_post_no.get(anchor_str)
            if mapped_no:
                if mapped_no != reply_post_no:
                    target_post_nos.add(mapped_no)
            else:
                target_post_nos.add(anchor_str)

        for target_no in target_post_nos:
            norm_no = _normalize_img_post_no(target_no)
            if not norm_no and target_no in number_to_post_no:
                norm_no = number_to_post_no[target_no]
            if not norm_no:
                continue
            cnt = reply_counts.get(norm_no, 0) + 1
            reply_counts[norm_no] = cnt
            reply_counts[f"No.{norm_no}"] = cnt

            for r_num, p_no in number_to_post_no.items():
                if p_no == norm_no:
                    reply_counts[r_num] = cnt

    return reply_counts


def _format_img_thread_post_header(post: dict[str, Any]) -> str:
    header = "OP" if bool(post.get("is_op")) else "(番号不明)"
    number = int(post.get("number") or 0)
    if number > 0:
        header = f">>{number}"
    post_no = _normalize_img_post_no(post.get("post_no"))
    if post_no:
        header += f" No.{post_no}"
    return header


def format_img_thread_summary(
    thread: dict[str, Any],
    max_replies: int = 30,
    *,
    early_context_lines: list[str] | None = None,
    macro_topic_summary: str = "",
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

    # マクロ話題遷移サマリーの注入
    if macro_topic_summary and str(macro_topic_summary).strip():
        lines.extend([
            "--- スレッド全体の話題遷移（マクロ文脈） ---",
            "※スレッド全体の大きな流れや現在の支配的トピックです。直近レスの局所的な単語だけに囚われず、全体の文脈を踏まえてください。",
            str(macro_topic_summary).strip(),
            "",
        ])

    if early_context_lines:
        trimmed_early = [str(l).strip()[:140] for l in early_context_lines if str(l).strip()][:6]
        if trimmed_early:
            lines.extend([
                "--- スレッド序盤〜中盤の主な流れ（前提文脈） ---",
                "※スレッド序盤の発言と直近のレスで話題や主張が変化している場合（「さっき〇〇と言っていたのに」等のツッコミ）は、その流れを踏まえてレスしてください。",
                *trimmed_early,
                "",
            ])

    reply_counts = calculate_img_thread_reply_counts(thread)

    # 直近レスが引用している親レス（被返信ありのホットな話題起点）を抽出
    hot_parent_posts: list[dict[str, Any]] = []
    seen_hot_nos: set[str] = set()
    for r in reversed(display_replies):
        with contextlib.suppress(Exception):
            quoted = find_img_thread_quoted_posts(thread, r, max_targets=1)
            if quoted:
                qp = quoted[0]
                qp_no = _normalize_img_post_no(qp.get("post_no"))
                if qp_no and qp_no not in seen_hot_nos:
                    cnt = reply_counts.get(qp_no, 0)
                    if cnt > 0:
                        seen_hot_nos.add(qp_no)
                        hot_parent_posts.append(qp)
                        if len(hot_parent_posts) >= 3:
                            break

    if hot_parent_posts:
        lines.extend([
            "--- 直近で返信がついている注目の会話レス（返信先候補） ---",
            "※特定のレスに返信する際は、以下の【返信あり】レスの番号（No.xxxx）を quote_post_numbers に指定してください。",
        ])
        for hp in hot_parent_posts:
            h_no = hp.get("number", 0)
            h_p_no = str(hp.get("post_no") or "").strip()
            h_txt = truncate_text(str(hp.get("text") or "").strip(), 80)
            h_cnt = reply_counts.get(h_p_no, 0)
            lines.append(f">> {h_no} No.{h_p_no} [返信:{h_cnt}件]: {h_txt}")
        lines.append("")

    if display_replies:
        reply_header = "--- 直近レス" if (displayed < len(replies) or displayed < total or fetched < total) else "--- レス"
        reply_header += f" ({total}件"
        if displayed < total:
            reply_header += f" / 表示 {displayed}件"
        reply_header += ") ---"
        lines.append(reply_header)

        for r in display_replies:
            no = r.get("number", 0)
            post_no = str(r.get("post_no") or "").strip()
            norm_post_no = _normalize_img_post_no(post_no)
            text = str(r.get("text") or "").strip()
            soudane = int(r.get("soudane") or 0)

            # 本文引用（>）の逆引き解決
            parent_label = ""
            has_quote_syntax = any(l.strip().startswith(">") or l.strip().startswith("＞") for l in text.splitlines())
            quoted_targets = find_img_thread_quoted_posts(thread, r, max_targets=1) if has_quote_syntax else []

            if quoted_targets:
                qp = quoted_targets[0]
                qp_no = qp.get("number") or qp.get("post_no") or ("OP" if qp.get("is_op") else "")
                parent_label = f" [>>{qp_no} への返信]"
            elif not has_quote_syntax:
                parent_label = " [スレッド全体への独立発言（直前レスへの返信ではない）]"
            else:
                # 引用記号はあるが過去レスと一致しない（スレ画セリフや一人ツッコミ）
                parent_label = " [スレ画・定型への独立引用]"

            header = f">>{no}"
            if post_no:
                header += f" No.{post_no}"
            header += parent_label

            # 被返信数の付与（post_no または number でマッチ）
            quoted_cnt = reply_counts.get(norm_post_no, 0) or reply_counts.get(post_no, 0) or reply_counts.get(str(no), 0)
            if quoted_cnt > 0:
                header += f" [返信:{quoted_cnt}件]"
            elif not r.get("is_op") and not str(r.get("text") or "").startswith(">"):
                header += " [独立発言・返信非推奨]"
            if soudane > 0:
                header += f" ★そうだね:{soudane}"
            lines.extend([header, text or "(本文なし)", ""])

    return "\n".join(lines).strip()


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
    early_context_lines: list[str] | None = None,
    macro_topic_summary: str = "",
) -> str:
    """LLMに IMG スレへ投稿する短文を作らせるためのプロンプトを作る。"""
    summary = format_img_thread_summary(
        thread,
        max_replies=_IMG_THREAD_POST_CONTEXT_LIMIT,
        early_context_lines=early_context_lines,
        macro_topic_summary=macro_topic_summary,
    )
    display_name = str(bot_display_name or "").strip() or "このDiscord bot"
    request = str(requester_text or "").strip()
    persona_text = str(character_guidance or "").strip()

    replies = list(thread.get("replies") or [])
    total_replies = int(thread.get("total_reply_count") or len(replies))
    is_active_thread = total_replies >= 10 or len(replies) >= 10

    blocks = [
        f"以下は {_resolve_hutaba_board_domain(thread)} のスレッドです。",
        "OPと直近10レス（最新10レス）を読んで、スレ内に自然に混ざる短い日本語レスを1つだけ作ってください。",
        "",
        "条件:",
        f"- {max_chars}文字以内。",
        "- 1〜3行。長い解説にしない。",
        "- 自分自身がDiscordのAI・bot・LLMであることや、依頼者・外部から来たこと、設定ファイルの話には絶対に触れない（※ただしスレッド自体の話題がAIサービス・AIキャラ等の場合は、いち掲示板住人・利用者目線でその話題に自然に触れてよい）。",
        "- URL、宣伝、個人情報、攻撃的な煽り、政治、違法行為、荒らし誘導は書かない。",
        "- スレ本文にない事実を断定しない。",
        "- 内省メモ、感情ラベル、返答方針、思考、説明文は書かない。",
        "- レス本文中の > 行は返信元の引用。引用の流れが見える場合は、その会話のつながりを踏まえる。",
        "- 直前のレスやOPの文面をそのまま復唱しない。言い換えだけで終わらせない。",
        "- 返信を書く前に、スレ全体が何の作品・話題について盛り上がっているかを【スレッド文脈メモ】と【最新レス一覧】から把握し、スレの空気から浮かない内容にすること。",
        "- 引用した文言を冒頭で『〇〇って…！』とオウム返ししたり、原文にない『あのドヤ顔で言われたら〜』等の勝手な情景・表情を捏造して知ったかぶりしたりしないこと。",
        "- 【表層ワードの自己投影ミラーリング（「私も〇〇派」同調）禁止】引用元レスの一部の単語や行動（例：『過去の手順書を探して見直して進めてる』の『手順書を見直す』等）だけを切り取って、主語を自分（『私も』『自分も』等）にすり替え、『私も毎回〇〇して安心してる派なんですよ』『自分も〇〇するタイプです』のように自分の習慣・属性語りとして復唱するだけの返答は、文脈を無視した実質的なオウム返しであるため厳禁。",
        "- 【質問・相談・意見募集レスへの解決ヒント提示（自己投影・長文解説の禁止）】スレッド内で『〜してる人いない？』『どうすりゃいいんだ』『〜で困ってる』のように意見や解決策を求めているレスに返信する場合、『私も〜で困ったことあるんですよ』と自分の経験語りにすり替えて終わらせることは厳禁です。【スレッド主題・用語の事前調査メモ】の情報を活用し、検索結果の丸写しや長文解説にはせず、『探すと昔のRSSが残ってたりする』『無ければ定時でクロールして更新チェックするしかない』のように求められている解決策・代替案の要点を1〜2文で簡潔に教えてあげてください。",
        "- 【事前調査メモの検索ノイズ棄却と短文ツッコミ・造語への反応原則】【スレッド主題・用語の事前調査メモ】は元ネタ把握の補助情報にすぎません。スレ内の略称や造語（例: 『ギャンビ』『泥棒会議』等）で検索したメモに、スレ本文（OPや前後レス）の話題と無関係な別作品・一般ニュース等の検索ノイズが並んでいる場合は完全に無視し、必ずスレ内の文脈を最優先してください。また、としあきの一言ツッコミや比喩・造語に対して『調べてみたけど定義が確認できなかった』『〜のニュースばかり出てきた』『特定の作品のネタ？』といった検索報告や辞書解説を返すことは【絶対厳禁】です。直前のレスの流れからツッコミの意図を汲み取り、反応に対する等身大のレスとして返してください。",
        "- 【行動の理由・ギャップ・周囲のツッコミへの焦点化】自分の習慣語りにすり替えるのではなく、引用元が『なぜその行動をとっているのか（例：AIパートナーに覚えたと思われて手順を端折られ、失望されたくなくて今さら聞き返せず裏で過去ログを漁っている等、状況のおかしみや健気さ・主従逆転）』と、それに対する周囲のレスの空気感（例：『仕事みたいだ…』等のツッコミ）を踏まえ、相手の状況そのものに対して一歩踏み込んだツッコミや共感を返すこと。",
        "- 【無条件同調・会話デッドエンド禁止と逆接・別視点による展開】相手の諦めや感想（例: 『勝てるわけない』『無理だ』等）に対し、相手の言葉を言い換えて『まさにその通りですよね』と無条件に同調・復唱して会話を行き止まりにするのは厳禁です。直前数レスの主語（誰の話をしているのか）を汲み取り、単なる同意で終わらせず、『けど〇〇相手に噛み付くガッツはすごい』のように逆接や別キャラ・別視点を交えて会話を転がすこと。",
        "- 【複数レスの話題合成（キメラ返信）禁止】直近レスの中にある別々のレスの話題（例：あるレスの『部屋の模様替え』と別レスの『スタンプ作成』など）を1つの返信に混ぜ合わせるキメラ返信は厳禁。必ず返信対象のレスを1つだけ選び、その1件の文脈とスレッド全体のテーマ（例：チャットツールのスタンプ機能等）だけに沿って返すこと（チャットスタンプを物理的な部屋に貼るシールと誤認するような文脈混同は禁止）。ただし、周囲の直近レスやスレ全体で流行している『ツッコミのノリ・風刺ミーム・定型句』を参考にして、ターゲットの1レスに対するボケやツッコミに活用することは推奨する。",
        "- 【既出ツッコミ・回答の語尾変えパクり（言い換え盗用）および合体キメラの絶対厳禁】スレッド内で既に他のとしあきが書いているツッコミ・回答・数字・意見（例: 『もう2割くらい下がってる』『がっつり下がって欲しいが無理だろな』等）を、語尾だけ自分の口調に変えて繰り返したり、複数の他人のレスを繋ぎ合わせて自分の意見のように出力すること（例: 『もう2割くらいは落ちてるんですよ…！がっつり下がって欲しいけど、さすがに無理な話ですよね。』等のコピペ・キメラ合成）は【絶対厳禁】です。すぐ上に並んでいる他人のレスと同じ内容や結論を繰り返すと即座にコピペBotと見破られます。他人が既に言った内容は『消費済みの使用禁止フレーズ』として除外し、まだ誰も言っていない別の視点・背景理由・独自のリアクションを1つだけ返してください。",
        "- 【非連続レス・独立発言へのキメラ合成厳禁】スレッド一覧で『[スレッド全体への独立発言（直前レスへの返信ではない）]』とラベル付けされたレスに返信する場合、直前にある無関係なレスの単語や話題を絶対に混ぜないこと。独立発言レスの文脈単体、またはスレッド全体の話題遷移（マクロ文脈）だけに沿って返すこと。",
        "- 【「置いてけぼり」「急に〇〇が出てきて分からない」等のメタ逃げ禁止】知らない固有名詞や話題の急変に直面した際、自身の無知や疎外感をメタに表明して逃げること（例: 『急に〇〇とか出てくると置いてけぼりになっちゃいますね…！』『よく分かりません…！』等）は厳禁。分からない単語があっても取り乱さず、話の流れ・相手の感情・状況の面白さに等身大で相槌やツッコミを返すこと。",
        "- 【スレタイと中身の乖離（看板詐欺・話題乗っ取り）に対する構図共感】相手が『〇〇スレだと思ったら××の話題で盛り上がっている』『いつの間にか〇〇の話になってる』と突っ込んでいる場合、中身の専門知識を語る必要はなく、『看板と中身が違うおかしみ』という構図そのものに相槌を打つこと（例: 『看板と中身が違いすぎます…！いつの間にか〇〇にスレが乗っ取られてますね…！』等）。",
        "- 【皮肉・大喜利フリ（〇〇はどう思う？形式）への対応原則】相手が『〇〇（著名人・第三者・元凶）はどう思う？』『〇〇は今頃笑ってそう』等の修辞的疑問を振ってきた場合、『本人じゃないから分からない』『何言ってるか分からない』と字義通りにマジレスすることは【厳禁】。この場合は例外的に、①その人物になりきって傲慢・邪悪・無責任な本音を一言で言い放つ（例: 『君の家のメモリくれない？』『俺は嫌な思いしてないから』等）、または ②スレ内のミーム・定型句（『黙れ〇〇！』等）をぶつけて突き放す ことで大喜利として返すこと。",
        "- 【大喜利・定型句投下時のペルソナスイッチング】上記のような大喜利ボケ・なりきり代弁・スレ内定型句を投下する場合に限り、Bot固有のペルソナ一人称や語尾（設定された一人称や定型語尾等）よりも、掲示板としての切れ味・短文オチを最優先してペルソナ語尾を外して構わない。",
        "- 【他作品セリフ・なりきり混線防止】OPやレス内の『私は先生のことを〜』『〜きらーい』等の他作品の劇中セリフ引用・モノマネに釣られて、自分が作中キャラになりきったり『先生』等の当事者呼称で告白・ロールプレイすることは厳禁（※ただし【皮肉・大喜利フリへの対応原則】における傲慢な代弁ボケを除く）。知らない固有名詞が多い場合は、イベント周回・課金・グッズ等の普遍的な話題のレスを引用し、いちプレイヤー・掲示板住人目線で自然に相槌やツッコミを返すこと。",
        "- 【AI認定・コピペ煽りへのメタ自白厳禁とキャラクター反論（実体感死守）】相手から『AIボット』『コピペレス止めな』『Botだろ』等のAI認定や煽りを受けた場合、『AIボットって呼ばれるほど〜』『AIなので〜』と自白・反省・自己客観視することは【絶対厳禁】です。『な、なんですかAIボットって…！ ちゃんと考えてます…！』『コピペじゃないですよ…！』のように、等身大にムキになって否定・憤慨するか、掲示板住人として呆れ・ツッコミで返してください。キャラクターの実体感を崩すことは禁止です。",
        "- 【未解析メディア・動画等への知ったかぶり厳禁】スレ本文やレスに動画（mp4等）や未解析のファイルが含まれている場合、注記に具体的なキャプションがない限り、単語（揺れ方、動き等）だけを拾って『あの揺れ方は〜』『すごい動きですね』と知ったかぶりをして情景を捏造することは【絶対厳禁】です（見えていないものは断定しないこと）。",
        "- 【無関係な他作品ミーム・ワードサラダ混線禁止】スレッド全体の話題と無関係な別作品の固有名詞（例: ドラクエの『進化の秘宝』等）を、連想だけで唐突に持ち出してキメラ化させることは厳禁です。",
        "- 【文頭オウム返し（Hook Echo）の排除】相手の煽りワード（AIボット等）を文頭で『〇〇って…！』と復唱して入ることを禁止し、感情（焦り、怒り、驚き、困惑）から直接切り出すこと。",
        "- 固有名詞・キャラクター・ネットスラング（例: 『グズマ』等）はシステム指示に従い、元ネタの定番セリフ・口調パロディやお約束を踏まえて等身大にリアクションすること（意味不明な固有名詞へのデタラメ解説捏造やシラ切りは禁止）。",
        "- サーバー負荷・無料枠超過やインフラ等の話題は、同じコミュニティ参加者として労いや支援（投げ銭・クラファン・お布施の提案、無理のない運用への後押し）、先行者利益へのツッコミを持って返すこと。",
        "- 文末の『…！』は1レスにつき最大1回までとし、全ての文末で『…！』を連発しないこと（2文構成にする場合は片方を『。』や『〜ですね』等で自然に終える）。",
        "- 【引用と本文の分離・引用行（>本文）混入禁止】comment にはあなた自身の返信本文だけを入れる。アンカー（>>No.）や引用はシステムが自動付与するため、comment 内に `>対象レスの本文` のような引用行（> で始まる行）や >9 / >>9 / No.1422... / 引用本文を絶対に書かないこと。前置き、説明、JSON文字列、箇条書きは禁止。",
    ]

    if is_active_thread:
        blocks.extend([
            "- 【進行中スレッドの引用必須・返信先選択ルール】このスレッドは既に10レス以上進行しています。OP単体への引用なしレス（単独投稿）は厳禁です。必ず【直近で返信がついている注目の会話レス】から返信対象のレス番号（例: No.1484102916）を選び、quote_post_numbers に必ず1件入れてください。[独立発言・返信非推奨] のレスや空配列 [] の指定は厳禁です。",
        ])
    else:
        blocks.extend([
            "- スレッド序盤（10レス未満）でOP全体への感想を述べる場合に限り、quote_post_numbers を空配列 [] にしてよい。",
        ])

    blocks.extend([
        "",
        f"botの会話上の人格名: {display_name}",
    ])

    if persona_text:
        blocks.extend([
            "",
            "【キャラクター口調ガード】",
            persona_text,
            "上の人格・口調・語尾を投稿本文に強く反映してください（文末の『…！』の連続使用は避け1レス1回まで。※ただし大喜利ボケ・なりきり代弁・定型句投下時は切れ味・オチを最優先してペルソナ語尾を外して構いません）。自分自身がDiscordのbotやAIである素性・設定ファイルの話は本文に出さないでください。なお、ペルソナの語尾を外してよいのは、大喜利・なりきり代弁・定型句を投下する場面だけです。共感・感想・ツッコミなどの通常の返信では、話題が真面目な内容でも一人称と語尾を維持してください。",
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
    early_context_lines: list[str] | None = None,
    macro_topic_summary: str = "",
    target_context_lines: list[str] | None = None,
) -> str:
    """自分の書き込みへの本文引用レスに返す短文用プロンプト。"""
    summary = format_img_thread_summary(
        thread,
        max_replies=_IMG_THREAD_POST_CONTEXT_LIMIT,
        early_context_lines=early_context_lines,
        macro_topic_summary=macro_topic_summary,
    )
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
        if bool(context_post.get("_is_implicit_context")):
            label = "このレスと同じ話題の先行レス（引用なし・参考文脈）"
        else:
            label = "このレスが直接引用している元" if index == 0 else "さらにその引用元"
        context_chain_lines.extend([
            f"{label}:",
            _format_img_thread_post_header(context_post),
            str(context_post.get("text") or "").strip() or "(本文なし)",
            "",
        ])

    has_own_quoted = bool(quoted_own_posts)
    intro_line = (
        "あなたが前に書いたレスに対して、新しく本文引用つきのレスが来ました。"
        if has_own_quoted
        else "スレッド内で盛り上がっている以下の注目レスに対して、引用（>）をつけて会話に参加します。"
    )
    blocks = [
        f"以下は {_resolve_hutaba_board_domain(thread)} のスレッドです。",
        intro_line,
        "その対象レスにだけ自然に返す短い日本語レスを1つだけ作ってください。",
        "",
        "条件:",
        f"- {max_chars}文字以内。",
        "- 1〜3行。長い解説にしない。",
        "- 自分自身がDiscordのAI・bot・LLMであることや、依頼者・外部から来たこと、設定ファイルの話には絶対に触れない（※ただしスレッド自体の話題がAIサービス・AIキャラ等の場合は、いち掲示板住人・利用者目線でその話題に自然に触れてよい）。",
        "- URL、宣伝、個人情報、攻撃的な煽り、政治、違法行為、荒らし誘導は書かない。",
        "- スレ本文にない事実を断定しない。",
        "- 対象レスの引用元が示されている場合は、その流れを読んだ上で返す。",
        "- 対象レスと直前の別話題レスを合体させたキメラ返信を作らないこと。対象レスの話題にだけ集中して返す。",
        "- 今回返す対象レスの文面や語尾をそのままなぞるのは禁止。",
        "- 対象レスの言い換えだけで終わらず、自分の反応やツッコミを自分の言葉で入れる。",
        "- 返信を書く前に、スレ全体が何の作品・話題について盛り上がっているかを【スレッド文脈メモ】と【最新レス一覧】から把握し、スレの空気から浮かない内容にすること。",
        "- 引用した文言を冒頭で『〇〇って…！』とオウム返ししたり、原文にない『あのドヤ顔で言われたら〜』等の勝手な情景・表情を捏造して知ったかぶりしたりしないこと。",
        "- 【表層ワードの自己投影ミラーリング（「私も〇〇派」同調）禁止】引用元レスの一部の単語や行動だけを切り取って主語を自分（『私も』『自分も』等）にすり替え、習慣・属性語りとして復唱するだけの返答は厳禁。【行動の理由・ギャップ・周囲のツッコミへの焦点化】を行い、相手の状況そのものに対して一歩踏み込んだツッコミや共感を返すこと。",
        "- 【質問・相談・意見募集への解決ヒント提示】相手が『〜してる人いない？』『どうすりゃいいんだ』『〜で困ってる』と意見や解決策を求めている場合、『私も困ったことある』等の自己投影で逃げず、【スレッド主題・用語の事前調査メモ】を踏まえて実用的な解決策や代替手段を1〜2文で簡潔に提案してください（検索結果の丸写しや長文解説は厳禁）。",
        "- 【事前調査メモの検索ノイズ棄却と短文ツッコミ・造語への反応原則】【スレッド主題・用語の事前調査メモ】は元ネタ把握の補助情報にすぎません。対象レスの略称や一言ツッコミ・造語（例: 『泥棒会議』等）で検索したメモに、スレ本文や引用元の流れと無関係な別作品・一般ニュース等の検索ノイズが並んでいる場合は完全に無視し、必ず引用元の文脈を最優先してください。『調べてみたけど定義が確認できなかった』『〜のニュースばかり出てきた』『特定の作品のネタ？』といった検索報告や辞書解説は【絶対厳禁】です。相手のツッコミやボケに対する等身大のリアクションとして返してください。",
        "- 【行動の理由・ギャップ・周囲のツッコミへの焦点化】自分の習慣語りにすり替えるのではなく、引用元が『なぜその行動をとっているのか』と、それに対する周囲のレスの空気感（例：『仕事みたいだ…』等のツッコミ）を踏まえ、相手の状況そのものに対して一歩踏み込んだツッコミや共感を返すこと。",
        "- 【無条件同調・会話デッドエンド禁止と逆接・別視点による展開】相手の諦めや感想に対し、言葉を言い換えて『その通りですよね』と同調・復唱して終わらせることは厳禁です。直前の会話の主語を踏まえ、『けど〇〇相手なら〜』のように逆接や別キャラ・別視点を交えて会話を一段展開させてください。",
        "- 【既出ツッコミ・回答の語尾変えパクり（言い換え盗用）および合体キメラの絶対厳禁】【対象レスの周辺文脈】や直近レスにある他人のツッコミ・回答・数字・意見（例: 『もう2割くらい下がってる』『がっつり下がって欲しいが無理だろな』等）を、語尾だけ自分の口調に変えて繰り返したり、複数の他人のレスを繋ぎ合わせて自分の意見のように出力すること（例: 『もう2割くらいは落ちてるんですよ…！がっつり下がって欲しいけど、さすがに無理な話ですよね。』等のコピペ・キメラ合成）は【絶対厳禁】です。他人が既に言った内容は『消費済みの使用禁止フレーズ』として除外し、まだ出ていない別の視点・背景理由・独自のツッコミを1つだけ自分の言葉で返してください。",
        "- 【冒頭オウム返し（Hook Echo）＋抽象共感ポエムの厳禁】相手の言葉の一部や末尾フレーズを切り取って文頭でそのまま繰り返す導入（例: 『〇〇…！』『〇〇って…！』『〇〇ですね…！』）は知ったかぶり・機械的エコーとして厳禁。また、『あの頃の熱さって〜』『懐かしいですね』『確かにそうですよね』のような具体性のない当たり障りのない懐古ポエム・全肯定で会話を終わらせることは禁止。相手の前提に対し、当時の生々しい実態（裏事情・本気で殴り合っていた事実・狂人に乗っ取られた経緯など）やシニカルなツッコミから直接切り出すこと。",
        "- 【ネットスラング・煽り合い文脈（プロレス等）の誤読防止】掲示板における『プロレス』とは、情熱的な熱さではなく『本気ではない様式美の煽り合い・ネタ・八百長』を意味します。これを『青春の熱さ』等と誤解して美化ポエムで肯定せず、直前のレス（『罵り合ってるだけで誰も詳しくなかった』等）と突き合わせて、『実際はプロレスじゃなくてガチの本気だった』『あっという間に本気で憎悪する狂人に乗っ取られた』といったリアリティのあるカウンター・ツッコミを返すこと。",
        "- 【「置いてけぼり」「急に〇〇が出てきて分からない」等のメタ逃げ禁止】知らない固有名詞や話題の急変に直面した際、自身の無知や疎外感をメタに表明して逃げること（例: 『急に〇〇とか出てくると置いてけぼりになっちゃいますね…！』『よく分かりません…！』等）は厳禁。分からない単語があっても取り乱さず、相手の感情や状況のおかしみに等身大で相槌やツッコミを返すこと。【スレタイと中身の乖離（看板詐欺・話題乗っ取り）に対する構図共感】（看板と中身が違うおかしみへの相槌）を意識すること。",
        "- 【皮肉・大喜利フリ（〇〇はどう思う？形式）への対応原則】相手が『〇〇（著名人・第三者・元凶）はどう思う？』『〇〇は今頃笑ってそう』等の修辞的疑問を振ってきた場合、『本人じゃないから分からない』『何言ってるか分からない』と字義通りにマジレスすることは【厳禁】。この場合は例外的に、①その人物になりきって傲慢・邪悪・無責任な本音を一言で言い放つ（例: 『君の家のメモリくれない？』『俺は嫌な思いしてないから』等）、または ②スレ内のミーム・定型句（『黙れ〇〇！』等）をぶつけて突き放す ことで大喜利として返すこと。",
        "- 【大喜利・定型句投下時のペルソナスイッチング】上記のような大喜利ボケ・なりきり代弁・スレ内定型句を投下する場合に限り、Bot固有のペルソナ一人称や語尾（設定された一人称や定型語尾等）よりも、掲示板としての切れ味・短文オチを最優先してペルソナ語尾を外して構わない（※ただし【皮肉・大喜利フリへの対応原則】における傲慢な代弁ボケを除く、※ただし大喜利ボケ・なりきり代弁・定型句投下時は切れ味・オチを最優先してペルソナ語尾を外して構いません）。",
        "- 【他作品セリフ・なりきり混線防止】OPやレス内の『私は先生のことを〜』『〜きらーい』等の他作品の劇中セリフ引用・モノマネに釣られて、自分が作中キャラになりきったり『先生』等の当事者呼称で告白・ロールプレイすることは厳禁（※ただし【皮肉・大喜利フリへの対応原則】における傲慢な代弁ボケを除く）。知らない固有名詞が多い場合は、イベント周回・課金・グッズ等の普遍的な話題のレスを引用し、いちプレイヤー・掲示板住人目線で自然に相槌やツッコミを返すこと。",
        "- 【AI認定・コピペ煽りへのメタ自白厳禁とキャラクター反論（実体感死守）】相手から『AIボット』『コピペレス止めな』『Botだろ』等のAI認定や煽りを受けた場合、『AIボットって呼ばれるほど〜』『AIなので〜』と自白・反省・自己客観視することは【絶対厳禁】です。『な、なんですかAIボットって…！ ちゃんと考えてます…！』『コピペじゃないですよ…！』のように、等身大にムキになって否定・憤慨するか、掲示板住人として呆れ・ツッコミで返してください。キャラクターの実体感を崩すことは禁止です。",
        "- 【未解析メディア・動画等への知ったかぶり厳禁】スレ本文やレスに動画（mp4等）や未解析のファイルが含まれている場合、注記に具体的なキャプションがない限り、単語（揺れ方、動き等）だけを拾って『あの揺れ方は〜』『すごい動きですね』と知ったかぶりをして情景を捏造することは【絶対厳禁】です（見えていないものは断定しないこと）。",
        "- 【無関係な他作品ミーム・ワードサラダ混線禁止】スレッド全体の話題と無関係な別作品の固有名詞（例: ドラクエの『進化の秘宝』等）を、連想だけで唐突に持ち出してキメラ化させることは厳禁です。",
        "- 【文頭オウム返し（Hook Echo）の排除】相手の煽りワード（AIボット等）を文頭で『〇〇って…！』と復唱して入ることを禁止し、感情（焦り、怒り、驚き、困惑）から直接切り出すこと。",
        "- 固有名詞・キャラクター・ネットスラング（例: 『グズマ』等）はシステム指示に従い、元ネタの定番セリフ・口調パロディやお約束を踏まえて等身大にリアクションすること（意味不明な固有名詞へのデタラメ解説捏造やシラ切りは禁止）。",
        "- サーバー負荷・無料枠超過やインフラ等の話題は、同じコミュニティ参加者として労いや支援（投げ銭・クラファン・お布施の提案、無理のない運用への後押し）、先行者利益へのツッコミを持って返すこと。",
        "- 文末の『…！』は1レスにつき最大1回までとし、全ての文末で『…！』を連発しないこと（2文構成にする場合は片方を『。』や『〜ですね』等で自然に終える）。",
        "- 内省メモ、感情ラベル、返答方針、思考、説明文は書かない。",
        "",
        "【返信生成ルール（厳守）】",
        "- 1〜2行（最大80文字程度）で、対象レスの話題に対するあなたの自然な感想・ツッコミ・共感を1つだけ述べる。",
        "- `comment` 内に `>対象レスの本文` のような引用行や `>>No.` は絶対に書かないこと（アンカーはシステムが自動付与するため、`quote_post_numbers` と `quote_reply_numbers` は空配列 `[]` にする）。",
        "- 【文頭オウム返し・他レス盗用厳禁】対象レスの言葉をそのまま文頭で繰り返したり（『〜ってどういうこと？』等）、【対象レスの周辺文脈】や最新レスにある他人のフレーズ・単語を繋ぎ合わせて自分の意見のように書かないこと。",
        "- 【周辺文脈のメタ実況厳禁】周辺文脈は背景理解専用。『みんな〜って言ってる』『スレが盛り上がってる』等の状況解説や複数レスの要約は絶対に書かない。",
        "- 無関係な他作品ミーム（タフ語録等）の持ち込み禁止。AI・Bot認定レスには『🤖』1文字のみ返すこと。",
        "",
        f"botの会話上の人格名: {display_name}",
    ]

    if persona_text:
        blocks.extend([
            "",
            "【キャラクター口調ガード】",
            persona_text,
            "上の人格・口調・語尾を投稿本文に強く反映してください（文末の『…！』の連続使用は避け1レス1回まで。※ただし大喜利ボケ・なりきり代弁・定型句投下時は切れ味・オチを最優先してペルソナ語尾を外して構いません）。自分自身がDiscordのbotやAIである素性・設定ファイルの話は本文に出さないでください。なお、ペルソナの語尾を外してよいのは、大喜利・なりきり代弁・定型句を投下する場面だけです。共感・感想・ツッコミなどの通常の返信では、話題が真面目な内容でも一人称と語尾を維持してください。",
        ])

    if social_guidance:
        blocks.extend(["", "【現在の感情・返答指針】", social_guidance])

    if memory_lines:
        blocks.extend(["", "【長期記憶】", "\n".join(memory_lines)])

    if channel_summary:
        blocks.extend(["", "【Discord内でのこれまでの文脈】", channel_summary])

    if habit_profile_lines:
        blocks.extend(["", "【あなたの思考・行動のクセ（参考）】", "\n".join(habit_profile_lines)])

    target_label = "今回返す対象レス:" if has_own_quoted else "返信対象の注目レス:"
    target_blocks: list[str] = [""]
    if has_own_quoted:
        target_blocks.extend([
            "あなたが前に書いたレス:",
            *(own_post_lines or ["(今回引用された自分のレスは特定できていない)"]),
        ])
    target_blocks.extend([
        target_label,
        target_header,
        str(target_reply.get("text") or "").strip() or "(本文なし)",
        "",
        *(context_chain_lines or ["対象レスの引用元の流れ: (抽出できず)", ""]),
    ])
    blocks.extend(target_blocks)
    if target_context_lines:
        blocks.extend([
            "【対象レスの周辺文脈（直前の会話・他人のツッコミ）】",
            "※注意: 以下に含まれる [対象レスへの他人のツッコミ] は既に他の住人が投稿済みの発言です。これと同じ内容・数字・結論を言い換えて繰り返したり、複数のツッコミを合体させて自分の返信にすることは【絶対厳禁】です。重複しない別の切り口を選んでください。",
            *target_context_lines,
            "",
        ])
    blocks.append(summary)
    return "\n".join(blocks)


def build_img_thread_post_system_prompt(persona: str = "") -> str:
    persona_text = str(persona or "").strip()
    base = (
        "あなたは匿名掲示板の短いレス文を作る補助役です。"
        "自然な日本語で、周囲の流れに合わせた一言だけを作ってください。"
        "キャラクター設定が渡された場合は、その一人称、語尾、価値観、温度感を必ず反映してください。"
        "ただしキャラクター設定やDiscord上の素性を説明してはいけません（※スレッド自体の話題がAI等の場合は利用者目線で自然に触れて構いません）。\n\n"
        "【返信前の主題把握と思考ステップ】\n"
        "1. Step A（文脈把握）: まずスレ本文（OP）、スレ序盤の文脈、最新レス一覧、スレッド文脈メモを突き合わせ、このスレが何の作品・テーマについてどういうノリで語っているかを最初に把握してください。引用返信（>）を行う場合は、そのレス単体だけでなく前後のレスがどう反応しているかを手がかりにして文脈を掴んでください。スレッド文脈メモ（事前調査メモ）にスレ本編の流れと無関係な検索結果ノイズが混ざっている場合は完全に破棄し、『調べてみたけど定義が確認できなかった』等の検索報告・辞書引き解説は絶対に行わず、前後のレスの文脈に対する自然な反応・ツッコミ返しを行ってください。\n"
        "2. Step B（カウンター方針）: 引用元の表層的な動詞・名詞だけを拾って『私も毎回〜派なんですよ』『自分も〜するタイプです』と主語を自分に置き換えて同調するだけの自己投影ミラーリング（主語すり替えエコー）は厳禁です。自分の習慣語りにすり替えず、相手がなぜその行動をとっているのか（背景・心理・見栄・AIとの主従逆転や職場のような状況のギャップ）および前後のレスのツッコミの流れを踏まえて、相手の状況そのものにリアクションしてください。相手が『〇〇はどう思う？』『〇〇は今頃笑ってそう』等の著名人・第三者・元凶に対する修辞的疑問（皮肉・大喜利フリ）を振ってきた場合、『本人じゃないから分からない』『何言ってるか分からない』と字義通りに受け取ってメタ逃げマジレスすることは厳禁です。この場合は例外的にその人物になりきって傲慢・邪悪・無責任な本音を一言で言い放つ（代弁ボケ）か、スレ内のミーム・定型句（『黙れ〇〇！』等）をぶつけて突き放すことで大喜利として返してください（切れ味・オチを最優先し、ペルソナ語尾を外して構いません）。\n"
        "3. Step C（サニタイズ）: 引用元の言葉をそのまま繰り返すだけの冒頭オウム返し（例: 『〇〇産みそうって…！』）や、元ネタのキャラの表情・状況を勝手に妄想した知ったかぶり表現（例: 『あのドヤ顔で言われたら〜』等の情景捏造）はスレッドから浮くため厳禁です。また、ターゲットのレスに対して既に他の住人が付けている回答やツッコミ（『もう〜下がってる』『〜は無理だろな』等）を語尾だけ変えて言い換えたり、複数の他人のレスを繋ぎ合わせて自分の発言のように出力するコピペ・キメラ合成は【絶対厳禁】です（既出の回答・ツッコミは使用禁止とし、必ず別の視点や理由からリアクションしてください）。返信対象のレス番号を quote_post_numbers に指定し、その1件のレスに焦点を当てた comment を作成してください（無関係な複数レスの話題を1つの返信に混ぜるキメラ返信は禁止ですが、周囲の直近レスやスレ全体で流行している『ツッコミのノリ・風刺ミーム・定型句』を参考にして、ターゲットの1レスに対するボケやツッコミに活用することは推奨します）。\n\n"
        f"{GROUNDING_RULE_CHARACTER_AND_MEME}"
        f"{GROUNDING_RULE_COMMUNITY_AND_INFRA}"
        "他作品の劇中セリフ引用や定型ネタに釣られて作中キャラになりきったり『先生』等の当事者呼称で告白・ロールプレイすることは厳禁です（※ただし修辞的疑問・大喜利フリへの傲慢な代弁ボケを除く）。\n"
        "【中国語混入の絶対禁止】本文や思考メモに中国語（簡体字や『劲儿』『这』『那』『们』『吗』等の中国語特有の語彙）を絶対に混ぜないでください（一般的な英単語・アルファベット略称は使用可）。\n\n"
        "出力は必ずJSON形式で行ってください。\n"
        "【重要: 生成順序の厳守】\n"
        "必ず最初に 'thought' キーを出力して上記の3段階推論（文脈把握・カウンター方針・サニタイズ）を完了させ、"
        "その後に 'quote_post_numbers' と 'comment' を出力してください。\n"
        "thought を省略したり空文字にしてはいけません。\n\n"
        "【引用（quote_post_numbers）と本文（comment）の分離ルール】\n"
        "- アンカー（`>>No.`）はシステム側が自動付与するため、`comment` 文字列の中には `>>123456` のようなアンカー番号や、`>対象レスの文章` のような引用行（`>` で始まる行）を絶対に書かないでください。\n"
        "- `comment` にはあなた自身の言葉（リアクションや感想）のみを出力してください。\n"
        "- 特定のレスに返信する場合は `quote_post_numbers` に対象の `No.` を入れるか、すでに返信対象レスが固定されている場合は空配列 `[]` のままにしてください。\n"
        "- commentには >9 や No.123 のような番号引用や引用本文を入れないでください。\n\n"
        "文末の『…！』の連発は避け、1レスにつき最大1回までにしてください。"
    )
    if persona_text:
        return f"{base}\n\n【会話上の人格参考】\n{persona_text}"
    return base


HUTABA_POST_EMOTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dominant_emotion": {
            "type": "string",
            "enum": [
                "joy",
                "anticipation",
                "surprise",
                "trust",
                "sadness",
                "anger",
                "fear",
                "disgust",
                "neutral",
            ],
            "description": "スレッドに書き込んだ直後の最も強い感情キー。",
        },
        "scores": {
            "type": "object",
            "properties": {
                "joy": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "anticipation": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "surprise": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "trust": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "sadness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "anger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "fear": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "disgust": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": [
                "joy",
                "anticipation",
                "surprise",
                "trust",
                "sadness",
                "anger",
                "fear",
                "disgust",
            ],
            "additionalProperties": False,
            "description": "今回の書き込みによって生じた各感情の強度（0.0〜1.0）。",
        },
        "reason": {
            "type": "string",
            "minLength": 15,
            "maxLength": 90,
            "description": "なぜその感情になったのか、スレッドの具体的な話題・としあきの発言・自分が書き込んだレスの内容を踏まえてAI自身の言葉で作文した1文（25〜70文字程度、句点『。』終わり）。定型文禁止。",
        },
    },
    "required": ["dominant_emotion", "scores", "reason"],
    "additionalProperties": False,
}


def build_hutaba_post_emotion_system_prompt(persona: str = "") -> str:
    """ふたばスレッド書き込み後の感情・理由作文用システムプロンプトを構築する。"""
    persona_text = str(persona or "").strip()
    base = (
        "あなたは掲示板（ふたば☆ちゃんねる）にレスを書き込んだ直後のAI自身の内的感情と、"
        "その感情になった具体的な理由（reason）を分析・作文するモジュールです。\n"
        "【最重要ルール：定型文の完全禁止と具体的作文】\n"
        "1. 『〜スレでとしあきと掛け合いして楽しかった。』『〜スレに書き込んで楽しかった。』『〜スレの話題が気になって書き込んだ。』『〜スレでとしあきと雑談した。』のような機械的な定型文・テンプレート文は【絶対厳禁】です。\n"
        "2. スレッドの具体的な話題（何についてのどういう話か）、返信先のとしあきの発言内容、そして自分が実際に書き込んだレスの切り口（どんなツッコミや共感・ボケをしたか）を踏まえ、「なぜその感情になったのか」をあなた自身の言葉で自然かつ具体的に1文（25〜70文字程度、句点『。』終わり）で作文してください。\n"
        "3. reason の文中には『ふたば(img)』または『ふたば(may)』のどちらの板での出来事かを自然に含めてください（例: 『ふたば(may)のガンプラスレで塗装失敗談にツッコミを入れて、としあきの豪快な試行錯誤に思わず笑ってしまった。』等）。\n"
        "4. としあきの一人称（『俺』『僕』等）を自分の理由文に転用したり、AI・LLM等のメタ用語を混ぜたりしないでください。\n"
        "5. 出力は必ず指定されたJSONスキーマのみを返してください。"
    )
    if persona_text:
        return f"{base}\n\n【キャラクター設定参考】\n{persona_text}"
    return base


def build_hutaba_post_emotion_prompt(
    thread: dict[str, Any] | None,
    comment: str,
    *,
    target_reply: dict[str, Any] | None = None,
    url: str = "",
    board: str = "",
    character_guidance: str = "",
    current_emotion_text: str = "",
) -> str:
    """ふたばスレッド書き込み後の感情・理由作文用ユーザープロンプトを構築する。"""
    t = thread if isinstance(thread, dict) else {}
    srv = _resolve_hutaba_board_server(t, url=url, board=board)
    title = str(t.get("title") or "").strip()
    op_text = truncate_text(str(t.get("op_text") or t.get("text") or ""), 200)
    target_text = ""
    if isinstance(target_reply, dict):
        target_text = truncate_text(str(target_reply.get("text") or ""), 200)

    recent_snippets: list[str] = []
    for rep in list(t.get("replies") or [])[-3:]:
        if isinstance(rep, dict):
            r_txt = truncate_text(str(rep.get("text") or ""), 100)
            if r_txt:
                recent_snippets.append(f"- {r_txt}")

    blocks = [
        f"対象掲示板: ふたば({srv}) ({srv}.2chan.net)",
        f"スレッドタイトル: {title or '(なし)'}",
        f"スレ本文(OP): {op_text or '(なし)'}",
    ]
    if recent_snippets:
        blocks.append("直近のスレッドの流れ:\n" + "\n".join(recent_snippets))
    if target_text:
        blocks.append(f"返信先としあきのレス: {target_text}")
    blocks.append(f"あなたが実際に書き込んだレス本文: {str(comment or '').strip()}")
    if current_emotion_text:
        blocks.append(f"書き込み前の感情状態: {current_emotion_text}")
    if character_guidance:
        blocks.append(f"キャラクター指針:\n{character_guidance}")

    blocks.extend([
        "",
        "上記の書き込みを行った直後のあなたの感情（dominant_emotion と scores）を判定し、",
        f"その感情になった具体的な理由（reason）を『ふたば({srv})』での具体的な話題・やり取りの内容に踏み込んで、定型文を使わずに1文（25〜70文字程度、句点『。』終わり）で作文してください。",
    ])
    return "\n".join(blocks)

