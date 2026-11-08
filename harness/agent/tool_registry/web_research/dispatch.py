"""
web_research/dispatch.py

このファイルは各リサーチモード（ウェブ検索、ブラウザ直接閲覧、Xタイムライン、IMGスレッド等）の
オーケストレーション（実行の制御）を行います。
主な責務:
- LLMによる検索プランニング・検索結果の要約
- 各モードごとの固有の処理とフォールバックハンドリング
- research_dispatch 関数の提供
- エラーハンドリングと最終結果の構成

AIエージェントが新しい調査機能を追加する場合はこのファイルを修正してください。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_int
from ollama_bot.common.http_session import get_shared_http_session
from lib.text_utils import extract_urls
from harness.agent.tool_registry.mcp.fact_check import build_link_summaries_from_text
from ollama_bot.common.ollama_helpers import call_ollama_json, truncate_text
from .config import (
    ResearchDispatchMode,
    _BROWSER_QUERY_SCHEMA,
    _BROWSER_QUERY_SYSTEM_PROMPT,
    _BROWSER_SUMMARY_SCHEMA,
    _BROWSER_SUMMARY_SYSTEM_PROMPT,
    _DEFAULT_MAX_CRAWL_URLS,
    _DEFAULT_WEB_RESEARCH_SUMMARY_NUM_PREDICT,
    _DEFAULT_X_TIMELINE_HARD_LIMIT,
    _NO_RESULTS_TEXT,
    _safe_browser_page_limit,
    _safe_result_limit,
    _safe_x_timeline_limit,
    _x_timeline_extra_wait_sec,
)
from .helpers import (
    _choose_query,
    _compact_whitespace,
    _is_login_wall_or_error_page_fallback,
    _is_noise_domain,
    _is_safe_public_browser_url_resolved,
    _is_trusted_fact_domain,
    _query_terms,
    _remember_known_terms_from_text,
    detect_reply_research_rules,
    extract_core_noun_for_query_refinement,
    extract_quoted_terms,
    generate_refined_queries,
    looks_like_fact_check_or_verification,
    resolve_research_mode,
)
from .parsing import (
    _build_result,
    _build_source,
    _dedupe_sources,
    _extract_browser_page,
    _extract_raw_text_from_tool_result,
    _extract_tab_id,
    _extract_text_from_tool_result,
    _format_sources_summary,
    _normalize_search_source,
    _iter_tool_result_payloads,
    _extract_search_entries_from_tool_result,
    _extract_search_entries_from_html,
    _build_read_page_args,
)
from .client import (
    MCPChromeHTTPClient,
    _browser_operation_guard,
    _browser_debug_enabled,
    _build_navigate_args,
    _close_browser_tab_if_possible,
    _get_shared_mcp_client,
    _is_invalid_mcp_session_error,
    _is_mcp_transport_already_connected_error,
    _mcp_tool_map,
    _read_browser_content_with_retries,
    _read_search_results_with_browser,
    _refresh_x_timeline_tab_if_reused,
    _should_close_browser_tab_after_navigation,
    _sleep_after_browser_navigation,
    _browser_result_preview,
)

# 実行時に動的にインポートされるヘルパーモジュール
_xcom: Any = None
_img_helper: Any = None

log = logging.getLogger("ollama_bot.common.web_research")

def _init_helpers(xcom: Any, img_helper: Any) -> None:
    global _xcom, _img_helper
    _xcom = xcom
    _img_helper = img_helper


_MCP_CHROME_UNAVAILABLE_ERROR = (
    "mcp-chrome が起動していないか、拡張機能が未接続です（127.0.0.1:12306 に接続不可）。"
)

def _is_login_wall_or_error_page(content_text: str) -> bool:
    if _xcom and hasattr(_xcom, "is_login_wall_or_error_page"):
        return bool(_xcom.is_login_wall_or_error_page(content_text))
    return _is_login_wall_or_error_page_fallback(content_text)

def _browser_dispatch_error_message(error_text: str) -> str:
    compacted = _compact_whitespace(error_text)
    lowered = compacted.lower()
    if (
        "connectionrefused" in lowered
        or "connection refused" in lowered
        or "connect call failed" in lowered
    ) and ("127.0.0.1" in lowered or "12306" in lowered):
        return _MCP_CHROME_UNAVAILABLE_ERROR
    if "cannot connect to host" in lowered and ("127.0.0.1" in lowered or "12306" in lowered):
        return _MCP_CHROME_UNAVAILABLE_ERROR
    if _is_invalid_mcp_session_error(lowered):
        return "mcp-chrome のセッションが切れたため、ブラウザ調査に失敗しました。"
    if _is_mcp_transport_already_connected_error(lowered):
        return "mcp-chrome が別セッションに接続済みのため、ブラウザ調査に失敗しました。"
    return "ブラウザ調査に失敗しました。"


async def _search_web_entries_with_mcp(query: str, *, max_results: int) -> list[dict[str, str]]:
    client = _get_shared_mcp_client()
    result = await _read_search_results_with_browser(client, query, max_results=max_results)
    return _extract_search_entries_from_tool_result(result, max_results=max_results)


async def _read_url_with_browser(
    client: MCPChromeHTTPClient,
    url: str,
    *,
    source: dict[str, Any] | None = None,
    query: str | list[str] = "",
) -> dict[str, Any]:
    if not await _is_safe_public_browser_url_resolved(url):
        raise RuntimeError(f"Blocked unsafe browser target URL: {url}")

    async with _browser_operation_guard():
        tools = await client.list_tools()
        tool_map = _mcp_tool_map(tools)
        if "chrome_navigate" not in tool_map or "chrome_get_web_content" not in tool_map:
            raise RuntimeError("Required Chrome MCP tools are not available")

        navigate_result = await client.call_tool(
            "chrome_navigate",
            _build_navigate_args(tool_map, url, prefer_new_window=True),
        )
        tab_id = _extract_tab_id(navigate_result)
        await _sleep_after_browser_navigation(url, reason="read_url")
        try:
            content_result = await _read_browser_content_with_retries(
                client,
                tool_map,
                url=url,
                tab_id=tab_id,
                preferred_format="text",
                reject_login_wall=True,
            )
        finally:
            if _should_close_browser_tab_after_navigation(navigate_result):
                await _close_browser_tab_if_possible(client, tool_map, tab_id)

    page = _extract_browser_page(url, content_result, query=query)
    content_text = page.get("content_text") or ""
    if _is_login_wall_or_error_page(content_text):
        log.warning(
            "browser read produced login-wall/error content url=%s page_url=%s title=%r text_len=%s preview=%r",
            url,
            page.get("url") or "",
            page.get("title") or "",
            len(content_text),
            _browser_result_preview(content_text, fallback_chars=220) if _browser_debug_enabled() else "",
        )
        raise RuntimeError(f"Login wall or error page detected for URL: {url}")
    merged = _build_source(
        source_type="browser",
        title=page.get("title") or str((source or {}).get("title") or "").strip(),
        url=page.get("url") or url,
        snippet=str((source or {}).get("snippet") or "").strip(),
        content_text=page.get("content_text") or "",
        content_excerpt=page.get("content_excerpt") or "",
        used_in_answer=True,
    )
    return merged


def _is_pdf_url(url: str) -> bool:
    """URLがPDF直リンク（.pdfまたは/_pdf）であるかを判定する。"""
    if not url:
        return False
    u = url.strip().lower()
    return bool(re.search(r"(\.pdf(?:[?#].*)?$|/_pdf(?:/|[?#]|$))", u))


def _browser_source_quality(
    source: dict[str, Any],
    query: str | list[str] = "",
    *,
    is_fact_check: bool = False,
) -> int:
    score = 0
    excerpt = str(source.get("content_excerpt") or "").strip()
    content_text = str(source.get("content_text") or "").strip()
    title = str(source.get("title") or "").strip()
    url = str(source.get("url") or source.get("href") or "").strip()

    # Chrome MCPでテキスト抽出不能なPDF直リンクは大幅減点して巡回を抑止
    if _is_pdf_url(url):
        return -50

    score += min(len(excerpt) // 120, 4)
    score += min(len(content_text) // 400, 4)
    lowered = f"{title} {excerpt}".lower()
    for term in _query_terms(query):
        if term.lower() in lowered:
            score += 2

    # ノイズドメインと信頼できる百科事典/公的ドメインの評価
    if _is_trusted_fact_domain(url):
        score += 8
    elif _is_noise_domain(url):
        score -= 15 if is_fact_check else 10

    return score


def _prioritize_candidate_sources(
    candidate_sources: list[dict[str, Any]],
    query: str | list[str] = "",
    *,
    is_fact_check: bool = False,
) -> list[dict[str, Any]]:
    """検索候補ソースを信頼性・関連性に基づいて並び替える。
    信頼できる百科事典や公式サイトがある場合、一般掲示板・知恵袋・成人漫画サイト等の
    ノイズドメインや、テキスト抽出が困難なPDF直リンクの優先度を大幅に下げる。
    """
    if not candidate_sources:
        return []

    # PDF直リンク以外の候補が存在する場合は、PDF直リンクをブラウザ巡回対象から除外（無駄な3回リトライを防止）
    non_pdf = [s for s in candidate_sources if not _is_pdf_url(str(s.get("url") or s.get("href") or ""))]
    targets = non_pdf if non_pdf else candidate_sources

    def _source_priority(s: dict[str, Any]) -> tuple[int, int]:
        url = str(s.get("url") or s.get("href") or "").strip()
        title = str(s.get("title") or "").strip()
        snippet = str(s.get("snippet") or s.get("body") or "").strip()

        tier = 1
        if _is_pdf_url(url):
            tier = -1
        elif _is_trusted_fact_domain(url):
            tier = 3
        elif _is_noise_domain(url):
            tier = 0

        term_matches = 0
        lowered = f"{title} {snippet}".lower()
        for term in _query_terms(query):
            if term.lower() in lowered:
                term_matches += 1

        return (tier, term_matches)

    return sorted(targets, key=_source_priority, reverse=True)


def _rank_and_merge_serp_sources(
    query_sources_map: list[list[dict[str, Any]]] | list[dict[str, Any]],
    queries: list[str] | str,
    *,
    is_fact_check: bool = False,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """複数クエリSERPの交差適合ボーナス・RRF（Reciprocal Rank Fusion）ランキング。

    各クエリの順位に応じたRRFスコア（1 / (k + rank)）を合算し、
    複数クエリで出現したソースに交差適合ボーナス（出現クエリ数 - 1）を加算する。
    さらにドメイン信頼性（百科事典・公的機関）やノイズドメイン判定を加味して最終順位を決定する。
    """
    if not query_sources_map:
        return []

    resolved_queries = [queries] if isinstance(queries, str) else list(queries)

    normalized_serp_lists: list[list[dict[str, Any]]] = []
    first_item = query_sources_map[0]
    if isinstance(first_item, list):
        for item in query_sources_map:
            if isinstance(item, list):
                normalized_serp_lists.append([s for s in item if isinstance(s, dict)])
    elif isinstance(first_item, dict):
        if "sources" in first_item:
            for item in query_sources_map:
                if isinstance(item, dict) and isinstance(item.get("sources"), list):
                    normalized_serp_lists.append([s for s in item["sources"] if isinstance(s, dict)])
        else:
            normalized_serp_lists.append([s for s in query_sources_map if isinstance(s, dict)])

    if not normalized_serp_lists:
        return []

    def _get_identity(s: dict[str, Any]) -> str:
        url = str(s.get("url") or s.get("href") or "").strip()
        if url:
            return url.lower()
        title = str(s.get("title") or "").strip()
        return title.lower()

    sources_by_key: dict[str, dict[str, Any]] = {}
    query_appearance_count: dict[str, int] = {}
    rrf_scores: dict[str, float] = {}

    for query_idx, sources in enumerate(normalized_serp_lists):
        seen_in_query: set[str] = set()
        for rank, source in enumerate(sources, start=1):
            key = _get_identity(source)
            if not key:
                continue

            if key not in seen_in_query:
                seen_in_query.add(key)
                query_appearance_count[key] = query_appearance_count.get(key, 0) + 1
                rrf_scores[key] = rrf_scores.get(key, 0.0) + (1.0 / (rrf_k + rank))

            if key not in sources_by_key:
                sources_by_key[key] = dict(source)
            else:
                curr = sources_by_key[key]
                for f in ("snippet", "content_excerpt", "content_text"):
                    if not curr.get(f) and source.get(f):
                        curr[f] = source.get(f)

    scored_items: list[tuple[float, dict[str, Any]]] = []
    for key, source in sources_by_key.items():
        base_rrf = rrf_scores.get(key, 0.0)
        appearances = query_appearance_count.get(key, 1)

        # 交差適合ボーナス (2つ以上のクエリでヒットした場合に大きく加算)
        intersection_bonus = (appearances - 1) * 0.05

        url = str(source.get("url") or source.get("href") or "").strip()
        domain_modifier = 0.0
        if _is_pdf_url(url):
            domain_modifier -= 0.1
        elif _is_trusted_fact_domain(url):
            domain_modifier += 0.02
        elif _is_noise_domain(url):
            domain_modifier -= 0.05 if is_fact_check else 0.03

        term_bonus = 0.0
        title_snippet = f"{source.get('title', '')} {source.get('snippet', '')}".lower()
        for q in resolved_queries:
            for term in _query_terms(q):
                if term.lower() in title_snippet:
                    term_bonus += 0.002

        final_score = base_rrf + intersection_bonus + domain_modifier + term_bonus
        source["rrf_score"] = final_score
        source["query_appearances"] = appearances
        scored_items.append((final_score, source))

    scored_items.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored_items]


def build_current_datetime_context() -> str:
    """サーバの現在日時コンテキスト文字列を生成する。"""
    from lib.date_utils import now_jst
    now = now_jst()
    return (
        f"【現在日時コンテキスト】 現在のサーバ時刻は {now.year}年{now.month}月{now.day}日 です。\n"
        f"（※ {now.year}年などの記述は未来の架空日付ではなく現在の有効な日付です）"
    )


_QUERY_PROMPT_EXAMPLE_TERMS: tuple[str, ...] = ("かじゅき",)


def _extract_context_speaker_labels(context: str) -> set[str]:
    """補助文脈から発言者の表示名・ラベルを抽出する。"""
    if not context:
        return set()
    speakers: set[str] = set()
    for line in context.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(r"^\[[^\]]+\]\s*", "", line)
        m = re.match(r"^([^\s:()（）]+)(?:\s*[\(（][^\)）]*[\)）])?\s*[:：]", cleaned)
        if m:
            spk = m.group(1).strip()
            if spk and len(spk) >= 2:
                speakers.add(spk)
    return speakers


def _sanitize_browser_search_queries(
    queries: list[str],
    user_text: str,
    context: str,
) -> list[str]:
    """生成された検索クエリから、user_text に出現しない話者名・文脈ラベル・プロンプト例示語を除去する。"""
    if not queries:
        return []

    user_text_lower = user_text.lower()
    stop_terms: set[str] = set()

    # 1. context 由来の話者名
    context_speakers = _extract_context_speaker_labels(context)
    for spk in context_speakers:
        if spk.lower() not in user_text_lower:
            stop_terms.add(spk)

    # 2. 定番の役割ラベル
    for default_role in ("assistant", "system", "user", "bot", "speaker"):
        if default_role not in user_text_lower:
            stop_terms.add(default_role)

    # 3. プロンプト例示語（user_text になければ除去）
    for ex in _QUERY_PROMPT_EXAMPLE_TERMS:
        if ex.lower() not in user_text_lower:
            stop_terms.add(ex)

    if not stop_terms:
        return queries

    stop_terms_lower = {st.lower() for st in stop_terms}
    sanitized_queries: list[str] = []
    for q in queries:
        tokens = q.split()
        cleaned_tokens: list[str] = []
        for tok in tokens:
            stripped = tok.strip(" \"'「」『』()（）[]【】:：")
            if stripped.lower() in stop_terms_lower:
                continue
            cleaned_tokens.append(tok)
        cleaned_query = " ".join(cleaned_tokens).strip()
        if cleaned_query and cleaned_query not in sanitized_queries:
            sanitized_queries.append(cleaned_query)

    return sanitized_queries


def _build_browser_observation_prompt(query: str, context: str, sources: list[dict[str, Any]]) -> str:
    dt_context = build_current_datetime_context()
    source_blocks: list[str] = []
    for idx, source in enumerate(sources, start=1):
        source_blocks.append(
            f"[{idx}] タイトル: {source.get('title') or '(なし)'}\n"
            f"URL: {source.get('url') or '(なし)'}\n"
            f"検索抜粋: {truncate_text(str(source.get('snippet') or ''), 240)}\n"
            f"本文観測: {truncate_text(str(source.get('content_text') or source.get('content_excerpt') or ''), 1800)}"
        )
    return (
        f"{dt_context}\n\n"
        "以下はブラウザで観測したWeb調査メモです。\n"
        "後段の会話モデルが事実に基づいて正確に回答できるよう、単なる1文のあらすじに丸めず、以下の要素を具体名を落とさずに構造的にまとめてください:\n"
        "- ① 時期・作品名・登場枠などの前提事実\n"
        "- ② 行動や事象の理由・経緯・段階的な因果関係（なぜそうなったのか）\n"
        "- ③ 話題となった特有のエピソードや俗称・空耳・ネットスラングの真偽や位置づけ\n"
        "事実の断定は観測できた範囲に限り、情報が不足している場合のみ insufficient=true とし、その理由を insufficiency_reason に60文字以内で簡潔に書いてください（insufficient=false の場合は必ず空文字 \"\" にしてください）。\n\n"
        "【検索クエリ】\n"
        f"{query or '(なし)'}\n\n"
        "【補助文脈】\n"
        f"{truncate_text(_compact_whitespace(context), 500) or '(なし)'}\n\n"
        "【観測メモ】\n"
        f"{chr(10).join(source_blocks) if source_blocks else '(なし)'}"
    )


async def _generate_browser_search_plan(user_text: str, context: str, *, max_results: int) -> dict[str, Any]:
    from lib.date_utils import now_jst
    now = now_jst()
    date_header = (
        f"【現在日時コンテキスト】 現在のサーバ時刻は {now.year}年{now.month}月{now.day}日 です。\n"
        f"最新情報・近況・ニュースの調査では必要に応じて年号（{now.year}年）を検索語にバインドしてください。\n"
        f"多角的な調査（比較・仕様・評判・複合質問等）が必要な場合は、視点を変えた2〜3個のクエリ（queries）に展開してください。"
    )
    prompt = (
        f"{date_header}\n\n"
        f"ユーザー質問:\n{user_text}\n\n"
        f"補助文脈:\n{context}\n\n"
        f"【検索計画の要件】\n"
        f"1. 主張・要素分解（Claim Decomposition Fan-out）: 単なるキーワード羅列を避け、「①基本設定・時期・前提」「②動機・理由・経緯・背景」「③特有の俗称・スラング・評判」など複数の観点に分解して2〜3個のクエリを作成してください。\n"
        f"2. スラング・固有表記の保持: 質問の核となっている俗称・ネットミーム・空耳・カギ括弧等の特有表現は言い換えずそのままクエリに含めてください。\n"
        f"3. 話者名・文脈ラベル混入禁止: 「補助文脈」に登場する会話参加者の表示名・発言者ラベル（「Assistant」や「話者:」）やプロンプト内の説明語句は、「ユーザー質問」本体に書かれていない限り検索クエリに絶対に含めないでください。\n"
        f"max_results={max_results}です。"
    )
    try:
        raw_plan = await call_ollama_json(
            prompt,
            system_prompt=_BROWSER_QUERY_SYSTEM_PROMPT,
            model=str(cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
            think=False,
            schema=_BROWSER_QUERY_SCHEMA,
            timeout_sec=float(cfg("WEB_RESEARCH_PLAN_TIMEOUT_SEC", 8.0) or 8.0),
            retries=1,
            temperature=0.1,
            num_predict=128,
        )
        queries_val = raw_plan.get("queries")
        queries: list[str] = []
        if isinstance(queries_val, list):
            for q in queries_val:
                q_str = str(q or "").strip()
                if q_str and q_str not in queries:
                    queries.append(q_str)
        fallback_query = str(raw_plan.get("query") or "").strip()
        if not queries and fallback_query:
            queries.append(fallback_query)

        # ポストフィルタ: 話者名・文脈ラベル・プロンプト例示語のサニタイズ
        queries = _sanitize_browser_search_queries(queries, user_text, context)

        if not queries:
            ch_query = _choose_query(user_text, context)
            if ch_query:
                queries.append(ch_query)

        # スラング・固有表記の保持（多重防御: 引用符やカギ括弧の語がクエリに含まれていない場合に追加）
        quoted_terms = extract_quoted_terms(user_text)
        if quoted_terms:
            for q_term in quoted_terms:
                already_included = any(q_term.lower() in q.lower() for q in queries)
                if not already_included:
                    core_noun = extract_core_noun_for_query_refinement(user_text, context)
                    slang_query = f"{core_noun} {q_term}".strip() if core_noun and core_noun != q_term else q_term
                    if len(queries) < 3:
                        queries.append(slang_query)
                    elif queries:
                        queries[-1] = slang_query

        queries = _sanitize_browser_search_queries(queries, user_text, context)
        queries = queries[:3]
        return {
            "queries": queries,
            "query": queries[0] if queries else "",
            "open_top_n": int(raw_plan.get("open_top_n") or 3),
            "recency_priority": bool(raw_plan.get("recency_priority", False)),
        }
    except Exception as e:
        log.warning("browser search plan generation failed; using fallback: %r", e)
        fallback_query = _choose_query(user_text, context)
        fb_queries = [fallback_query] if fallback_query else []
        quoted_terms = extract_quoted_terms(user_text)
        if quoted_terms:
            for q_term in quoted_terms:
                if not any(q_term.lower() in q.lower() for q in fb_queries):
                    core_noun = extract_core_noun_for_query_refinement(user_text, context)
                    slang_query = f"{core_noun} {q_term}".strip() if core_noun and core_noun != q_term else q_term
                    fb_queries.append(slang_query)
        fb_queries = _sanitize_browser_search_queries(fb_queries, user_text, context)
        fb_queries = fb_queries[:3]
        return {
            "queries": fb_queries,
            "query": fb_queries[0] if fb_queries else fallback_query,
            "open_top_n": 3,
            "recency_priority": False,
        }


async def _summarize_browser_observations(
    query: str,
    context: str,
    sources: list[dict[str, Any]],
) -> tuple[str, float, str]:
    if not sources:
        return _NO_RESULTS_TEXT, 0.0, ""

    try:
        result = await call_ollama_json(
            _build_browser_observation_prompt(query, context, sources),
            system_prompt=_BROWSER_SUMMARY_SYSTEM_PROMPT,
            model=str(cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
            think=False,
            schema=_BROWSER_SUMMARY_SCHEMA,
            timeout_sec=float(cfg("WEB_RESEARCH_SUMMARY_TIMEOUT_SEC", 12.0) or 12.0),
            retries=0,
            temperature=0.1,
            num_predict=max(
                cfg_int("WEB_RESEARCH_SUMMARY_NUM_PREDICT", _DEFAULT_WEB_RESEARCH_SUMMARY_NUM_PREDICT),
                512,
            ),
        )
        summary = str(result.get("summary") or "").strip()
        confidence = max(0.0, min(float(result.get("confidence") or 0.0), 1.0))
        insufficiency_reason = str(result.get("insufficiency_reason") or "").strip()
        if bool(result.get("insufficient")) and insufficiency_reason:
            summary = f"{summary}\n不足: {insufficiency_reason}".strip()
        if summary:
            return summary, confidence, insufficiency_reason
    except Exception as e:
        log.warning("browser observation summarization failed; using fallback summary: %r", e)

    return _format_sources_summary(sources, no_results_text=_NO_RESULTS_TEXT), 0.78 if sources else 0.0, ""


async def _orchestrate_browser_search(
    *,
    user_text: str,
    context: str,
    max_results: int,
    is_fact_check: bool = False,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    is_fact_check = bool(is_fact_check or looks_like_fact_check_or_verification(user_text, context))
    plan: dict[str, Any] = {}
    resolved_queries: list[str] = []
    if queries:
        for q in queries:
            qs = str(q or "").strip()
            if qs and qs not in resolved_queries:
                resolved_queries.append(qs)

    if not resolved_queries:
        plan = await _generate_browser_search_plan(user_text, context, max_results=max_results)
        plan_queries = plan.get("queries")
        if isinstance(plan_queries, list):
            for q in plan_queries:
                qs = str(q or "").strip()
                if qs and qs not in resolved_queries:
                    resolved_queries.append(qs)
        fallback_q = str(plan.get("query") or "").strip()
        if not resolved_queries and fallback_q:
            resolved_queries.append(fallback_q)

    if not resolved_queries:
        ch = _choose_query(user_text, context)
        if ch:
            resolved_queries.append(ch)

    if not resolved_queries:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="検索クエリを作れませんでした。",
            confidence=0.0,
            mode="browser_search",
            error="検索クエリを作れませんでした。",
        )

    primary_query = resolved_queries[0]
    search_candidate_limit = _safe_result_limit(max(max_results, cfg_int("WEB_RESEARCH_BROWSER_MAX_RESULTS_TO_OPEN", 5)))
    
    candidate_sources: list[dict[str, Any]] = []
    simple_results: list[dict[str, Any]] = []
    query_sources_map: list[list[dict[str, Any]]] = []
    for q in resolved_queries:
        sr = await _simple_search_dispatch(q, max_results=search_candidate_limit, is_fact_check=is_fact_check)
        simple_results.append(sr)
        query_sources_map.append(list(sr.get("sources") or []))

    # 複数クエリSERPの交差適合ボーナス・RRFランキングまたは重複排除＋優先度ソート
    if len(resolved_queries) > 1:
        candidate_sources = _rank_and_merge_serp_sources(
            query_sources_map,
            resolved_queries,
            is_fact_check=is_fact_check,
        )
    else:
        flat_sources = [s for sublist in query_sources_map for s in sublist]
        candidate_sources = _dedupe_sources(flat_sources)
        candidate_sources = _prioritize_candidate_sources(candidate_sources, primary_query, is_fact_check=is_fact_check)

    # 検索結果不十分時のクエリリファイン（フォールバック 2nd pass）
    if not candidate_sources or all(_is_noise_domain(str(s.get("url") or "")) for s in candidate_sources):
        refined_queries = generate_refined_queries(primary_query, user_text=user_text, context=context)
        for refined_query in refined_queries:
            if refined_query in resolved_queries:
                continue
            log.info(
                "Browser search 1st pass for %r insufficient; attempting query refinement: %r",
                primary_query,
                refined_query,
            )
            refined_result = await _simple_search_dispatch(refined_query, max_results=search_candidate_limit, is_fact_check=is_fact_check)
            refined_sources = list(refined_result.get("sources") or [])
            if refined_sources:
                primary_query = refined_query
                simple_results.append(refined_result)
                query_sources_map.append(refined_sources)
                resolved_queries.append(refined_query)
                candidate_sources = _rank_and_merge_serp_sources(
                    query_sources_map,
                    resolved_queries,
                    is_fact_check=is_fact_check,
                )
                break

    first_error = next((str(sr.get("error") or "") for sr in simple_results if sr.get("error")), "")
    if first_error and not candidate_sources:
        first_sr = next((sr for sr in simple_results if sr.get("error")), simple_results[0])
        return _build_result(
            used_web=bool(first_sr.get("used_web")),
            used_browser=bool(first_sr.get("used_browser")),
            summary=str(first_sr.get("summary") or ""),
            sources=list(first_sr.get("sources") or []),
            confidence=float(first_sr.get("confidence") or 0.0),
            mode="browser_search",
            query=primary_query,
            error=first_error,
        )

    if not candidate_sources:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=_NO_RESULTS_TEXT,
            confidence=0.0,
            mode="browser_search",
            query=primary_query,
        )

    # 単一クエリ時で未ソートの場合の優先度ソート
    if len(resolved_queries) <= 1:
        candidate_sources = _prioritize_candidate_sources(candidate_sources, primary_query, is_fact_check=is_fact_check)

    if not cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return _build_result(
            used_web=True,
            used_browser=any(bool(sr.get("used_browser")) for sr in simple_results),
            summary=_format_sources_summary(candidate_sources, no_results_text=_NO_RESULTS_TEXT),
            sources=candidate_sources,
            confidence=max((float(sr.get("confidence") or 0.0) for sr in simple_results), default=0.0),
            mode="browser_search",
            query=primary_query,
        )

    client = _get_shared_mcp_client()
    open_top_n = int(plan.get("open_top_n") or (2 if is_fact_check else 3))
    configured_max_open = _safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_RESULTS_TO_OPEN", 5))
    initial_limit = min(max(open_top_n, 1), len(candidate_sources))
    if is_fact_check and len(candidate_sources) >= 2:
        initial_limit = max(initial_limit, 2)

    max_open_limit = max(initial_limit, min(configured_max_open, len(candidate_sources)))

    # Deep Crawl（ページ本文取得）上限クリッピング:
    # 複数クエリの場合は肥大化防止のため _DEFAULT_MAX_CRAWL_URLS (4) に制限
    # 明示的な環境変数 WEB_RESEARCH_MAX_CRAWL_URLS があればそれを最優先
    default_crawl_urls = _DEFAULT_MAX_CRAWL_URLS if len(resolved_queries) > 1 else max_open_limit
    max_crawl_urls = max(cfg_int("WEB_RESEARCH_MAX_CRAWL_URLS", default_crawl_urls), 1)
    initial_limit = min(initial_limit, max_crawl_urls)
    max_open_limit = min(max_open_limit, max_crawl_urls)

    browser_sources: list[dict[str, Any]] = []
    total_observed_chars = 0
    query_for_page: str | list[str] = resolved_queries if len(resolved_queries) > 1 else primary_query

    async def _observe_range(start: int, end: int) -> None:
        nonlocal total_observed_chars
        for source in candidate_sources[start:end]:
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            try:
                observed = await _read_url_with_browser(client, url, source=source, query=query_for_page)
                observed = _build_source(
                    source_type=str(observed.get("source_type") or "browser"),
                    title=str(observed.get("title") or source.get("title") or "").strip(),
                    url=str(observed.get("url") or url).strip(),
                    snippet=str(observed.get("snippet") or source.get("snippet") or "").strip(),
                    content_text=str(observed.get("content_text") or "").strip(),
                    content_excerpt=str(observed.get("content_excerpt") or "").strip(),
                    used_in_answer=True,
                )
                observed["quality"] = _browser_source_quality(observed, query_for_page, is_fact_check=is_fact_check)
                browser_sources.append(observed)
                total_observed_chars += len(str(observed.get("content_text") or observed.get("content_excerpt") or ""))
            except Exception as e:
                log.warning("browser orchestrator read failed url=%s err=%r", url, e)

    await _observe_range(0, initial_limit)

    should_second_pass = cfg_bool("WEB_RESEARCH_BROWSER_SECOND_PASS_ENABLED", True) and (
        len(browser_sources) < 2
        or total_observed_chars < max(cfg_int("WEB_RESEARCH_BROWSER_SECOND_PASS_MIN_TOTAL_CHARS", 1400), 400)
    )
    if should_second_pass and initial_limit < max_open_limit:
        await _observe_range(initial_limit, max_open_limit)

    query_summary_title = ", ".join(resolved_queries) if len(resolved_queries) > 1 else primary_query

    if browser_sources:
        candidate_term = str(((plan.get("rule_info") or {}) if isinstance(plan.get("rule_info"), dict) else {}).get("candidate_term") or "").strip()
        _remember_known_terms_from_text(candidate_term or primary_query)
        browser_sources.sort(key=lambda item: int(item.get("quality") or 0), reverse=True)
        summary, summary_confidence, insufficiency_reason = await _summarize_browser_observations(
            query_summary_title,
            context,
            browser_sources[:max_open_limit],
        )
        # ファクトチェック時、または複数クエリ展開（Claim Decomposition Fan-out）時などに本文生抜粋を結合して保持
        should_append_excerpts = bool(
            is_fact_check
            or len(resolved_queries) > 1
        )
        if should_append_excerpts and browser_sources:
            excerpts: list[str] = []
            max_excerpt_chars_budget = 1000
            current_excerpt_chars = 0
            for s in browser_sources[:3]:
                t = str(s.get("title") or "").strip()
                exc = str(s.get("content_excerpt") or "").strip()
                if exc and exc not in summary:
                    truncated = truncate_text(exc, 450)
                    item_str = f"【{t or '引用元'}】\n{truncated}"
                    if current_excerpt_chars + len(item_str) > max_excerpt_chars_budget:
                        break
                    excerpts.append(item_str)
                    current_excerpt_chars += len(item_str)
            if excerpts:
                summary = f"{summary}\n\n" + "\n\n".join(excerpts) if summary and summary != _NO_RESULTS_TEXT else "\n\n".join(excerpts)

        merged_sources = _dedupe_sources(browser_sources[:max_open_limit] + candidate_sources)
        result = _build_result(
            used_web=True,
            used_browser=True,
            summary=summary,
            sources=merged_sources,
            confidence=max(summary_confidence, 0.78 if browser_sources else 0.0),
            mode="browser_search",
            query=primary_query,
        )
        result["plan"] = {
            "query": primary_query,
            "queries": resolved_queries,
            "initial_limit": initial_limit,
            "max_open_limit": max_open_limit,
            "second_pass_used": bool(should_second_pass and initial_limit < max_open_limit),
        }
        result["insufficiency_reason"] = insufficiency_reason
        return result

    fallback_summary = _format_sources_summary(candidate_sources, no_results_text=_NO_RESULTS_TEXT)
    if context:
        fallback_summary = fallback_summary or truncate_text(_compact_whitespace(context), 500)
    return _build_result(
        used_web=True,
        used_browser=True,
        summary=fallback_summary,
        sources=candidate_sources,
        confidence=max((float(sr.get("confidence") or 0.0) for sr in simple_results), default=0.0),
        mode="browser_search",
        query=primary_query,
    )


def _parse_ddg_lite_html(html_text: str, *, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo Lite (lite.duckduckgo.com/lite/) のHTML結果をパースする。"""
    import html as html_lib
    from urllib.parse import unquote

    link_pattern = re.compile(
        r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*?class=[\"']result-link[\"'][^>]*>(.*?)</a>|"
        r"<a\b[^>]*?class=[\"']result-link[\"'][^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r"<td\b[^>]*?class=[\"']result-snippet[\"'][^>]*>(.*?)</td>",
        flags=re.DOTALL | re.IGNORECASE,
    )

    links: list[tuple[str, str]] = []
    for m in link_pattern.finditer(html_text):
        if m.group(1) is not None:
            href, raw_title = m.group(1), m.group(2)
        else:
            href, raw_title = m.group(3), m.group(4)
        links.append((href, raw_title))

    snippets = snippet_pattern.findall(html_text)

    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    for idx, (raw_href, raw_title) in enumerate(links):
        clean_title = html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        actual_href = raw_href
        m_uddg = re.search(r"uddg=([^&]+)", raw_href)
        if m_uddg:
            actual_href = unquote(m_uddg.group(1))
        elif actual_href.startswith("//"):
            actual_href = "https:" + actual_href

        snippet_text = ""
        if idx < len(snippets):
            snippet_text = html_lib.unescape(re.sub(r"<[^>]+>", "", snippets[idx])).strip()

        if not actual_href or not clean_title or actual_href in seen:
            continue
        seen.add(actual_href)

        entries.append({
            "title": clean_title,
            "body": snippet_text,
            "href": actual_href,
            "source_backend": "http_fallback",
        })
        if len(entries) >= max_results:
            break

    return entries


async def _search_web_entries_http(query: str, *, max_results: int) -> list[dict[str, str]]:
    """Chrome MCP が使用できない場合の HTTP直接検索フォールバック"""
    from urllib.parse import quote_plus

    clean_query = str(query or "").strip()
    if not clean_query:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    try:
        session = await get_shared_http_session(timeout_sec=8.0, headers=headers)

        # 1. 最優先: DuckDuckGo Lite への POST リクエスト（Bot対策・CAPTCHAを回避しやすい）
        try:
            ddg_lite_url = "https://lite.duckduckgo.com/lite/"
            ddg_lite_data = {"q": clean_query, "kl": "jp-jp"}
            async with session.post(ddg_lite_url, data=ddg_lite_data, allow_redirects=True) as resp:
                if resp.status == 200:
                    html_text = await resp.text(errors="ignore")
                    entries = _parse_ddg_lite_html(html_text, max_results=max_results)
                    if entries:
                        log.info(
                            "[web_research] DuckDuckGo Lite POST search succeeded with %d entries for query=%r",
                            len(entries),
                            clean_query,
                        )
                        return entries
        except Exception as e:
            log.debug("DuckDuckGo Lite search attempt failed query=%r: %s", clean_query, e)

        # 2. 次点: html.duckduckgo.com および Google への GET リクエスト
        encoded = quote_plus(clean_query)
        urls = [
            f"https://html.duckduckgo.com/html/?q={encoded}",
            f"https://www.google.com/search?q={encoded}&hl=ja",
        ]
        for url in urls:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        html_text = await resp.text(errors="ignore")
                        entries = _extract_search_entries_from_html(html_text, max_results=max_results)
                        if entries:
                            for entry_item in entries:
                                entry_item["source_backend"] = "http_fallback"
                            return entries
            except Exception as e:
                log.debug("HTTP search attempt failed url=%s: %s", url, e)
    except Exception as e:
        log.warning("HTTP direct search failed query=%r: %s", query, e)
    return []


async def search_web_entries_unified(query: str, *, max_results: int = 3) -> list[dict[str, str]]:
    """外部モジュール向けの統合Web検索公開関数。

    Chrome MCP（最優先）-> HTTP直接検索（フォールバック: DDG Lite POST -> Google / DDG HTML）の
    多重防御チェーンにより BotブロックやCAPTCHAを回避しながら堅牢にWeb検索エントリを取得する。
    """
    clean_query = str(query or "").strip()
    if not clean_query:
        return []

    limit = max(1, min(int(max_results), 20))

    # 1. Chrome MCP 最優先
    try:
        entries = await _search_web_entries_with_mcp(clean_query, max_results=limit)
        if entries:
            log.debug("[search_web_entries_unified] Chrome MCP search succeeded with %d entries", len(entries))
            return entries
    except TimeoutError as e:
        log.warning("[search_web_entries_unified] Chrome MCP search timed out query=%r: %s", clean_query, e)
    except Exception as e:
        log.warning("[search_web_entries_unified] Chrome MCP search failed query=%r: %s", clean_query, e)

    # 2. HTTP直接検索フォールバック（DDG Lite POST -> Google / DDG HTML）
    try:
        entries = await _search_web_entries_http(clean_query, max_results=limit)
        if entries:
            log.info("[search_web_entries_unified] HTTP fallback search succeeded with %d entries", len(entries))
            return entries
    except Exception as e:
        log.warning("[search_web_entries_unified] HTTP direct search failed query=%r: %s", clean_query, e)

    return []


async def _simple_search_dispatch(
    query: str,
    *,
    max_results: int,
    is_fact_check: bool = False,
    allow_refinement: bool = True,
) -> dict[str, Any]:
    max_results = _safe_result_limit(max_results)
    entries: list[dict[str, str]] = []
    mcp_error = ""

    try:
        entries = await _search_web_entries_with_mcp(query, max_results=max_results)
    except TimeoutError as e:
        mcp_error = f"Chrome MCP検索がタイムアウトしました: {e}"
        log.warning("[web_research] %s, attempting HTTP fallback", mcp_error)
    except Exception as e:
        mcp_error = f"Chrome MCP検索エラー: {e}"
        log.warning("[web_research] %s, attempting HTTP fallback", mcp_error)

    if not entries and mcp_error:
        # Chrome MCP 失敗時の HTTP 直接検索フォールバック
        try:
            entries = await _search_web_entries_http(query, max_results=max_results)
            if entries:
                log.info(
                    "[web_research] HTTP fallback search succeeded with %d entries for query=%r",
                    len(entries),
                    query,
                )
        except Exception as e:
            log.warning("[web_research] HTTP fallback search failed: %s", e)

    # 検索結果不十分時のクエリリファイン（フォールバック 2nd pass）
    if allow_refinement and (not entries or all(_is_noise_domain(str(e.get("href") or "")) for e in entries)):
        refined_queries = generate_refined_queries(query)
        for refined_query in refined_queries:
            if refined_query == query:
                continue
            log.info(
                "[web_research] Simple search 1st pass for %r insufficient; trying refined query: %r",
                query,
                refined_query,
            )
            try:
                refined_entries = await _search_web_entries_with_mcp(refined_query, max_results=max_results)
            except Exception:
                refined_entries = await _search_web_entries_http(refined_query, max_results=max_results)
            if refined_entries:
                entries = refined_entries
                query = refined_query
                mcp_error = ""
                break

    if not entries and mcp_error:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=f"検索中にエラーが発生しました: {mcp_error}",
            confidence=0.0,
            mode="simple_search",
            query=query,
            error=f"Chrome MCP検索中にエラーが発生しました。少し待ってからもう一度試してください。詳細: {mcp_error}",
        )

    sources = [_normalize_search_source(entry) for entry in entries]
    sources = _prioritize_candidate_sources(sources, query, is_fact_check=is_fact_check)
    if sources:
        _remember_known_terms_from_text(query)
    summary = _format_sources_summary(sources, no_results_text=_NO_RESULTS_TEXT)
    confidence = 0.55 if sources else 0.0
    return _build_result(
        used_web=bool(sources),
        used_browser=bool(sources),
        summary=summary,
        sources=sources,
        confidence=confidence,
        mode="simple_search",
        query=query,
    )


async def _browser_search_dispatch(
    query: str,
    context: str,
    *,
    max_results: int,
    is_fact_check: bool = False,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    return await _orchestrate_browser_search(
        user_text=query,
        context=context,
        max_results=max_results,
        is_fact_check=is_fact_check,
        queries=queries,
    )


from front.x.timeline import (
    read_x_timeline_with_browser as _read_x_timeline_with_browser,
    x_timeline_entries_from_tool_results as _x_timeline_entries_from_tool_results,
)
from front.x.tools import (
    x_search_dispatch as _x_search_dispatch,
    x_timeline_dispatch as _x_timeline_dispatch,
)


async def _browser_read_url_dispatch(user_text: str, context: str) -> dict[str, Any]:
    combined = "\n".join(part for part in (str(user_text or "").strip(), str(context or "").strip()) if part).strip()
    user_urls = extract_urls(user_text)
    urls = user_urls or extract_urls(combined)
    fallback_text = str(user_text or "").strip() if user_urls else combined
    fallback_query = _choose_query(user_text, context)
    if not urls:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="URLが見つかりませんでした。",
            confidence=0.0,
            mode="browser_read_url",
            error="調査対象のURLが見つかりませんでした。",
        )

    try:
        fallback_summaries = await build_link_summaries_from_text(
            fallback_text,
            max_links=_safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES", 2)),
        )
    except Exception as e:
        log.warning("link summary fallback failed urls=%s err=%r", urls, e)
        fallback_summaries = []
    fallback_sources = [
        _build_source(
            source_type="link_summary",
            title="",
            url="",
            snippet=str(item or "").strip(),
            content_excerpt="",
            used_in_answer=False,
        )
        for item in fallback_summaries
        if str(item or "").strip()
    ]
    try:
        search_fallbacks = await _search_web_entries_with_mcp(fallback_query, max_results=5)
        for s in search_fallbacks:
            fallback_sources.append(
                _build_source(
                    source_type="search_fallback",
                    title=str(s.get("title") or "").strip(),
                    url=str(s.get("href") or "").strip(),
                    snippet=str(s.get("body") or "").strip(),
                )
            )
    except Exception as e:
        log.debug("browser read fallback search failed: %r", e)

    fallback_sources = [
        source
        for source in fallback_sources
        if not _is_login_wall_or_error_page(str(source.get("snippet") or ""))
    ]

    if not cfg_bool("WEB_RESEARCH_BROWSER_ENABLED", True):
        return _build_result(
            used_web=bool(fallback_sources),
            used_browser=False,
            summary=_format_sources_summary(fallback_sources, no_results_text=_NO_RESULTS_TEXT) if fallback_sources else "URLの読み取りに失敗しました。",
            sources=fallback_sources,
            confidence=0.5 if fallback_sources else 0.0,
            mode="browser_read_url",
            query=urls[0],
        )

    browser_sources: list[dict[str, Any]] = []
    client = _get_shared_mcp_client()
    browser_limit = _safe_browser_page_limit(cfg_int("WEB_RESEARCH_BROWSER_MAX_PAGES", 2))
    last_browser_error = ""

    for url in urls[:browser_limit]:
        try:
            observed = await _read_url_with_browser(client, url, query=fallback_query or url)
            browser_sources.append(
                _build_source(
                    source_type=str(observed.get("source_type") or "browser"),
                    title=str(observed.get("title") or "").strip(),
                    url=str(observed.get("url") or url).strip(),
                    snippet=str(observed.get("snippet") or "").strip(),
                    content_text=str(observed.get("content_text") or "").strip(),
                    content_excerpt=str(observed.get("content_excerpt") or "").strip(),
                    used_in_answer=bool(observed.get("used_in_answer", True)),
                    fetched_at=str(observed.get("fetched_at") or "").strip(),
                )
            )
        except Exception as e:
            err_msg = str(e)
            if "Login wall" in err_msg or "error page detected" in err_msg:
                log.info("browser_read_url skipped login-wall url=%s", url)
            else:
                last_browser_error = err_msg
                log.warning("browser_read_url failed url=%s err=%r", url, e)

    if browser_sources:
        merged_sources = _dedupe_sources(browser_sources + fallback_sources)
        return _build_result(
            used_web=True,
            used_browser=True,
            summary=_format_sources_summary(merged_sources, no_results_text=_NO_RESULTS_TEXT),
            sources=merged_sources,
            confidence=0.82,
            mode="browser_read_url",
            query=urls[0],
        )

    return _build_result(
        used_web=bool(fallback_sources),
        used_browser=False,
        summary=_format_sources_summary(fallback_sources, no_results_text=_NO_RESULTS_TEXT) if fallback_sources else "URLの読み取りに失敗しました。",
        sources=fallback_sources,
        confidence=0.5 if fallback_sources else 0.0,
        mode="browser_read_url",
        query=urls[0],
        error="" if fallback_sources else _browser_dispatch_error_message(last_browser_error),
    )


from front.hutaba.tools import (
    img_thread_dispatch as _img_thread_dispatch,
    img_top5_dispatch as _img_top5_dispatch,
)


async def _image_search_dispatch(image_url_or_query: str, context: str = "") -> dict[str, Any]:
    """画像URLまたは画像クエリから画像検索を実行し、統一リサーチ結果形式で返す"""
    from ..adapters.image_search import ImageSearchToolAdapter, extract_image_url_from_context

    clean_target = str(image_url_or_query or "").strip()
    target_url = clean_target if (clean_target.startswith("http://") or clean_target.startswith("https://")) else ""

    if not target_url:
        target_url = extract_image_url_from_context({"user_text": clean_target, "context_text": context})

    log.info("[IMAGE_SEARCH_DISPATCH] Dispatching image search: input='%s', target_url='%s'", clean_target, target_url)

    if not target_url:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="画像検索の対象となる画像URLが見つかりませんでした。",
            confidence=0.0,
            mode="image_search",
            error="no_image_url",
        )

    res = await ImageSearchToolAdapter.search_image_unified(target_url, query=clean_target)
    log.info("[IMAGE_SEARCH_DISPATCH] Result: success=%s, source='%s', lens_url='%s'", res.get("success"), res.get("source"), res.get("lens_url"))
    summary_text = str(res.get("text") or "画像検索結果を取得できませんでした。").strip()
    lens_url = str(res.get("lens_url") or "")

    sources = []
    if lens_url:
        sources.append(_build_source(
            source_type="browser",
            title="Google Lens 画像検索結果",
            url=lens_url,
            snippet=summary_text[:240],
            content_text=summary_text,
            used_in_answer=True,
            source_backend="google_lens",
        ))

    return _build_result(
        used_web=True,
        used_browser=res.get("source") == "google_lens_mcp",
        summary=summary_text,
        sources=sources,
        confidence=0.85 if res.get("success") else 0.3,
        mode="image_search",
        query=target_url,
    )





