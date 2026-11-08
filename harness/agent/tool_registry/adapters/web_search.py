"""AIエージェント・ハーネス Web検索ツールアダプター (adapters/web_search.py)

Web検索およびブラウジング調査機能を ToolDefinition として提供するアダプター。
Chrome MCP（ポート12306）によるブラウザ検索を優先実行し、障害時は simple_search へ多重防御フォールバックする。
対話相手名（target_name）誤Web検索ガード（User-First Resolution）を配備。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.web_search")

# 検索クエリ引数の揺らぎ候補キー
_QUERY_FALLBACK_KEYS = (
    "search_query",
    "keyword",
    "keywords",
    "q",
    "text",
    "query_text",
    "search_text",
    "search_term",
    "input",
    "prompt",
    "target",
)

# 雑談・相槌・親密度・感情・関係性に関するキーワード（対話相手名誤爆検索検知用）
_CONVERSATIONAL_CHAT_PHRASES: tuple[str, ...] = (
    "好きだよね",
    "好き",
    "すき",
    "大好き",
    "脈アリ",
    "脈あり",
    "脈なし",
    "脈ナシ",
    "仲良し",
    "仲いい",
    "仲が良い",
    "相性",
    "ランキング",
    "両思い",
    "片思い",
    "片思",
    "両思",
    "付き合",
    "彼氏",
    "彼女",
    "カップル",
    "告白",
    "どう思う",
    "どう思って",
    "教えて",
    "可愛い",
    "かわいい",
    "かっこいい",
    "照れ",
    "ツンデレ",
    "推し",
    "関係",
    "雑談",
    "会話",
    "相槌",
)


def extract_partner_names(
    context: dict[str, Any] | Any = None,
    **kwargs: Any,
) -> tuple[str, set[str]]:
    """context や kwargs から対話相手名（target_name）および別名リスト（other_names）を安全に抽出する。"""
    target_name = ""
    other_names: set[str] = set()

    if isinstance(context, dict):
        for key in (
            "target_name",
            "reply_target_user",
            "author_display_name",
            "user_name",
            "partner_name",
        ):
            val = context.get(key)
            if val and str(val).strip():
                target_name = str(val).strip()
                break

        for okey in ("other_names", "partner_names", "partner_name_list"):
            raw_other = context.get(okey)
            if isinstance(raw_other, (list, tuple, set)):
                for n in raw_other:
                    if n and str(n).strip():
                        other_names.add(str(n).strip())
            elif isinstance(raw_other, str) and raw_other.strip():
                other_names.add(raw_other.strip())

    if not target_name:
        for key in (
            "target_name",
            "reply_target_user",
            "author_display_name",
            "user_name",
            "partner_name",
        ):
            val = kwargs.get(key)
            if val and str(val).strip():
                target_name = str(val).strip()
                break

    for okey in ("other_names", "partner_names", "partner_name_list"):
        raw_kw_other = kwargs.get(okey)
        if isinstance(raw_kw_other, (list, tuple, set)):
            for n in raw_kw_other:
                if n and str(n).strip():
                    other_names.add(str(n).strip())
        elif isinstance(raw_kw_other, str) and raw_kw_other.strip():
            other_names.add(raw_kw_other.strip())

    return target_name, other_names


def is_partner_name_query(
    query: str,
    target_name: str,
    other_names: set[str] | list[str] | None = None,
) -> tuple[bool, str]:
    """クエリが対話相手の名前を含み、かつ雑談の相槌語や親密度語句と組み合わされた誤爆検索であるかを判定する。

    Returns:
        (is_blocked, matched_name)
    """
    if not query:
        return False, ""

    from lib.text_utils import extract_urls
    if extract_urls(query):
        return False, ""

    candidate_names: list[str] = []
    if target_name and target_name.strip():
        candidate_names.append(target_name.strip())
    if other_names:
        for on in other_names:
            if on and str(on).strip() and str(on).strip() not in candidate_names:
                candidate_names.append(str(on).strip())

    if not candidate_names:
        return False, ""

    q_lower = query.lower()
    q_stripped = query.strip()

    for name in candidate_names:
        name_lower = name.lower()
        if not name_lower:
            continue

        if name_lower not in q_lower:
            continue

        # 1. 名前単体、または敬称（さん、くん、ちゃん、氏等）が付いただけの場合
        suffix_clean = re.sub(
            r"^(?:さん|くん|君|ちゃん|氏|どの|殿)?$",
            "",
            q_stripped.replace(name, "").strip(),
        )
        if not suffix_clean:
            return True, name

        # 2. 雑談相槌語・親密度語句が含まれている場合
        for phrase in _CONVERSATIONAL_CHAT_PHRASES:
            if phrase.lower() in q_lower:
                return True, name

    return False, ""


class WebSearchToolAdapter:
    """Web検索ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "search_web", timeout_sec: float = 40.0) -> ToolDefinition:
        """search_web ツール定義を作成する（デフォルトタイムアウト: 40秒）"""
        return ToolDefinition(
            name=name,
            description="インターネットを検索して最新の情報や不明な単語の意味を調べます。最新ニュース、事実確認、数値や設定に基づく影響・被害・結果のシミュレーション・検証（〜したらどうなる？ 人体への影響は？ 等）の客観的調査が必要な場合に使用してください。",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="検索キーワード、または閲覧対象のWeb/URL（例: '最新のAIニュース', 'https://example.com/article'）",
                    required=False,
                ),
                ToolParameter(
                    name="queries",
                    type="array",
                    description="複数の検索キーワードリスト（比較検討や多面的調査用。最大3件）",
                    required=False,
                ),
            ],
            handler=cls.execute_search,
            category=ToolCategory.WEB,
            tags={"web", "search", "research"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def _resolve_queries(
        cls,
        query: str = "",
        queries: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """queries 配列または query 文字列（および kwargs 内の揺らぎキー）から空文字・重複を除去したクエリリスト（最大3件）を返す。"""
        raw_list: list[str] = []
        if queries:
            if isinstance(queries, (list, tuple)):
                for q in queries:
                    if q and str(q).strip():
                        raw_list.append(str(q).strip())
            elif isinstance(queries, str) and queries.strip():
                raw_list.append(queries.strip())

        resolved_single = str(query or "").strip()
        if resolved_single:
            raw_list.append(resolved_single)

        for key in ("queries", "search_queries"):
            val = kwargs.get(key)
            if val and isinstance(val, (list, tuple)):
                for q in val:
                    if q and str(q).strip():
                        raw_list.append(str(q).strip())

        for key in _QUERY_FALLBACK_KEYS:
            val = kwargs.get(key)
            if val is not None:
                if isinstance(val, (list, tuple)):
                    for q in val:
                        if q and str(q).strip():
                            raw_list.append(str(q).strip())
                elif str(val).strip():
                    raw_list.append(str(val).strip())

        seen: set[str] = set()
        deduped: list[str] = []
        for q in raw_list:
            if q not in seen:
                seen.add(q)
                deduped.append(q)
                if len(deduped) >= 3:
                    break

        return deduped

    @classmethod
    def _resolve_query(cls, query: str = "", **kwargs: Any) -> str:
        """後方互換用: 単一クエリを解決して返す。"""
        resolved_list = cls._resolve_queries(query=query, **kwargs)
        return resolved_list[0] if resolved_list else ""

    @classmethod
    def _format_search_observation(cls, query: str | list[str], result: Any) -> str:
        """後続フェーズ（Observation / Final Response）向け構造化テキストに整形する。

        【Web検索結果】
        検索キーワード: <query>
        概要: <summary>
        主な情報源・解説:
        - <タイトル>: <本文スニペット/要約> (<URL>)
        """
        if isinstance(query, (list, tuple)):
            query_display = ", ".join(str(q).strip() for q in query if str(q).strip())
        else:
            query_display = str(query or "").strip()
        query_display = query_display or "(なし)"

        if not isinstance(result, dict):
            text = str(result or "").strip()
            if not text:
                return (
                    f"【Web検索結果】\n"
                    f"検索キーワード: {query_display}\n"
                    f"概要: 該当する情報は見つかりませんでした。\n"
                    f"主な情報源・解説:\n"
                    f"- 該当する情報は見つかりませんでした"
                )
            return (
                f"【Web検索結果】\n"
                f"検索キーワード: {query_display}\n"
                f"概要: {text}\n"
                f"主な情報源・解説:\n"
                f"- 検索結果メモ: {text}"
            )

        summary = str(result.get("summary") or "").strip()
        sources = list(result.get("sources") or [])
        error = str(result.get("error") or "").strip()

        source_lines: list[str] = []
        seen_urls: set[str] = set()
        for s in sources:
            if not isinstance(s, dict):
                continue
            title = str(s.get("title") or "").strip()
            url = str(s.get("url") or s.get("href") or "").strip()
            snippet = str(
                s.get("content_excerpt")
                or s.get("snippet")
                or s.get("body")
                or s.get("content_text")
                or ""
            ).strip()

            snippet = " ".join(snippet.split())
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."

            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)

            if title or snippet:
                display_title = title or "Web情報"
                desc_part = f": {snippet}" if snippet else ""
                url_part = f" ({url})" if url else ""
                source_lines.append(f"- {display_title}{desc_part}{url_part}")

        if not summary:
            if source_lines:
                summary = f"{query_display} に関するWeb検索結果を取得しました。"
            elif error:
                summary = f"検索中にエラーが発生しました: {error}"
            else:
                summary = "該当する情報は見つかりませんでした。"

        if not source_lines:
            sources_text = "- 該当する情報は見つかりませんでした"
        else:
            sources_text = "\n".join(source_lines[:5])

        return (
            f"【Web検索結果】\n"
            f"検索キーワード: {query_display}\n"
            f"概要: {summary}\n"
            f"主な情報源・解説:\n"
            f"{sources_text}"
        )

    @classmethod
    async def execute_search(
        cls,
        query: str = "",
        queries: list[str] | None = None,
        *,
        is_fact_check: bool | None = None,
        context: dict[str, Any] | Any = None,
        target_name: str | None = None,
        other_names: list[str] | set[str] | str | None = None,
        **kwargs: Any,
    ) -> str:
        """Web検索を実行し、構造化テキストを返す。

        1. query / queries 引数の揺らぎ解決（kwargs からフォールバック）
        2. 対話相手名（target_name）誤Web検索ガード (User-First Resolution)
        3. Chrome MCP 優先実行 (URL時は browser_read_url、通常時は browser_search)
        4. 障害時の多重防御フォールバック (URL時は直接要約、キーワード時は simple_search)
        5. 検索実行・フォールバック時の構造化ログ出力
        6. Observation 向け構造化テキスト整形
        """
        resolved_queries = cls._resolve_queries(query=query, queries=queries, **kwargs)
        log.info("[tool_registry] Invoking WebSearchToolAdapter resolved_queries=%r", resolved_queries)

        if not resolved_queries:
            return cls._format_search_observation(
                query="",
                result={
                    "summary": "検索キーワードが指定されていません。",
                    "error": "検索キーワードが空です。",
                },
            )

        primary_query = resolved_queries[0]
        query_for_obs: str | list[str] = resolved_queries if len(resolved_queries) > 1 else primary_query

        # 対話相手名（target_name）および雑談相槌語の誤Web検索ガード (User-First Resolution)
        resolved_target_name, resolved_other_names = extract_partner_names(
            context=context or kwargs.get("context"),
            target_name=target_name,
            other_names=other_names,
            **kwargs,
        )
        for check_q in resolved_queries:
            blocked, matched_name = is_partner_name_query(
                check_q, target_name=resolved_target_name, other_names=resolved_other_names
            )
            if blocked:
                guidance = (
                    f"『{matched_name}』は現在の対話相手（Discordユーザー）の名前であり、外部Web検索の対象ではありません。"
                    "目の前の相手との会話文脈に基づいて応答してください。"
                )
                log.info(
                    "[tool_registry] Partner name search blocked: query=%r target_name=%r",
                    check_q,
                    matched_name,
                )
                log.info(
                    "[web-search] Executed query=%r mode=%s fallback_used=%s result_chars=%d",
                    primary_query,
                    "blocked_partner_name",
                    False,
                    len(guidance),
                )
                return guidance

        from lib.text_utils import extract_urls
        from harness.agent.tool_registry.web_research.config import ResearchDispatchMode
        has_url = bool(extract_urls(primary_query)) or any(bool(extract_urls(q)) for q in resolved_queries)
        target_mode: ResearchDispatchMode = "browser_read_url" if has_url else "browser_search"

        dispatch_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in (
                "mode", "user_text", "query", "queries", "is_fact_check",
                "context", "target_name", "other_names", "reply_target_user",
                "author_display_name", "user_name", "partner_name", "partner_names",
            ) and k not in _QUERY_FALLBACK_KEYS
        }
        if is_fact_check is not None:
            dispatch_kwargs["is_fact_check"] = is_fact_check

        if context is not None:
            if isinstance(context, str):
                dispatch_kwargs["context"] = context
            elif isinstance(context, dict):
                dispatch_kwargs["context"] = str(context.get("conversation_text") or context.get("text") or "")

        try:
            from harness.agent.tool_registry.web_research.dispatch import research_dispatch

            # 1. Chrome MCP 優先実行 (URL時は browser_read_url、通常時は browser_search)
            log.info(
                "[tool_registry] Attempting %s via Chrome MCP queries=%r (has_url=%s)",
                target_mode,
                resolved_queries,
                has_url,
            )
            result: Any = None
            try:
                result = await research_dispatch(
                    user_text=primary_query,
                    queries=resolved_queries,
                    mode=target_mode,
                    **dispatch_kwargs,
                )
            except Exception as mcp_err:
                log.warning(
                    "[tool_registry] Chrome MCP %s exception for queries=%r: %s",
                    target_mode,
                    resolved_queries,
                    mcp_err,
                )
                result = None

            # 2. フォールバック判定 (未起動・エラー・結果なし等の場合にフォールバックへ)
            need_fallback = False
            if result is None:
                need_fallback = True
            elif isinstance(result, dict):
                has_error = bool(result.get("error"))
                has_sources = bool(result.get("sources"))
                has_web = bool(result.get("used_web"))
                if has_error or (not has_sources and not has_web):
                    need_fallback = True
            else:
                need_fallback = True

            if need_fallback:
                log.info(
                    "[web-search] Executed query=%r mode=%s fallback_used=%s result_chars=%d",
                    primary_query,
                    target_mode,
                    True,
                    len(cls._format_search_observation(query_for_obs, result)) if result else 0,
                )
                if has_url:
                    log.info(
                        "[tool_registry] Chrome MCP browser_read_url failed or returned error for URL query=%r; preserving URL direct read and preventing search engine fallback",
                        primary_query,
                    )
                    # URL時は検索エンジン検索（simple_search）へ流さず（URL検索誤爆防止・ban_list #1763遵守）、
                    # build_link_summaries_from_text 等による直接取得を試みるか、あるいは既存のエラー/結果を保全
                    if result is None or (isinstance(result, dict) and result.get("error")):
                        try:
                            from harness.agent.tool_registry.mcp.fact_check import build_link_summaries_from_text
                            summaries = await build_link_summaries_from_text(primary_query, max_links=2)
                            if summaries:
                                result = {
                                    "used_web": True,
                                    "used_browser": False,
                                    "summary": "\n".join(summaries),
                                    "sources": [
                                        {"title": "Web Link Summary", "url": u, "snippet": s}
                                        for u, s in zip(extract_urls(primary_query), summaries)
                                    ],
                                    "mode": "browser_read_url",
                                }
                        except Exception as link_err:
                            log.warning("[tool_registry] Direct link summary fallback exception: %s", link_err)
                    if result is None:
                        result = {
                            "error": "URLの読み取りに失敗しました。",
                            "summary": f"指定されたURLの閲覧・取得に失敗しました: {primary_query}",
                        }
                else:
                    log.info(
                        "[tool_registry] Chrome MCP browser_search failed or returned error; falling back to simple_search queries=%r",
                        resolved_queries,
                    )
                    try:
                        fallback_result = await research_dispatch(
                            user_text=primary_query,
                            queries=resolved_queries,
                            mode="simple_search",
                            **dispatch_kwargs,
                        )
                        if isinstance(fallback_result, dict):
                            if fallback_result.get("sources") or fallback_result.get("used_web") or not result:
                                result = fallback_result
                        elif not result:
                            result = fallback_result
                    except Exception as fallback_err:
                        log.warning(
                            "[tool_registry] Fallback simple_search exception queries=%r: %s",
                            resolved_queries,
                            fallback_err,
                        )
                        if result is None:
                            result = {
                                "error": str(fallback_err),
                                "summary": f"検索に失敗しました: {fallback_err}",
                            }

            formatted_output = cls._format_search_observation(query_for_obs, result)
            executed_mode = target_mode
            if need_fallback and not has_url:
                executed_mode = "simple_search"
            log.info(
                "[web-search] Executed query=%r mode=%s fallback_used=%s result_chars=%d",
                primary_query,
                executed_mode,
                need_fallback,
                len(formatted_output),
            )
            return formatted_output

        except Exception as e:
            log.exception("[tool_registry] WebSearchToolAdapter unexpected error: %s", e)
            err_output = cls._format_search_observation(
                query_for_obs,
                {"error": str(e), "summary": f"検索実行中にエラーが発生しました: {e}"},
            )
            log.info(
                "[web-search] Executed query=%r mode=%s fallback_used=%s result_chars=%d",
                primary_query,
                target_mode,
                True,
                len(err_output),
            )
            return err_output

    # 互換性エイリアス: WebSearchToolAdapter.execute
    execute = execute_search
