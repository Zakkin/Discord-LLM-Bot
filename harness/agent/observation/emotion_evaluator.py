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

# リアクション評価値
EMOTION_REACTION_VALUES = ("GOOD", "BAD", "NONE")

# 感情理由のJSONスキーマ
EMOTION_REASON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string"}},
    "required": ["reason"],
    "additionalProperties": False,
}

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

_FALLBACK_APPRAISAL_MAP = {
    "joy": "好意的に受け取った出来事だと感じる。",
    "anticipation": "この先の反応や続きが気になる出来事だと感じる。",
    "anger": "少し突っかかられたように感じる。",
    "disgust": "距離を取りたくなる言い方だと感じる。",
    "sadness": "気持ちが沈む受け取り方になった。",
    "surprise": "予想外で少し戸惑う出来事だと感じる。",
    "fear": "少し身構えたくなる言い方だと感じる。",
    "neutral": "大きな出来事とは受け取っていない。",
}

# 鎮静化（incoming < current）時の凸結合ブレンド率の上下限。
# 1ターンで3割以上スコアを削り取る急冷を防ぎ、感情の余韻を滑らかに減衰させる。
_DISSIPATION_BLEND_MIN = 0.12
_DISSIPATION_BLEND_MAX = 0.22

# 時間経過減衰（オフライン減衰・定期減衰）でのみ付与されるべき表現。
# 会話直後の理由・出来事評価にこれらが残っている場合は「減衰理由の永久引き継ぎ」とみなして修復する。
_TIME_DECAY_TEXT_PATTERN = re.compile(
    r"(時間が経|時間がた|時間の経過|時間を置|引っかかりが薄れ)"
)

# 会話直後に主要感情が平常（neutral）で、今回の出来事評価からも理由を導出できない場合の安全な日常平常理由
_POST_CONVERSATION_NEUTRAL_REASON = "相手との何気ないやり取りを、穏やかに受け止めているためです。"


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


@dataclass
class EmotionScoringEvaluation:
    """感情スコアリング・出来事評価の観察評価結果"""
    scores: dict[str, float]
    appraisal: str
    reaction: str
    is_appraisal_usable: bool
    has_meaningful_signal: bool
    dominant_emotion: str
    mitigated_paranoid_anger: bool = False
    observation: Observation | None = None


