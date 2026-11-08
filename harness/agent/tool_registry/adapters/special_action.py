"""AIエージェント・ハーネス 特殊アクションツールアダプター (adapters/special_action.py)

要約（Summarize）、わかりやすい解説（Simplify）、これ何？（What is this）、言説検証（Fact check）
などの特殊アクションを ToolDefinition として提供するアダプター。
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from lib.text_utils import normalize_compare_text
from harness.agent.observation.web_evaluator import WebObservationEvaluator
from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.special_action")
_web_evaluator = WebObservationEvaluator()


def is_special_action_parrot(target_text: str, reply_text: str) -> bool:
    """特殊アクションの出力が対象テキストの丸写し（オウム返し）になっていないか判定する。"""
    t = normalize_compare_text(target_text)
    r = normalize_compare_text(reply_text)
    if not t or not r:
        return False
    if t == r:
        return True
    if len(t) >= 8:
        ratio = difflib.SequenceMatcher(None, t, r).ratio()
        if ratio >= 0.75:
            return True
        if t in r and len(t) >= int(len(r) * 0.70):
            return True
        if r in t and len(r) >= int(len(t) * 0.70):
            return True
    return False


def is_special_action_query_hijacked(
    search_query: str,
    target_text: str,
    *,
    user_instruction: str = "",
    min_target_len: int = 6,
) -> bool:
    """検索クエリが対象文章・指示文と無関係（直前雑談等に目的語ハイジャックされた）か判定する。"""
    return _web_evaluator.is_query_hijacked(
        query=search_query,
        subject_text=target_text,
        user_instruction=user_instruction,
        min_target_len=min_target_len,
    )


def sanitize_special_action_query_hallucinations(
    search_query: str,
    target_text: str,
    context_lines: list[str] | None = None,
    *,
    user_instruction: str = "",
    min_target_len: int = 6,
) -> str:
    """検索クエリから対象文章・文脈に存在しない外部幻覚単語を除去・サニタイズする。"""
    return _web_evaluator.sanitize_query_hallucinations(
        query=search_query,
        subject_text=target_text,
        context_lines=context_lines,
        user_instruction=user_instruction,
        min_target_len=min_target_len,
    )


def fallback_extract_keywords(target_text: str, *, max_keywords: int = 3) -> list[str]:
    """外部モデル呼び出しが使えない場合やクエリ抽出失敗時に、対象文章から代表的な単語をフォールバック抽出する。"""
    def _clean(t: str) -> str:
        t = re.sub(r"https?://\S+", "", t)
        t = re.sub(r"[!-/:-@[-`{-~、。！？!?…『』「」（）()【】\[\]\"'“”‘’]+", " ", t)
        return " ".join(t.split()).strip()

    cleaned = _clean(target_text)
    if not cleaned:
        return []

    tokens = [tok for tok in cleaned.split() if len(tok) >= 2]
    keywords: list[str] = []
    seen: set[str] = set()

    for tok in tokens:
        k = tok.lower()
        if k in seen:
            continue
        seen.add(k)
        keywords.append(tok)
        if len(keywords) >= max_keywords:
            break

    return keywords


class SpecialActionToolAdapter:
    """特殊アクションの統合アダプター"""

    @classmethod
    def create_summarize_tool(cls, name: str = "summarize_text", timeout_sec: float = 30.0) -> ToolDefinition:
        """要約ツールの定義を作成する"""
        return ToolDefinition(
            name=name,
            description="指定された長文やメッセージの要点を簡潔にまとめます。",
            parameters=[
                ToolParameter(
                    name="text",
                    type="string",
                    description="要約対象のテキストやメッセージ",
                    required=True,
                )
            ],
            handler=cls.execute_summarize,
            category=ToolCategory.CUSTOM,
            tags={"summarize", "special_action"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def create_simplify_tool(cls, name: str = "simplify_explanation", timeout_sec: float = 30.0) -> ToolDefinition:
        """わかりやすい解説ツールの定義を作成する"""
        return ToolDefinition(
            name=name,
            description="難解な概念や長文を小学生でもわかるように優しく解説します。",
            parameters=[
                ToolParameter(
                    name="text",
                    type="string",
                    description="解説対象のテキストや概念",
                    required=True,
                )
            ],
            handler=cls.execute_simplify,
            category=ToolCategory.CUSTOM,
            tags={"simplify", "special_action"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def create_what_is_this_tool(cls, name: str = "what_is_this", timeout_sec: float = 40.0) -> ToolDefinition:
        """これ何？（単語・概念調査）ツールの定義を作成する"""
        return ToolDefinition(
            name=name,
            description="単語や言説の意味をWeb検索などを活用して調査・解説します。",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="調査対象の単語や言説",
                    required=True,
                )
            ],
            handler=cls.execute_what_is_this,
            category=ToolCategory.CUSTOM,
            tags={"what_is_this", "research", "special_action"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def create_fact_check_tool(cls, name: str = "fact_check", timeout_sec: float = 45.0) -> ToolDefinition:
        """ファクトチェック（真偽検証）ツールの定義を作成する"""
        return ToolDefinition(
            name=name,
            description="指定された言説・主張・ニュースの事実確認と真偽検証をWeb検索と照合して行います。",
            parameters=[
                ToolParameter(
                    name="claim",
                    type="string",
                    description="検証対象の言説・テキスト・主張",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="検索件数の上限（デフォルト: 3）",
                    required=False,
                    default=3,
                ),
            ],
            handler=cls.execute_fact_check,
            category=ToolCategory.CUSTOM,
            tags={"fact_check", "verification", "special_action"},
            timeout_sec=timeout_sec,
        )

    @classmethod
    def create_all_tools(cls) -> list[ToolDefinition]:
        """全特殊アクションツールのリストを作成する"""
        return [
            cls.create_summarize_tool(),
            cls.create_simplify_tool(),
            cls.create_what_is_this_tool(),
            cls.create_fact_check_tool(),
        ]

    @classmethod
    async def execute_summarize(cls, text: str = "", **kwargs: Any) -> str:
        """長文・メッセージの要約を実行する"""
        target_text = str(
            text
            or kwargs.get("content")
            or kwargs.get("target")
            or kwargs.get("input")
            or kwargs.get("user_text")
            or ""
        ).strip()
        if not target_text:
            return "【要約】\n要約対象のテキストが指定されていません。"

        log.info("[special_action] Executing summarize text_len=%d", len(target_text))
        try:
            from ollama_bot.common.ollama_helpers import call_ollama, extract_first_user_facing_reply

            prompt = (
                "以下のテキストの要点を簡潔にまとめ、要約してください。\n"
                "箇条書きやマークダウンは使わず、自然な日本語の文章で要約本文のみを返してください。\n\n"
                f"【対象テキスト】\n{target_text}"
            )
            raw = await call_ollama(prompt, think=False, timeout_sec=25.0)
            cleaned = extract_first_user_facing_reply(raw) if raw else ""
            if cleaned and not is_special_action_parrot(target_text, cleaned):
                return f"【要約】\n{cleaned}"
        except Exception as e:
            log.warning("[special_action] summarize LLM call failed: %r; using fallback", e)

        # フォールバック: 先頭文および主要行の抽出
        lines = [line.strip() for line in target_text.splitlines() if line.strip()]
        fallback_summary = " ".join(lines[:3])[:500] if lines else target_text[:500]
        return f"【要約】\n{fallback_summary}"

    @classmethod
    async def execute_simplify(cls, text: str = "", **kwargs: Any) -> str:
        """難解な概念・文章の平易な解説を実行する"""
        target_text = str(
            text
            or kwargs.get("content")
            or kwargs.get("target")
            or kwargs.get("input")
            or kwargs.get("user_text")
            or ""
        ).strip()
        if not target_text:
            return "【わかりやすい解説】\n解説対象のテキストが指定されていません。"

        log.info("[special_action] Executing simplify text_len=%d", len(target_text))
        try:
            from ollama_bot.common.ollama_helpers import call_ollama, extract_first_user_facing_reply

            prompt = (
                "以下の難解な概念や文章を、小学生でもわかるように優しく平易な言葉で解説してください。\n"
                "専門用語は避け、直感的な例えを用いて自然な文章で返してください。\n\n"
                f"【対象テキスト】\n{target_text}"
            )
            raw = await call_ollama(prompt, think=False, timeout_sec=25.0)
            cleaned = extract_first_user_facing_reply(raw) if raw else ""
            if cleaned and not is_special_action_parrot(target_text, cleaned):
                return f"【わかりやすい解説】\n{cleaned}"
        except Exception as e:
            log.warning("[special_action] simplify LLM call failed: %r; using fallback", e)

        # フォールバック
        return f"【わかりやすい解説】\n{target_text[:500]}"

    @classmethod
    async def execute_what_is_this(cls, query: str = "", **kwargs: Any) -> str:
        """単語・言説の意味調査を実行する"""
        resolved_query = str(
            query
            or kwargs.get("search_query")
            or kwargs.get("keyword")
            or kwargs.get("target")
            or kwargs.get("text")
            or ""
        ).strip()
        if not resolved_query:
            return "【これ何？（調査結果）】\n調査対象のキーワードが指定されていません。"

        context_lines = kwargs.get("context_lines")
        user_instruction = str(kwargs.get("user_instruction") or "")
        
        # クエリの幻覚除去・目的語ハイジャック検査
        if kwargs.get("target_text"):
            resolved_query = sanitize_special_action_query_hallucinations(
                resolved_query,
                str(kwargs.get("target_text")),
                context_lines=context_lines if isinstance(context_lines, list) else None,
                user_instruction=user_instruction,
            )
            if is_special_action_query_hijacked(
                resolved_query,
                str(kwargs.get("target_text")),
                user_instruction=user_instruction,
            ):
                keywords = fallback_extract_keywords(str(kwargs.get("target_text")))
                if keywords:
                    resolved_query = " ".join(keywords)

        log.info("[special_action] Executing what_is_this query=%r", resolved_query)
        try:
            from ..web_research.dispatch import research_dispatch

            result = await research_dispatch(resolved_query, mode="browser_search", max_results=3)
            summary = str(result.get("summary") or "").strip()
            sources = list(result.get("sources") or [])
            
            source_lines: list[str] = []
            for s in sources[:3]:
                if isinstance(s, dict):
                    title = str(s.get("title") or "").strip()
                    url = str(s.get("url") or s.get("href") or "").strip()
                    snippet = str(s.get("snippet") or s.get("content_excerpt") or "").strip()
                    if title or snippet:
                        item_text = f"- {title or '情報源'}: {snippet[:150]}"
                        if url:
                            item_text += f" ({url})"
                        source_lines.append(item_text)

            sources_text = "\n".join(source_lines) if source_lines else "- 関連情報源なし"
            if not summary:
                summary = f"{resolved_query} の調査を完了しました。"

            return (
                f"【これ何？（調査結果）】\n"
                f"キーワード: {resolved_query}\n"
                f"概要: {summary}\n\n"
                f"主な情報源:\n"
                f"{sources_text}"
            )
        except Exception as e:
            log.warning("[special_action] what_is_this research failed: %r", e)
            return f"【これ何？（調査結果）】\nキーワード: {resolved_query}\n概要: 調査中にエラーが発生しました: {e}"

    @classmethod
    async def execute_fact_check(cls, claim: str = "", max_results: int = 3, **kwargs: Any) -> str:
        """ファクトチェック・言説検証を実行する"""
        resolved_claim = str(
            claim
            or kwargs.get("text")
            or kwargs.get("query")
            or kwargs.get("target")
            or ""
        ).strip()
        if not resolved_claim:
            return "【ファクトチェック結果】\n検証対象の言説が指定されていません。"

        log.info("[special_action] Executing fact_check claim=%r", resolved_claim)
        try:
            from ..mcp.fact_check.service import FactCheckService

            service = FactCheckService()
            verification = await service.verify_claim(resolved_claim, max_results=max_results)
            return (
                f"【ファクトチェック結果】\n"
                f"対象言説: {resolved_claim}\n"
                f"判定: {verification.status} (信頼度: {verification.confidence:.1f})\n\n"
                f"{verification.summary}"
            )
        except Exception as e:
            log.warning("[special_action] FactCheckService failed: %r; falling back to research_dispatch", e)
            try:
                from ..web_research.dispatch import research_dispatch

                res = await research_dispatch(resolved_claim, mode="browser_search", max_results=max_results)
                summary = str(res.get("summary") or "").strip() or "関連する検証情報が見つかりませんでした。"
                return (
                    f"【ファクトチェック結果】\n"
                    f"対象言説: {resolved_claim}\n"
                    f"判定: 未確定\n\n"
                    f"{summary}"
                )
            except Exception as dispatch_err:
                log.error("[special_action] fact_check fallback also failed: %r", dispatch_err)
                return f"【ファクトチェック結果】\n対象言説: {resolved_claim}\n検証中にエラーが発生しました: {dispatch_err}"


SpecialActionAdapter = SpecialActionToolAdapter

_is_special_action_parrot = is_special_action_parrot
_is_special_action_query_hijacked = is_special_action_query_hijacked
_sanitize_special_action_query_hallucinations = sanitize_special_action_query_hallucinations
_fallback_extract_keywords = fallback_extract_keywords

