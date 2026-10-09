"""ファクトチェック MCP 型定義 (mcp/fact_check/types.py)

ファクトチェック、URL要約、Web検索に関連する型定義およびデータ構造を提供する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ReplyActionType = Literal["fact_check", "summarize", "what_is_this", "simplify"]
ReplyInvestigateMode = ReplyActionType

# 明示的な真偽確認を最優先し、その次に要約を優先します。
# 「教えて」「解説して」は表現として広いので最後に回して誤爆を減らします。
ACTION_KEYWORDS: dict[ReplyActionType, tuple[str, ...]] = {
    "fact_check": (
        "ファクトチェック",
        "factcheck",
        "検証して",
        "検証",
        "裏取り",
        "裏を取",
        "本当か",
        "本当",
        "ほんと",
        "ホント",
        "真偽",
        "嘘",
        "デマ",
        "ガセ",
        "事実か",
    ),
    "summarize": (
        "要約",
        "要点",
        "まとめて",
        "まとめ",
        "短くして",
        "短めに",
        "一言で",
        "3行",
        "三行",
        "3文",
        "三文",
        "ざっくり",
        "かいつまんで",
        "あらすじ",
        "tldr",
        "tl;dr",
    ),
    "what_is_this": (
        "これ何",
        "これなに",
        "何これ",
        "なにこれ",
        "これは何",
        "これはなに",
        "って何",
        "ってなに",
        "て何",
        "てなに",
        "とは何",
        "とはなに",
        "ってなんだ",
        "てなんだ",
        "って誰",
        "て誰",
        "ってどこ",
        "てどこ",
        "ってどういうこと",
        "ってどういう意味",
        "とは",
        "何者",
        "だれ",
        "誰",
        "どこの",
        "調べて",
        "検索して",
        "検索",
        "リサーチして",
        "詳細",
        "ソース",
        "出典",
    ),
    "simplify": (
        "わかりやすく",
        "分かりやすく",
        "かみくだいて",
        "噛み砕いて",
        "やさしく",
        "優しく",
        "初心者向け",
        "簡単に説明",
        "平たく言うと",
        "どういう意味",
        "どういうこと",
        "解説して",
        "説明して",
        "詳しく教えて",
        "kwsk",
    ),
}


@dataclass(slots=True)
class FactCheckTargetContext:
    original_text: str
    link_summaries: list[str]
    enriched_text: str
    search_query_source_text: str
    link_context_block: str


@dataclass(slots=True)
class FactCheckResult:
    """ファクトチェック検証結果の構造化データ"""
    query: str
    status: Literal["verified", "disputed", "unverified", "error"]
    summary: str
    sources: list[dict[str, str]] = field(default_factory=list)
    raw_search_results: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "status": self.status,
            "summary": self.summary,
            "sources": self.sources,
            "raw_search_results": self.raw_search_results,
            "confidence": self.confidence,
        }
