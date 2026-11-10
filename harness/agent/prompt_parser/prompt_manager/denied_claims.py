"""否定済み主張レジストリと文脈注釈ユーティリティ"""
from __future__ import annotations

import re
import time
from typing import Any

from ollama_bot.common.config_helpers import cfg

__all__ = [
    "register_denied_claim",
    "list_denied_claims",
    "clear_denied_claims",
    "annotate_context_lines",
]

# キー: (persona, channel_key) -> list of dict
_DENIED_CLAIMS_REGISTRY: dict[tuple[str, str], list[dict[str, Any]]] = {}


def register_denied_claim(
    *,
    persona: str,
    channel_key: str,
    claim: str,
    corrected_fact: str = "",
) -> None:
    claim_clean = str(claim or "").strip()
    if not claim_clean or len(claim_clean) < 2:
        return
    max_items = int(cfg("DENIED_CLAIMS_MAX", 10) or 10)
    key = (persona.strip() or "default", str(channel_key).strip())
    claims = _DENIED_CLAIMS_REGISTRY.setdefault(key, [])

    # 重複排除
    claims[:] = [c for c in claims if c.get("claim") != claim_clean]
    claims.append({
        "claim": claim_clean[:80],
        "corrected_fact": str(corrected_fact or "").strip()[:80],
        "registered_at": time.time(),
    })
    if len(claims) > max_items:
        _DENIED_CLAIMS_REGISTRY[key] = claims[-max_items:]


def list_denied_claims(*, persona: str, channel_key: str) -> list[dict[str, str]]:
    ttl = float(cfg("DENIED_CLAIMS_TTL_SEC", 21600.0) or 21600.0)
    now = time.time()
    key = (persona.strip() or "default", str(channel_key).strip())
    claims = _DENIED_CLAIMS_REGISTRY.get(key, [])
    # TTL切れ除去
    valid_claims = [c for c in claims if (now - float(c.get("registered_at", 0.0))) <= ttl]
    _DENIED_CLAIMS_REGISTRY[key] = valid_claims
    return [
        {"claim": str(c.get("claim", "")), "corrected_fact": str(c.get("corrected_fact", ""))}
        for c in valid_claims
    ]


def clear_denied_claims() -> None:
    """テスト用クリア関数"""
    _DENIED_CLAIMS_REGISTRY.clear()


def annotate_context_lines(lines: list[str], claims: list[dict[str, str]]) -> list[str]:
    """Bot発言行に否定済み注釈を付与する純粋関数"""
    if not lines or not claims:
        return list(lines)
    valid_claims = [c["claim"] for c in claims if len(c.get("claim", "")) >= 8]
    if not valid_claims:
        return list(lines)

    annotated: list[str] = []
    for line in lines:
        raw = str(line or "")
        # ユーザー行（コロンの左が相手等）やBot以外の行判定（簡易判定: （あなた）やBotプレフィックス）
        # Bot発言行と判定されるか、もしくは注釈が付いていない行を対象とする
        is_bot_line = "（あなた）" in raw or "[Bot]" in raw or "AI:" in raw
        if is_bot_line and "（※この内容はユーザーが否定済みです）" not in raw:
            norm = re.sub(r"[\s、。！？!?,.]+", "", raw).lower()
            hit = any(re.sub(r"[\s、。！？!?,.]+", "", c).lower() in norm for c in valid_claims)
            if hit:
                annotated.append(f"{raw.rstrip()}（※この内容はユーザーが否定済みです）")
                continue
        annotated.append(raw)
    return annotated
