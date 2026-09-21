"""
[AI Agent Summary]
このファイルは、LLMを使用せずにテキストから感情スコアを推論するヒューリスティックな処理（正規表現ベース）を担当します。
This file handles heuristic, non-LLM based emotion inference from text (regex based).
"""
from __future__ import annotations

import re

from ...common.emotion_helpers import (
    EMOTION_KEYS,
    clamp_score,
)

_EMOTION_GROSS_PATTERNS = re.compile(r"(下痢|うんこ|糞|ゲロ|吐しゃ|吐瀉|くさ|臭|汚|腐|下水)")
_EMOTION_DIRECT_ANGER_PATTERNS = re.compile(r"(ふざけんな|黙れ|うざ|ウザ|きも|キモ|最悪|ムカ|腹立|キレ)")
_EMOTION_ANGER_STATE_PATTERNS = re.compile(r"(怒る|怒った|怒って|怒り|苛立|イラッ|イラつ)")
_EMOTION_SADNESS_PATTERNS = re.compile(r"(悲し|つら|辛い|しんど|寂し|さみし|もう無理|消えたい|泣)")
_EMOTION_FEAR_PATTERNS = re.compile(r"(怖|こわ|恐|不安|やばい|ヤバい|危な|震え)")
_EMOTION_JOY_PATTERNS = re.compile(r"(嬉し|うれし|最高|ありがとう|好き|楽しい|たのし)")
_EMOTION_SURPRISE_PATTERNS = re.compile(r"(まじ|マジ|えっ|え,|え、|なんだって|予想外|そんな|まさか|もし|たらどうする)")


def _infer_emotion_scores_without_llm(latest_text: str) -> dict[str, float]:
    text = str(latest_text or "").strip()
    compact = re.sub(r"\s+", "", text)
    scores = {key: 0.0 for key in EMOTION_KEYS}
    if not compact:
        return scores

    if _EMOTION_GROSS_PATTERNS.search(text):
        scores["disgust"] = max(scores["disgust"], 0.78)
        scores["surprise"] = max(scores["surprise"], 0.45)
    if _EMOTION_DIRECT_ANGER_PATTERNS.search(text):
        scores["anger"] = max(scores["anger"], 0.78)
        scores["disgust"] = max(scores["disgust"], 0.45)
    elif _EMOTION_ANGER_STATE_PATTERNS.search(text):
        scores["anger"] = max(scores["anger"], 0.65)
    if _EMOTION_SADNESS_PATTERNS.search(text):
        scores["sadness"] = max(scores["sadness"], 0.75)
    if _EMOTION_FEAR_PATTERNS.search(text):
        scores["fear"] = max(scores["fear"], 0.75)
        scores["surprise"] = max(scores["surprise"], 0.35)
    if _EMOTION_JOY_PATTERNS.search(text):
        scores["joy"] = max(scores["joy"], 0.75)
    if _EMOTION_SURPRISE_PATTERNS.search(text):
        scores["surprise"] = max(scores["surprise"], 0.65)
    if re.search(r"[?？]", text):
        scores["anticipation"] = max(scores["anticipation"], 0.60)
        scores["surprise"] = max(scores["surprise"], 0.30)
    if re.search(r"[!！]", text):
        scores["surprise"] = max(scores["surprise"], 0.45)

    return {key: clamp_score(value) for key, value in scores.items()}