async def research_dispatch(
    user_text: str,
    context: str = "",
    mode: ResearchDispatchMode = "auto",
    *,
    max_results: int | None = None,
    queries: list[str] | None = None,
    is_fact_check: bool | None = None,
) -> dict[str, Any]:
    normalized_user_text = str(user_text or "").strip()
    normalized_context = str(context or "").strip()
    
    cleaned_queries: list[str] = []
    if queries:
        for q in queries:
            qs = str(q or "").strip()
            if qs and qs not in cleaned_queries:
                cleaned_queries.append(qs)

    if cleaned_queries:
        query = cleaned_queries[0]
    else:
        query = _choose_query(normalized_user_text, normalized_context)
        if query:
            cleaned_queries = [query]

    if is_fact_check is not None:
        effective_is_fact_check = bool(is_fact_check)
    else:
        effective_is_fact_check = bool(
            str(mode).strip().lower() in {"fact_check", "factcheck", "deep", "deep_research"}
            or looks_like_fact_check_or_verification(normalized_user_text, normalized_context)
        )

    resolved_mode = resolve_research_mode(
        normalized_user_text,
        context=normalized_context,
        mode=mode,
        is_fact_check=effective_is_fact_check,
    )
    resolved_max_results = _safe_result_limit(
        max(int(max_results if max_results is not None else cfg_int("WEB_RESEARCH_MAX_RESULTS", 4)), 1)
    )
    resolved_x_timeline_limit = _safe_x_timeline_limit()

    if not query and resolved_mode not in {"browser_read_url", "x_timeline", "img_top5", "img_thread", "image_search"}:
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="検索クエリを作れませんでした。",
            confidence=0.0,
            mode=resolved_mode,
            error="検索クエリを作れませんでした。",
        )
    dispatch_timeout = float(cfg("WEB_RESEARCH_DISPATCH_TIMEOUT_SEC", 60.0) or 60.0)

    async def _execute_mode() -> dict[str, Any]:
        if resolved_mode == "image_search":
            return await _image_search_dispatch(query or normalized_user_text, normalized_context)
        if resolved_mode == "browser_read_url":
            return await _browser_read_url_dispatch(normalized_user_text, normalized_context)
        if resolved_mode == "img_top5":
            return await _img_top5_dispatch(normalized_user_text, normalized_context)
        if resolved_mode == "img_thread":
            return await _img_thread_dispatch(normalized_user_text, normalized_context)
        if resolved_mode == "x_timeline":
            return await _x_timeline_dispatch(
                normalized_user_text,
                normalized_context,
                limit=resolved_x_timeline_limit,
            )
        if resolved_mode == "x_search":
            return await _x_search_dispatch(
                query,
                normalized_context,
                limit=resolved_x_timeline_limit,
                max_results=resolved_max_results,
            )
        if resolved_mode == "browser_search":
            return await _browser_search_dispatch(
                query,
                normalized_context,
                max_results=resolved_max_results,
                is_fact_check=effective_is_fact_check,
                queries=cleaned_queries if queries is not None else None,
            )
        if resolved_mode == "simple_search":
            if cleaned_queries and len(cleaned_queries) > 1:
                query_sources_map: list[list[dict[str, Any]]] = []
                for q in cleaned_queries:
                    res = await _simple_search_dispatch(q, max_results=resolved_max_results, is_fact_check=effective_is_fact_check)
                    query_sources_map.append(list(res.get("sources") or []))
                all_sources = _rank_and_merge_serp_sources(query_sources_map, cleaned_queries, is_fact_check=effective_is_fact_check)
                return _build_result(
                    used_web=bool(all_sources),
                    used_browser=bool(all_sources),
                    summary=_format_sources_summary(all_sources, no_results_text=_NO_RESULTS_TEXT),
                    sources=all_sources,
                    confidence=0.55 if all_sources else 0.0,
                    mode="simple_search",
                    query=", ".join(cleaned_queries),
                )
        return await _simple_search_dispatch(
            query,
            max_results=resolved_max_results,
            is_fact_check=effective_is_fact_check,
        )

    try:
        return await asyncio.wait_for(_execute_mode(), timeout=dispatch_timeout)
    except (asyncio.TimeoutError, TimeoutError) as e:
        log.warning(
            "research_dispatch timed out after %.1fs: mode=%s query=%r err=%r",
            dispatch_timeout,
            resolved_mode,
            query,
            e,
        )
        return _build_result(
            used_web=False,
            used_browser=False,
            summary="Web調査がタイムアウトしました。時間を置いて再度お試しください。",
            confidence=0.0,
            mode=resolved_mode,
            query=query,
            error=f"Web調査がタイムアウトしました（{dispatch_timeout}秒）。",
        )
    except Exception as e:
        log.exception(
            "research_dispatch unexpected error: mode=%s query=%r err=%r",
            resolved_mode,
            query,
            e,
        )
        return _build_result(
            used_web=False,
            used_browser=False,
            summary=f"Web調査中にエラーが発生しました: {e}",
            confidence=0.0,
            mode=resolved_mode,
            query=query,
            error=str(e),
        )


def get_ollama_web_search_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "インターネットを検索して最新の情報や不明な単語の意味を調べます。わからないキーワードがあればまずこれを使ってください。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "検索キーワード（例: '最新のAIニュース', 'Python MCPとは'）"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]