@dataclass
class ActionPolicyEvaluation:
    """行動方針（Action Policy）観察評価結果"""
    policy: str
    reason: str = ""
    dominant_emotion: str = ""
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
        role_flip_patterns = [
            r"(?:私|自分|AI|こちら)の愚痴を(?:無視|スルー)",
            r"愚痴を(?:ぶっつけ|言っ|吐い)たのに",
        ]
        for ptn in role_flip_patterns:
            if re.search(ptn, t):
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

    @staticmethod
    def clamp_score(val: Any, min_val: float = 0.0, max_val: float = 1.0) -> float:
        """スコア値を指定の範囲（デフォルト 0.0〜1.0）にクランプする。"""
        try:
            f = float(val or 0.0)
            return max(min_val, min(max_val, f))
        except Exception:
            return min_val

    def looks_like_bad_appraisal_text(self, text: str) -> bool:
        """出来事評価（appraisal）テキストが壊れているか、有害・不適切かを観察・判定する。"""
        cleaned = str(text or "").strip()
        if len(cleaned) < 4:
            return True
        if re.fullmatch(r"[\.\u3002…・\-\s]+", cleaned):
            return True
        if self.contains_cjk_non_japanese(cleaned):
            return True
        for pattern in (
            r"雑菌要求",
            r"AI.*?うんこ.*?してください",
            r"^json",
            r"^\{",
            r"(?:私|自分|AI|こちら)の愚痴を(?:無視|スルー)",
            r"愚痴を(?:ぶっつけ|言っ|吐い)たのに",
        ):
            if re.search(pattern, cleaned, flags=re.IGNORECASE):
                return True
        return False

    def normalize_appraisal_text(self, text: str, max_chars: int = 80, bot_display: str = "") -> str:
        """出来事評価テキストを正規化して整形する。"""
        t = unicodedata.normalize("NFKC", str(text or "")).strip()
        t = re.sub(r"\s+", " ", t)
        bd = str(bot_display or "").strip()
        if not bd:
            try:
                from lib.config_utils import cfg
                bd = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
            except Exception:
                bd = ""
        if bd:
            t = t.replace(bd, "こちら")
            no_ai = re.sub(r"^(?:AI|bot|ボット)\s*", "", bd, flags=re.IGNORECASE).strip()
            if no_ai:
                t = re.sub(rf"AI\s*{re.escape(no_ai)}[斯子]?", "こちら", t)
        t = t.replace("ユーザー", "相手")
        t = t.strip(" 、。")
        if t and not t.endswith(("。", "！", "？")):
            t += "。"
        if len(t) > max_chars:
            t = t[:max_chars].rstrip(" 、。") + "。"
        return t

    def fallback_appraisal_text(self, emotion_key: str) -> str:
        """指定の感情キーに対する安全なフォールバック出来事評価を生成する。"""
        return _FALLBACK_APPRAISAL_MAP.get(emotion_key, "大きな出来事とは受け取っていない。")

    def is_neutral_or_decayed_reason(self, text: str | None) -> bool:
        """理由テキストが平常・時間減衰・未初期化などのニュートラル系であるかを判定する。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return True
        neutral_patterns = (
            "平常状態",
            "落ち着いてきた",
            "引っかかりが薄れてきた",
            "うまく言葉にできない",
            "特に理由はない",
            "特になし",
            "大きな偏りがない",
        )
        return any(p in cleaned for p in neutral_patterns)

    def is_neutral_or_decayed_appraisal(self, text: str | None) -> bool:
        """出来事評価テキストが平常・時間減衰・未初期化などのニュートラル系であるかを判定する。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return True
        neutral_patterns = (
            "平常状態",
            "引っかかりが薄れてきた",
            "落ち着いてきた",
            "大きな出来事とは受け取っていない",
            "特になし",
        )
        return any(p in cleaned for p in neutral_patterns)

    def is_negative_mitigated_appraisal(self, text: str | None) -> bool:
        """出来事評価テキストにおいて、怒り・警戒・不満などの解消・和らぎ・安心が示されているかを判定する。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        # 警戒や不満の残存が明記されている場合は解消ではない
        if any(neg in cleaned for neg in ("警戒心が", "警戒が残", "怒りが残", "不満が残", "信頼できていない", "身構えが残", "警戒している")):
            return False
        mitigated_patterns = (
            "安心",
            "安堵",
            "落ち着",
            "ホッと",
            "解決",
            "なおった",
            "直った",
            "和ら",
            "薄れ",
            "解消",
            "穏やか",
            "好意的",
            "問題ない",
            "怒りは薄れ",
            "怒りが薄れ",
            "怒りは収ま",
            "怒りが収ま",
            "怒りはない",
            "警戒は薄れ",
            "警戒が薄れ",
            "警戒を解",
        )
        return any(p in cleaned for p in mitigated_patterns)

    def is_time_decay_text(self, text: str | None) -> bool:
        """テキストに時間経過減衰でのみ使われるべき表現（「時間が経って」「引っかかりが薄れてきた」等）が含まれるか判定する。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        return bool(_TIME_DECAY_TEXT_PATTERN.search(cleaned))

    def resolve_low_signal_reuse(
        self,
        *,
        used_low_signal_carry: bool,
        dominant: str,
        previous_dominant: str,
        previous_reason: str | None,
        previous_appraisal: str | None,
    ) -> tuple[bool, bool]:
        """低シグナル入力時に前回の感情理由・出来事評価を引き継いでよいかを観察・判定する。

        Returns:
            (can_reuse_reason, can_reuse_appraisal)

        ガードレール:
        - 低シグナル入力かつ主要感情が前回から変わっていない場合のみ、感情の余韻として引き継ぎを検討する。
        - 主要感情が neutral / 非 neutral のいずれであっても、前回の理由・出来事評価が平常・時間減衰系
          （「時間が経って、少し落ち着いてきたためです。」等）であれば絶対に引き継がない。
          （neutral 時に減衰理由がバイパスされ、会話しても永久にコピペされ続ける不具合の再発防止）
        - 平常（neutral）時は余韻として保持すべき感情理由が存在しないため、引き継がず今回の出来事評価から再導出する。
        """
        if not used_low_signal_carry or dominant != previous_dominant:
            return False, False
        if dominant == "neutral":
            return False, False
        prev_reason = str(previous_reason or "").strip()
        prev_appraisal = str(previous_appraisal or "").strip()
        if (
            self.is_neutral_or_decayed_reason(prev_reason)
            or self.is_neutral_or_decayed_appraisal(prev_appraisal)
            or self.is_time_decay_text(prev_reason)
            or self.is_time_decay_text(prev_appraisal)
        ):
            return False, False
        return bool(prev_reason), bool(prev_appraisal)

    def repair_post_conversation_emotion_text(
        self,
        *,
        dominant: str,
        reason: str | None,
        appraisal: str | None,
        max_chars: int = 70,
    ) -> tuple[str, str]:
        """会話処理直後の出来事評価・感情理由を観察し、時間減衰表現の残存や感情との不整合を修復する（多重防御）。

        Returns:
            (repaired_appraisal, repaired_reason)

        - 会話直後に時間減衰表現（「時間が経って…」「引っかかりが薄れてきた…」）が残る出来事評価は、
          主要感情に即した安全なフォールバック評価へ置換する。
        - 主要感情がアクティブ（非 neutral）なのに理由が平常・減衰系の場合は、感情に即した理由へ修復する。
        - 主要感情が neutral で理由に時間減衰表現が残る場合は、今回の出来事評価から導出した日常平常理由、
          それも使えなければ会話後の平常理由へ修復する。
        """
        fixed_appraisal = str(appraisal or "").strip()
        if self.is_time_decay_text(fixed_appraisal):
            self.log.info(
                "[emotion_evaluator] time-decay appraisal remained after conversation; repaired: appraisal=%r dominant=%s",
                fixed_appraisal,
                dominant,
            )
            fixed_appraisal = self.fallback_appraisal_text(dominant)

        fixed_reason = str(reason or "").strip()
        if dominant != "neutral":
            if self.is_neutral_or_decayed_reason(fixed_reason) or self.is_time_decay_text(fixed_reason):
                fixed_reason = self.fallback_reason_text(dominant)
            return fixed_appraisal, fixed_reason

        if self.is_time_decay_text(fixed_reason):
            candidate = ""
            if (
                fixed_appraisal
                and not self.is_neutral_or_decayed_appraisal(fixed_appraisal)
                and not self.looks_like_bad_appraisal_text(fixed_appraisal)
            ):
                candidate = self.reason_from_appraisal(fixed_appraisal, "neutral", max_chars=max_chars)
            if (
                not candidate
                or self.is_time_decay_text(candidate)
                or self.is_neutral_or_decayed_reason(candidate)
            ):
                candidate = _POST_CONVERSATION_NEUTRAL_REASON
            self.log.info(
                "[emotion_evaluator] time-decay reason remained after conversation (neutral); repaired: %r -> %r",
                fixed_reason,
                candidate,
            )
            fixed_reason = candidate
        return fixed_appraisal, fixed_reason

    def reason_from_appraisal(self, appraisal: str | None, emotion_key: str, *, max_chars: int = 70) -> str:
        """出来事評価テキストから感情理由テキストを観察・導出する。"""
        base = str(appraisal or "").strip().strip("。")
        if not base or self.looks_like_bad_appraisal_text(str(appraisal or "").strip()):
            return self.fallback_reason_text(emotion_key)
        reason = self.normalize_reason_text(f"{base}からです。", max_chars=max_chars)
        if self.looks_like_bad_reason_text(reason):
            return self.fallback_reason_text(emotion_key)
        return reason

    def has_meaningful_emotion_signal(
        self,
        scores: dict[str, float] | None,
        *,
        min_peak: float = 0.08,
        min_total: float = 0.12,
    ) -> bool:
        """感情スコア群を有意な感情シグナルが存在するか観察・判定する。"""
        source = scores or {}
        values = sorted((self.clamp_score(source.get(key, 0.0)) for key in EMOTION_KEYS), reverse=True)
        peak = values[0] if values else 0.0
        top_two_total = sum(values[:2])
        return peak >= min_peak or top_two_total >= min_total

    def peak_emotion_key(self, scores: dict[str, float] | None, *, min_score: float = 0.18) -> str:
        """スコア群から最も強い主要感情キーを観察・特定する。"""
        source = scores or {}
        best_key = max(EMOTION_KEYS, key=lambda key: self.clamp_score(source.get(key, 0.0)), default="neutral")
        if self.clamp_score(source.get(best_key, 0.0)) < min_score:
            return "neutral"
        return best_key

    def blend_emotion_state_score(self, current: float, incoming: float, *, decay: float, blend: float) -> float:
        """感情スコアの新旧ブレンドを計算する。上昇時は即応、鎮静化時は滑らかに減衰。

        鎮静化時（incoming < current）は incoming へ向かう凸結合と自然減衰上限（current * decay）の小さい方を採る
        （AGENTS.md 項目133: 加算式ブレンドによる高止まり禁止）。
        ただしブレンド率は _DISSIPATION_BLEND_MIN〜_DISSIPATION_BLEND_MAX（0.12〜0.22）に収め、
        低シグナル時のブレンド率（0.18）を尊重する。1〜2回の相槌で閾値（0.20）を割り込む急冷を防ぎ、
        感情の余韻が自然な減衰曲線を描くようにする。
        """
        current_score = self.clamp_score(current)
        incoming_score = self.clamp_score(incoming)
        if incoming_score >= current_score:
            blended = (current_score * decay) + (incoming_score * blend)
            return self.clamp_score(max(incoming_score, blended))
        else:
            decayed = current_score * decay
            if incoming_score <= 0.0:
                return self.clamp_score(decayed)
            effective_blend = min(max(blend, _DISSIPATION_BLEND_MIN), _DISSIPATION_BLEND_MAX)
            blended = (current_score * (1.0 - effective_blend)) + (incoming_score * effective_blend)
            return self.clamp_score(min(decayed, blended))

    def load_persisted_timestamp(
        self,
        value: Any,
        *,
        now: float,
        field_name: str = "",
        default_on_old: float = 0.0,
    ) -> float:
        """永続化タイムスタンプを安全に観察・復元し、異常値（古いmonotonic値や未来時刻）を正規化する。"""
        try:
            ts = float(value or 0.0)
        except Exception:
            return 0.0
        if ts <= 0.0:
            return 0.0

        if ts < 100000000.0:
            self.log.warning(
                "old monotonic timestamp detected, using default_on_old: "
                "field=%s value=%.3f now=%.3f default_on_old=%.3f",
                field_name,
                ts,
                now,
                default_on_old,
            )
            return default_on_old

        if ts > now + 86400.0:
            self.log.warning(
                "reset future timestamp from emotion state: field=%s value=%.3f now=%.3f",
                field_name,
                ts,
                now,
            )
            return now
        return ts

    def normalize_emotion_scores_from_model(
        self,
        scores: dict[str, Any] | None,
        *,
        integer_scale: bool = False,
    ) -> dict[str, float]:
        """LLMのJSON出力から各感情スコアを観察し、0.0〜1.0 に正規化する。"""
        source = scores or {}
        normalized: dict[str, float] = {}
        for key in EMOTION_KEYS:
            try:
                raw_val = float(source.get(key, 0.0))
                if integer_scale:
                    normalized[key] = self.clamp_score(raw_val / 10.0)
                else:
                    normalized[key] = self.clamp_score(raw_val)
            except Exception:
                normalized[key] = 0.0
        return normalized

    def normalize_scored_reaction(self, value: Any) -> str:
        """LLMのリアクション評価値を観察し、"GOOD", "BAD", "NONE" に正規化する。"""
        normalized = str(value or "").strip().upper()
        return normalized if normalized in EMOTION_REACTION_VALUES else "NONE"

    def resolve_emotion_blend_params(
        self,
        incoming_scores: dict[str, float] | None,
        *,
        low_signal_peak: float = 0.20,
        low_signal_total: float = 0.28,
        low_decay: float = 0.88,
        low_blend: float = 0.18,
        normal_decay: float = 0.92,
        normal_blend: float = 0.35,
    ) -> tuple[float, float, bool]:
        """感情スコア群のシグナル強度を観察し、適切な減衰率・ブレンド率を決定する。"""
        values = sorted((self.clamp_score((incoming_scores or {}).get(key, 0.0)) for key in EMOTION_KEYS), reverse=True)
        peak = values[0] if values else 0.0
        top_two_total = sum(values[:2])
        if peak <= low_signal_peak and top_two_total <= low_signal_total:
            return self.clamp_score(low_decay), self.clamp_score(low_blend), True
        return self.clamp_score(normal_decay), self.clamp_score(normal_blend), False

    def get_emotion_score_schema(self, *, integer_scale: bool = False) -> dict[str, Any]:
        """感情スコアリング用の構造化JSON Schemaを生成する。"""
        if integer_scale:
            item_schema = {"type": "integer", "minimum": 0, "maximum": 10}
        else:
            item_schema = {"type": "number", "minimum": 0.0, "maximum": 1.0}
        return {
            "type": "object",
            "properties": {
                "appraisal": {"type": "string"},
                "reaction": {"type": "string", "enum": list(EMOTION_REACTION_VALUES)},
                **{key: dict(item_schema) for key in EMOTION_KEYS},
            },
            "required": ["appraisal", "reaction", *EMOTION_KEYS],
            "additionalProperties": False,
        }

    def pick_dominant_emotion(
        self,
        scores: dict[str, float],
        incoming_scores: dict[str, float] | None = None,
        appraisal: str | None = None,
        *,
        threshold: float = 0.20,
    ) -> str:
        """感情スコアおよび出来事評価を観察・評価し、現在の支配的感情を決定する。

        AGENTS.md ガードレール:
        - 過去の残り火（anger, disgust, fear）があっても、最新の出来事評価で安心・和らぎ（negative_mitigated）
          が観察された場合は速やかに neutral や joy へ移行。
        - 出来事評価で解消が確認されない限り、低シグナルの相槌1通だけで残り火スコアを無視して neutral へ
          強制消滅させない。感情の余韻はスコアの自然減衰推移（blend_emotion_state_score）に委ね、
          閾値（threshold）を下回った時点で neutral へ移行する。
        """
        if not scores:
            return "neutral"
        best_key = max(scores, key=lambda k: scores.get(k, 0.0))
        best_score = scores.get(best_key, 0.0)
        if best_score < threshold:
            return "neutral"

        if incoming_scores is not None:
            appraisal_text = str(appraisal or "").strip()
            negative_mitigated = self.is_negative_mitigated_appraisal(appraisal_text)
            negative_persists = any(
                neg in appraisal_text
                for neg in ("警戒心", "警戒が残", "警戒して", "怒りが残", "不満が残", "信頼できていない", "身構えが残")
            )

            strong_negative_emotions = ("anger", "disgust", "fear")
            if best_key in strong_negative_emotions and negative_mitigated and not negative_persists:
                if self.clamp_score(incoming_scores.get(best_key, 0.0)) < threshold:
                    incoming_joy = self.clamp_score(incoming_scores.get("joy", 0.0))
                    if (
                        incoming_joy >= 0.12
                        or any(kw in appraisal_text for kw in ("安心", "安堵", "喜", "なおった", "直った"))
                    ):
                        return "joy"
                    return "neutral"

        return best_key

    def mitigate_wordplay_paranoid_anger(
        self,
        user_text: str,
        appraisal: str,
        scores: dict[str, float],
        reaction: str,
    ) -> tuple[dict[str, float], str, str, bool]:
        """言葉遊び・クイズに対する被害妄想（愚痴の無視など）による怒り・嫌悪の暴走を観察・緩和する。"""
        from .reply_evaluator import ReplyObservationEvaluator
        # 言葉遊びパターンの検知
        u = (user_text or "").strip()
        is_wordplay = False
        if any(ptn in u for ptn in ("しりとり", "なぞなぞ", "クイズ", "連想ゲーム", "早口言葉")):
            is_wordplay = True
        elif re.search(r"^(?:次は|つぎは|「?[あ-んア-ンa-zA-Z]」?から始まる)", u):
            is_wordplay = True

        if not is_wordplay:
            return scores, appraisal, reaction, False

        mitigated = False
        new_scores = dict(scores)
        new_appraisal = appraisal
        new_reaction = reaction

        if any(kw in appraisal for kw in ("無視", "愚痴", "馬鹿", "文字遊びで返", "からかわれ")):
            if new_scores.get("anger", 0.0) > 0.3 or new_scores.get("disgust", 0.0) > 0.3 or new_reaction == "BAD":
                self.log.info("[emotion_evaluator] mitigated paranoid anger on wordplay/quiz: appraisal=%r", appraisal)
                new_scores["anger"] = min(new_scores.get("anger", 0.0), 0.1)
                new_scores["disgust"] = min(new_scores.get("disgust", 0.0), 0.1)
                if new_reaction == "BAD":
                    new_reaction = "NONE"
                new_appraisal = "文字遊び（言葉遊び）の提案として受け取る。"
                mitigated = True

        return new_scores, new_appraisal, new_reaction, mitigated

    def evaluate_action_policy(
        self,
        *,
        fatigue: float = 0.0,
        tension: float = 0.0,
        dominant: str = "neutral",
        trust: float = 0.0,
        affinity: float = 0.0,
        affection: float = 0.0,
        hatred: float = 0.0,
    ) -> ActionPolicyEvaluation:
        """エージェントの感情状態および関係性パラメータを観察・評価し、適切な行動方針（policy）を導出する。"""
        policy = "普段通り"
        reason = "平常状態"

        if fatigue > 0.8:
            policy = "塩対応（最小限の相槌のみで早々に会話を切り上げる）"
            reason = "疲労蓄積"
        elif hatred >= 5.0:
            policy = "厳戒態勢・反発（強い警戒と不満を抱き、からかいや無茶ぶりに厳しくクギを刺す・反論する）"
            reason = "強い敵意・嫌悪"
        elif hatred >= 2.5 and affection < 2.0:
            policy = "敵愾心・皮肉（冷淡かつ棘のある態度で接する）"
            reason = "敵愾心と低好意"
        elif affection >= 2.5 and hatred >= 2.5:
            policy = "愛憎葛藤（素直になれず反発しつつも気にかけてしまう）"
            reason = "愛憎相反"
        elif tension > 0.75 and trust <= 0:
            policy = "警戒・事務的（無難に感情を交えず返す）"
            reason = "高緊張かつ低信頼"
        elif dominant in ("anger", "disgust") and trust < 0:
            policy = "突き放す（不快感を示し、距離を置く）"
            reason = "不快感情かつ不信"
        elif dominant == "sadness" and trust > 0.2:
            policy = "慰める・寄り添う（相手の感情に同調する）"
            reason = "悲哀と信頼に基づく共感"
        elif dominant == "joy" and (affinity > 0.4 or affection > 0.5):
            policy = "茶化す・盛り上がる（冗談交じりで親しげに乗っかる）"
            reason = "喜びと親愛"
        elif affection >= 4.0 and hatred <= 0.0:
            policy = "親愛・好意（好意を隠さず優しく素直に接する）"
            reason = "強い好意"

        obs = Observation(
            tool_name="action_policy_evaluator",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=policy,
            data={
                "policy": policy,
                "reason": reason,
                "dominant": dominant,
                "fatigue": fatigue,
                "tension": tension,
                "trust": trust,
                "affinity": affinity,
                "affection": affection,
                "hatred": hatred,
            },
        )
        return ActionPolicyEvaluation(
            policy=policy,
            reason=reason,
            dominant_emotion=dominant,
            observation=obs,
        )


