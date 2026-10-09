"""AIエージェント・ハーネス 感情観察評価器 (emotion_evaluator.py)

フェーズ4. 結果の観察と評価（Observation）において、
ユーザー入力や対話結果に対するエージェントの感情変化、感情スコア、
および感情理由（reason）の品質・妥当性を観察・評価する。

AGENTS.md ガードレール遵守:
- 項目23: 中国語・簡体字（「会继续」「继续」「觉得」等）の混入検知とフォールバック
- 項目24: 感情理由からの架空エピソード・事実捏造（ハルシネーション）の遮断
- 項目27: 感情理由のプロンプト隔離・セリフ調テキストサニタイズ
- 項目87: 長期記憶抽出における感情理由からの事実捏造防止
- 項目90: 直前感情理由の他者漏洩遮断
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .types import Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.emotion_evaluator")

# 感情キーの標準定義
EMOTION_KEYS = ("joy", "anticipation", "anger", "disgust", "sadness", "surprise", "fear")

# 簡体字・中国語フレーズ（AGENTS.md 項目23）
_SIMPLIFIED_CHINESE_CHARS = set(
    "继续说话没很还着觉车门头发边东买卖听给"
)
_CHINESE_PHRASE_PATTERN = re.compile(
    r"(会继续|继续|觉得|由于|因为|所以|虽然|但是|没有|什么|怎么|怎样|这样|那样)"
)

# 述語パターンの検出（述語のない断片テキストを排除）
_PREDICATE_PATTERN = re.compile(
    r"(した|している|していた|なった|感じた|覚えた|見えた|受けた|ため|ので|から|です|ます|だ。|だった)"
)

_FALLBACK_REASON_MAP = {
    "joy": "相手の発言を前向きに受け取ったためです。",
    "anticipation": "相手の発言の続きや反応を期待したためです。",
    "anger": "相手の発言に苛立ちを覚えたためです。",
    "disgust": "相手の発言に嫌悪感を覚えたためです。",
    "sadness": "相手の発言を受けて気落ちしたためです。",
    "surprise": "相手の発言が予想外だったためです。",
    "fear": "相手の発言に不安を覚えたためです。",
    "neutral": "平常状態です。",
}


@dataclass
class EmotionEvaluation:
    """感情観察の評価結果モデル"""
    dominant_emotion: str
    scores: dict[str, float]
    reason: str
    is_valid: bool
    reaction_suggestion: str  # "GOOD" / "BAD" / "NONE"
    rejection_reason: str = ""
    observation: Observation | None = None


class EmotionObservationEvaluator:
    """エージェント感情状態・理由テキストの観察評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def contains_cjk_non_japanese(self, text: str) -> bool:
        """中国語・簡体字が混入しているか観察・検知する (AGENTS.md 項目23)"""
        if not text:
            return False
        if _CHINESE_PHRASE_PATTERN.search(text):
            return True
        for ch in text:
            code = ord(ch)
            if 0x3040 <= code <= 0x30FF:  # ひらがな・カタカナ
                continue
            if ch in _SIMPLIFIED_CHINESE_CHARS:
                return True
        return False

    def looks_like_bad_reason_text(self, text: str) -> bool:
        """感情理由として不適切または壊れたテキストか観察・評価する"""
        t = (text or "").strip()
        if not t:
            return True
        if self.contains_cjk_non_japanese(t):
            return True
        if not _PREDICATE_PATTERN.search(t):
            return True
        return False

    def normalize_reason_text(self, text: str, max_chars: int = 180) -> str:
        """理由テキストを正規化して整形する"""
        t = unicodedata.normalize("NFKC", str(text or "")).strip()
        t = re.sub(r"\s+", " ", t)
        t = t.replace('"', "").replace("'", "")
        t = t.strip(" 、。")
        t = t.replace("ユーザー", "相手")
        if t and not t.endswith("。"):
            t += "。"
        if len(t) > max_chars:
            t = t[:max_chars].rstrip(" 、。") + "。"
        return t or "相手の発言に不満を覚えたためです。"

    def fallback_reason_text(self, emotion_key: str) -> str:
        """指定の感情キーに対する安全なフォールバック理由を生成する"""
        return _FALLBACK_REASON_MAP.get(emotion_key, "平常状態です。")

    def evaluate_emotion_state(
        self,
        scores: dict[str, float],
        raw_reason: str,
        user_text: str = "",
        partner_name: str = "",
        max_chars: int = 180,
    ) -> EmotionEvaluation:
        """感情スコアおよび理由テキストを観察・評価し、正規化結果と判定を返す。"""
        # スコアのクランプ (-10.0 〜 +10.0)
        clamped_scores: dict[str, float] = {}
        for k in EMOTION_KEYS:
            val = float(scores.get(k, 0.0))
            clamped_scores[k] = max(-10.0, min(10.0, val))

        # 支配的感情の特定
        dominant = "neutral"
        max_val = 0.0
        for k, val in clamped_scores.items():
            if val > max_val:
                max_val = val
                dominant = k

        # 理由テキストの検証
        clean_raw = str(raw_reason or "").strip()
        is_bad = self.looks_like_bad_reason_text(clean_raw)
        rejection_reason = ""

        if is_bad:
            if self.contains_cjk_non_japanese(clean_raw):
                rejection_reason = "Contains Chinese characters / simplified script"
            elif not clean_raw:
                rejection_reason = "Empty reason text"
            else:
                rejection_reason = "Missing proper Japanese predicate or abnormal format"
            
            final_reason = self.fallback_reason_text(dominant)
            is_valid = False
            self.log.warning(
                "[emotion_evaluator] Invalid emotion reason rejected: raw=%r, reason=%s -> fallback=%r",
                clean_raw,
                rejection_reason,
                final_reason,
            )
        else:
            final_reason = self.normalize_reason_text(clean_raw, max_chars=max_chars)
            is_valid = True

        # リアクション推奨の算出
        joy_val = clamped_scores.get("joy", 0.0)
        anger_val = clamped_scores.get("anger", 0.0)
        disgust_val = clamped_scores.get("disgust", 0.0)
        
        if joy_val >= 2.0 and anger_val < 1.0 and disgust_val < 1.0:
            reaction_sugg = "GOOD"
        elif (anger_val >= 2.0 or disgust_val >= 2.0) and joy_val < 1.0:
            reaction_sugg = "BAD"
        else:
            reaction_sugg = "NONE"

        # Observation オブジェクト構築
        obs = Observation(
            tool_name="emotion_evaluator",
            success=is_valid,
            status=ObservationStatus.SUCCESS if is_valid else ObservationStatus.PARTIAL_SUCCESS,
            output=f"Dominant: {dominant}, Reason: {final_reason}",
            data={
                "dominant_emotion": dominant,
                "scores": clamped_scores,
                "reason": final_reason,
                "reaction": reaction_sugg,
            },
            metadata={
                "partner_name": partner_name,
                "is_fallback_reason": not is_valid,
            },
        )

        return EmotionEvaluation(
            dominant_emotion=dominant,
            scores=clamped_scores,
            reason=final_reason,
            is_valid=is_valid,
            reaction_suggestion=reaction_sugg,
            rejection_reason=rejection_reason,
            observation=obs,
        )

    @staticmethod
    def extract_meaningful_tokens(text: str) -> set[str]:
        """テキストから実質的な名詞・キーワード（漢字・カタカナ・英数字）を抽出する。"""
        s = str(text or "").strip()
        if not s:
            return set()
        s = re.sub(r"^[^（(「\s]{1,30}[（(]ユーザー[）)][はがのをに、\s]*", "", s)
        s = re.sub(r"^[^\s:：]{1,20}[さん君ちゃん氏][はがのをに、\s]*", "", s)
        s = re.sub(r"^ユーザー[はがのをに、\s]*", "", s)
        raw_tokens = set(re.findall(r"[\u4E00-\u9FFF]{2,}|[\u30A0-\u30FFー]{2,}|[A-Za-z0-9_]{3,}", s))
        stop_words = {
            "ユーザー", "メンバー", "アシスタント", "ボット", "自分", "相手", "発言", "会話",
            "日常", "話題", "設定", "情報", "共有", "予定", "約束", "現在", "以前", "今後",
            "最近", "今日", "昨日", "明日", "こと", "もの", "ため", "わけ", "よう", "そう",
            "平常", "状態", "前向き", "期待", "苛立ち", "嫌悪", "不安", "気落ち",
        }
        return {t for t in raw_tokens if t not in stop_words}

    def is_hallucinated_emotion_reason(
        self,
        candidate_memory: str,
        emotion_reason: str,
        dialogue_source: str = "",
    ) -> bool:
        """記憶候補テキストが感情理由（reason）からの架空引き写し・ハルシネーションかを観察・判定する。

        AGENTS.md 項目87: 長期記憶抽出における感情理由からの事実捏造防止
        """
        mem = (candidate_memory or "").strip()
        reason = (emotion_reason or "").strip()
        if not mem or not reason:
            return False

        reason_tokens = self.extract_meaningful_tokens(reason)
        mem_tokens = self.extract_meaningful_tokens(mem)
        if not reason_tokens or not mem_tokens:
            return False

        # トークンの完全一致または部分一致（例: 顔認証 と 顔認証技術）
        overlap: set[str] = set()
        for rt in reason_tokens:
            for mt in mem_tokens:
                if rt == mt or rt in mt or mt in rt:
                    overlap.add(min(rt, mt, key=len))

        if not overlap:
            return False

        # 会話ソースが与えられている場合、ソースに含まれている正規の単語はハルシネーションではない
        source = str(dialogue_source or "").strip()
        if source:
            is_hallucinated = any(t not in source for t in overlap)
        else:
            # 会話ソースが指定されていない場合、感情理由と意味的トークンが重複していれば引き写しと判定
            is_hallucinated = len(overlap) >= 1

        if is_hallucinated:
            self.log.warning(
                "[emotion_evaluator] Hallucinated emotion reason memory detected: memory=%r matched_overlap=%r from reason=%r",
                mem,
                overlap,
                reason,
            )
            return True

        return False


    def sanitize_emotion_reason_for_prompt(
        self,
        reason: str,
        is_same_user: bool = True,
    ) -> str:
        """プロンプトへ注入する感情理由を観察・サニタイズする。

        AGENTS.md 項目90: 直前感情理由の他者漏洩遮断
        - 他ユーザー宛ての場合は主観的感情理由を完全に隔離（空文字）
        - 同一ユーザー宛てでもセリフ調（「〜と言われた」「命令されて」等）は除去
        """
        r = (reason or "").strip()
        if not r:
            return ""

        # 他ユーザーへの返信時は主観的感情理由をプロンプトから完全隔離
        if not is_same_user:
            return ""

        # セリフ調テキストや命令言及のサニタイズ
        r = re.sub(r"「.*?」と言われ(?:た|ている)ため", "発言を受けたため", r)
        r = re.sub(r"命令され(?:た|ている)ため", "指示を受けたため", r)
        return r
