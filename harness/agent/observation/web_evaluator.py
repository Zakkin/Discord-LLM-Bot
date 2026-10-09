"""AIエージェント・ハーネス Web調査観察評価器 (web_evaluator.py)

フェーズ4. 結果の観察と評価（Observation）において、
Web検索・ブラウジングツールの実行結果を精査・評価し、
目的語ハイジャック検知、ノイズ除去、情報十分性の評価（Verdict）、
およびフェーズ2（Thought）やフェーズ5（Final Response）へのフィードバックを生成する。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .types import EvaluationVerdict, Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.web_evaluator")

# 歌詞サイト等のノイズドメイン
_NOISE_DOMAINS = (
    "uta-net.com",
    "utaten.com",
    "j-lyric.net",
    "kashinavi.com",
    "petitlyrics.com",
)

# 一般的な検索補助語（意図を補強する中立的な検索語）
_COMMON_SEARCH_TERMS = frozenset({
    "とは", "意味", "由来", "語源", "公式", "真偽", "事実", "デマ",
    "本当", "嘘", "理由", "wiki", "概要", "詳細", "ニュース",
    "事件", "発言", "元ネタ", "サービス", "定義", "使い方", "使用", "利用", "方法",
    "現在", "最新", "情報", "経緯", "歴史",
})


class WebObservationEvaluator:
    """Web検索・ブラウジング結果の観察・妥当性評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def is_query_hijacked(
        self,
        query: str,
        subject_text: str,
        context_lines: list[str] | None = None,
        *,
        user_instruction: str = "",
        min_target_len: int = 6,
    ) -> bool:
        """検索クエリが直前雑談など無関係な文脈にハイジャックされていないか観察・評価する。

        AGENTS.md 項目75: ファクトチェック・外部調査の目的語ハイジャック遮断
        """
        q = str(query or "").strip().lower()
        if not q:
            return False

        combined_target = f"{subject_text} {user_instruction}".strip()
        norm_target = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", combined_target).lower()

        # 対象文が極端に短い（代名詞等のみ）場合は文脈補完が必要なためハイジャック判定しない
        if len(norm_target) < min_target_len:
            return False
        content_without_pronouns = re.sub(r"(?:これ|それ|あれ|どれ|この|その|あの|どの|本当|ほんと|マジ|嘘|うそ|デマ)", "", norm_target)
        if len(content_without_pronouns) < 3:
            return False

        query_words = [w for w in re.split(r"[\s,]+", q) if len(w) >= 2]
        if not query_words:
            return False

        # 1. クエリの単語のうち、実質的な単語が対象文または指示文に含まれているか
        # ひらがなのみの単語は助詞等の誤一致を防ぐため3文字以上、漢字/カタカナ/英数字を含む単語は2文字以上で判定
        for w in query_words:
            w_lower = w.lower()
            if re.search(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", w_lower):
                if w_lower in norm_target:
                    return False
            elif len(w_lower) >= 3 and w_lower in norm_target:
                return False

        # 2. 漢字・カタカナ・英数字の実質文字単位のオーバーラップを確認
        # （形態素分割の境界差、例えば「ディスコ」と「ディスコード」などを許容するため）
        substantive_query_chars = [
            c for c in q
            if re.match(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", c)
        ]
        overlap_substantive_chars = sum(1 for c in substantive_query_chars if c in norm_target)
        if overlap_substantive_chars >= 2:
            return False

        self.log.warning(
            "[web_evaluator] Query hijack detected: query=%r has no overlap with subject=%r",
            query,
            subject_text,
        )
        return True

    def sanitize_query_hallucinations(
        self,
        query: str,
        subject_text: str,
        context_lines: list[str] | None = None,
        *,
        user_instruction: str = "",
        min_target_len: int = 6,
    ) -> str:
        """クエリから対象文章・指示文・文脈に存在しない無関係な幻覚単語を除去・サニタイズする。"""
        raw_q = str(query or "").strip()
        if not raw_q:
            return ""

        combined_target = f"{subject_text} {user_instruction}".strip()
        norm_target = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", combined_target).lower()
        if len(norm_target) < min_target_len:
            return raw_q

        # 対象文が「作中」「これ」「それ」など、主語・対象作品が文脈に依存している場合は過度な除去を行わない
        content_without_pronouns = re.sub(
            r"(?:これ|それ|あれ|どれ|この|その|あの|どの|本当|ほんと|マジ|嘘|うそ|デマ|作中|あの人|あいつ|彼|彼女)",
            "",
            norm_target,
        )
        substantive_target_chars = [c for c in content_without_pronouns if re.match(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", c)]
        if len(substantive_target_chars) < 6 or any(kw in norm_target for kw in ("作中", "この人", "あの人", "これって", "それって")):
            return raw_q

        norm_context = ""
        if context_lines:
            norm_context = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", " ".join(context_lines)).lower()

        query_words = [w for w in re.split(r"[\s,]+", raw_q) if w]
        if not query_words:
            return ""

        valid_words: list[str] = []
        for word in query_words:
            w_norm = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", word).lower()
            if not w_norm:
                continue

            # 1. 一般的な検索補助語（意味、由来、公式等）は許容
            if w_norm in _COMMON_SEARCH_TERMS:
                valid_words.append(word)
                continue

            # 2. 対象文（または指示文）に含まれているか確認
            is_in_target = False
            if re.search(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", w_norm):
                if w_norm in norm_target:
                    is_in_target = True
                else:
                    substantive_w_chars = [c for c in w_norm if re.match(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", c)]
                    if len(substantive_w_chars) >= 2 and sum(1 for c in substantive_w_chars if c in norm_target) >= 2:
                        is_in_target = True
                    elif len(substantive_w_chars) == 1 and substantive_w_chars[0] in norm_target:
                        is_in_target = True
            elif len(w_norm) >= 3 and w_norm in norm_target:
                is_in_target = True

            if is_in_target:
                valid_words.append(word)
                continue

            # 3. 文脈に含まれている場合は許容（完全外部単語ではない）
            if norm_context and (w_norm in norm_context):
                valid_words.append(word)
                continue

            # 対象文にも文脈にもない完全な外部幻覚単語を除外
            self.log.warning(
                "[web_evaluator] Stripped hallucinated query word: word=%r query=%r subject=%r",
                word,
                raw_q,
                subject_text,
            )

        if not valid_words:
            return ""

        return " ".join(valid_words).strip()


    def filter_clean_results(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """検索結果リストから歌詞サイトやノイズURL、空結果を除去して正規化する。"""
        cleaned: list[dict[str, Any]] = []
        for r in results:
            href = str(r.get("href") or "").lower()
            title = str(r.get("title") or "").strip()
            body = str(r.get("body") or "").strip()

            if not title and not body:
                continue

            # ノイズドメイン除外
            if any(domain in href for domain in _NOISE_DOMAINS):
                self.log.info("[web_evaluator] Filtered noise domain result: %s", href)
                continue

            cleaned.append(r)
        return cleaned

    def evaluate_observations(
        self,
        query: str,
        sources: list[dict[str, Any]],
        min_sources: int = 1,
    ) -> tuple[Observation, EvaluationVerdict]:
        """検索・ブラウジング結果を観察し、Observationと評価判定（Verdict）を返す。"""
        cleaned_sources = self.filter_clean_results(sources)

        if not cleaned_sources:
            obs = Observation(
                tool_name="web_search",
                success=True,
                status=ObservationStatus.EMPTY_RESULT,
                output="検索結果が見つかりませんでした。",
                data={"sources": []},
            )
            return obs, EvaluationVerdict.NEED_MORE_INFO

        # 結果のテキスト合成
        lines: list[str] = []
        for s in cleaned_sources[:5]:
            title = str(s.get("title") or "").strip()
            body = str(s.get("body") or "").strip()
            href = str(s.get("href") or "").strip()
            parts = [p for p in (title, body, f"URL: {href}" if href else "") if p]
            if parts:
                lines.append("- " + " / ".join(parts))

        combined_text = "\n".join(lines)
        obs = Observation(
            tool_name="web_search",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=combined_text,
            data={"sources": cleaned_sources, "query": query},
        )

        verdict = (
            EvaluationVerdict.GOAL_ACHIEVED
            if len(cleaned_sources) >= min_sources
            else EvaluationVerdict.NEED_MORE_INFO
        )
        return obs, verdict
