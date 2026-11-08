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

# 事実確認に不要かつ検索ノイズ（成人向け・愚痴・罵倒等）となるスラング・口語表現（除外対象）
_NOISE_SLANG_WORDS = frozenset({
    "ガキ", "がき", "ガキ共", "ガキども",
    "クソ", "くそ", "糞",
    "カス", "かす",
    "ゴミ", "ごみ",
    "やつ", "ヤツ", "奴",
    "野郎", "やろう",
    "野垂れ死に", "キモい", "きもい",
    "ガイジ", "池沼", "基地外", "キチガイ",
    "バカ", "ばか", "馬鹿",
    "アホ", "あほ",
})


def _levenshtein_distance(s1: str, s2: str) -> int:
    """2つの文字列のレーベンシュタイン距離（編集距離）を計算する。"""
    if s1 == s2:
        return 0
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if not s2:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _split_particles(token: str) -> list[str]:
    """「チョコグの彼氏」「双子のガキ」「彼氏と双子」のように
    漢字・カタカナ・英数字が助詞「の」「と」で結合されている語を分割する。
    """
    parts = re.split(
        r"(?<=[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9])(?:の|と)(?=[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9])",
        token,
    )
    return [p for p in parts if p]


def _correct_katakana_typo(word: str, target_katakana_list: list[str]) -> str:
    """対象テキスト内のカタカナ語ブロックと比較し、
    1文字違い（編集距離1）のタイポである場合に正規表記へ補正する。
    """
    w_clean = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", word)
    if not re.fullmatch(r"[ァ-ヴー]{2,}", w_clean):
        return word

    # 既に完全一致で存在すれば補正不要
    if w_clean in target_katakana_list:
        return word

    best_cand: str | None = None
    min_dist = 999
    for cand in target_katakana_list:
        if cand == w_clean:
            return word
        dist = _levenshtein_distance(w_clean, cand)
        if dist == 1:
            if dist < min_dist:
                min_dist = dist
                best_cand = cand
            elif dist == min_dist and best_cand is not None:
                if abs(len(cand) - len(w_clean)) < abs(len(best_cand) - len(w_clean)):
                    best_cand = cand

    if best_cand is not None and min_dist == 1:
        return best_cand
    return word


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
        """クエリから対象文章・指示文・文脈に存在しない無関係な幻覚単語を除去・サニタイズする。
        カタカナ語タイポの自動補正、結合助詞の分離、検索ノイズスラングの除外を含む。
        """
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

        # 1. 結合助詞の分離（「チョコグの彼氏」「双子のガキ」などを個別トークンに分離）
        raw_words = [w for w in re.split(r"[\s,]+", raw_q) if w]
        if not raw_words:
            return ""

        query_words: list[str] = []
        for rw in raw_words:
            query_words.extend(_split_particles(rw))

        # 対象文中のカタカナ語ブロックを抽出（タイポ引き戻し補正用）
        target_katakana_blocks = re.findall(r"[ァ-ヴー]{2,}", combined_target)
        unique_target_katakana = list(dict.fromkeys(target_katakana_blocks))

        valid_words: list[str] = []
        for word in query_words:
            w_norm = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", word).lower()
            if not w_norm:
                continue

            # 2. スラング・罵倒表現の除去（「ガキ」「クソ」等）
            if w_norm in _NOISE_SLANG_WORDS:
                self.log.info(
                    "[web_evaluator] Filtered noise slang word: %r query=%r",
                    word,
                    raw_q,
                )
                continue

            # 3. タイポ補正（対象原文カタカナ語への引き戻し機構）
            corrected_word = _correct_katakana_typo(word, unique_target_katakana)
            if corrected_word != word:
                self.log.info(
                    "[web_evaluator] Corrected typo word: %r -> %r (subject text candidate)",
                    word,
                    corrected_word,
                )
                word = corrected_word
                w_norm = re.sub(r"[\s!！?？^○^~〜()（）「」『』【】\[\]<>]+", "", word).lower()

            # 4. 一般的な検索補助語（意味、由来、公式等）は許容
            if w_norm in _COMMON_SEARCH_TERMS:
                valid_words.append(word)
                continue

            # 5. 対象文（または指示文）に含まれているか確認（厳格化）
            is_in_target = False
            if w_norm in norm_target:
                is_in_target = True
            elif re.fullmatch(r"[ァ-ヴー]{2,}", w_norm) or re.fullmatch(r"[a-z0-9]{2,}", w_norm):
                # 3文字以上のカタカナ語・英数字：たった2文字の部分一致での素通りは完全廃止
                is_in_target = False
            else:
                # 漢字を含む語などの場合：
                substantive_w_chars = [c for c in w_norm if re.match(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]", c)]
                if len(substantive_w_chars) == 1:
                    if substantive_w_chars[0] in norm_target:
                        is_in_target = True
                elif len(substantive_w_chars) >= 2:
                    # 連続する2文字以上の部分文字列が norm_target に含まれているか
                    if any(w_norm[i:i+2] in norm_target for i in range(len(w_norm) - 1)):
                        if not re.search(r"[ァ-ヴー]{3,}|[a-z0-9]{3,}", w_norm):
                            is_in_target = True

            if is_in_target:
                valid_words.append(word)
                continue

            # 6. 文脈に含まれている場合は許容（完全外部単語ではない）
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