# グローバル共有インスタンス
_default_emotion_evaluator = EmotionObservationEvaluator()

# モジュール関数としてのエクスポート（利便性と後方互換性）
clamp_score = _default_emotion_evaluator.clamp_score
looks_like_bad_appraisal_text = _default_emotion_evaluator.looks_like_bad_appraisal_text
normalize_appraisal_text = _default_emotion_evaluator.normalize_appraisal_text
fallback_appraisal_text = _default_emotion_evaluator.fallback_appraisal_text
is_neutral_or_decayed_reason = _default_emotion_evaluator.is_neutral_or_decayed_reason
is_neutral_or_decayed_appraisal = _default_emotion_evaluator.is_neutral_or_decayed_appraisal
is_negative_mitigated_appraisal = _default_emotion_evaluator.is_negative_mitigated_appraisal
is_time_decay_text = _default_emotion_evaluator.is_time_decay_text
resolve_low_signal_reuse = _default_emotion_evaluator.resolve_low_signal_reuse
repair_post_conversation_emotion_text = _default_emotion_evaluator.repair_post_conversation_emotion_text
reason_from_appraisal = _default_emotion_evaluator.reason_from_appraisal
has_meaningful_emotion_signal = _default_emotion_evaluator.has_meaningful_emotion_signal
peak_emotion_key = _default_emotion_evaluator.peak_emotion_key
blend_emotion_state_score = _default_emotion_evaluator.blend_emotion_state_score
load_persisted_timestamp = _default_emotion_evaluator.load_persisted_timestamp
normalize_emotion_scores_from_model = _default_emotion_evaluator.normalize_emotion_scores_from_model
normalize_scored_reaction = _default_emotion_evaluator.normalize_scored_reaction
resolve_emotion_blend_params = _default_emotion_evaluator.resolve_emotion_blend_params
get_emotion_score_schema = _default_emotion_evaluator.get_emotion_score_schema
pick_dominant_emotion = _default_emotion_evaluator.pick_dominant_emotion
mitigate_wordplay_paranoid_anger = _default_emotion_evaluator.mitigate_wordplay_paranoid_anger
evaluate_action_policy = _default_emotion_evaluator.evaluate_action_policy
