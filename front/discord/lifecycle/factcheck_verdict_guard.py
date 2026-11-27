"""§161: 要約劣化（summary_degraded）時のファクトチェック判定文ガード。"""
from __future__ import annotations

ABSENCE_AS_EVIDENCE_PATTERNS: tuple[str, ...] = (
    "確認できませんでした",
    "確認できなかった",
    "確認できません",
    "触れられていない",
    "触れられていません",
    "記載されていません",
    "記載がありません",
    "言及されていません",
    "見当たりません",
    "見当たらない",
    "根拠が見当たら",
)

DEGRADED_VERDICT_FALLBACK_TEXT: str = (
    "調査結果の要約が完了できなかったため、この発言については裏付けが取れませんでした。判定は保留とします。"
)

def contains_absence_as_evidence(text: str) -> bool:
    """判定文に「資料に書かれていない・確認できなかった」系の表現が含まれるかを返す純関数。"""
    try:
        target = str(text or "")
        if not target:
            return False
        return any(pattern in target for pattern in ABSENCE_AS_EVIDENCE_PATTERNS)
    except Exception:
        return False


def guard_degraded_verdict(
    text: str,
    *,
    degraded: bool,
    enabled: bool,
) -> tuple[str, bool]:
    """要約が劣化しており、かつ判定文に未記載表現が含まれる場合のみ固定文へ差し替える。"""
    try:
        safe_text = text if isinstance(text, str) else str(text or "")
        if bool(enabled) and bool(degraded) and contains_absence_as_evidence(safe_text):
            return DEGRADED_VERDICT_FALLBACK_TEXT, True
        return safe_text, False
    except Exception:
        return (text if isinstance(text, str) else ""), False
