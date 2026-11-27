"""§169: ファクトチェック系 JSON 応答の形状崩れサルベージ（純粋関数群）。"""
from __future__ import annotations

import json
from typing import Any, Callable

SALVAGE_ENABLED_SETTING: str = "FACTCHECK_JSON_SALVAGE_ENABLED"

_CHECKABLE_ALIAS_KEYS: tuple[str, ...] = (
    "is_verifiable_claim",
    "verifiable",
    "is_checkable",
    "checkable_flag",
)
_VALID_CLAIM_TYPES: frozenset[str] = frozenset(
    {"fact", "hearsay", "opinion", "personal_experience", "question", "other"}
)
_SEARCH_QUERY_KEYS: tuple[str, ...] = (
    "search_query",
    "query",
    "keywords",
    "search_keywords",
    "keyword",
)
_MAX_JSON_CANDIDATES: int = 5
_MAX_REASON_CHARS: int = 120
_MAX_QUERY_CHARS: int = 200

cfg_bool: Callable[[str, bool], bool] | None
try:
    from ollama_bot.common.config_helpers import cfg_bool as _cfg_bool_impl
    cfg_bool = _cfg_bool_impl
except Exception:
    cfg_bool = None

_json_loads: Callable[[str], Any]
try:
    from ollama_bot.common.json_compat import loads as _compat_json_loads
    _json_loads = _compat_json_loads
except Exception:
    _json_loads = json.loads

def _coerce_bool(value: Any) -> bool | None:
    """bool または 'true'/'false' 文字列（大小文字不問）を真偽値へ正規化する。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def extract_raw_json_from_error(exc: BaseException) -> Any | None:
    """例外メッセージの '{' / '[' 出現位置（最大5か所）から JSON パースを試み最初に成功した値を返す。"""
    try:
        text = str(exc)
    except Exception:
        return None
    if not text:
        return None

    positions: list[int] = []
    for idx, ch in enumerate(text):
        if ch in "{[":
            positions.append(idx)
            if len(positions) >= _MAX_JSON_CANDIDATES:
                break

    for pos in positions:
        candidate = text[pos:].rstrip()
        while candidate.endswith("```"):
            candidate = candidate[:-3].rstrip()
        if not candidate:
            continue
        try:
            return _json_loads(candidate)
        except Exception:
            continue
    return None


def salvage_checkability(obj: Any) -> dict[str, Any] | None:
    """別名キー・文字列真偽値など形状崩れの検証可能性分類辞書を正規化して返す。"""
    if not isinstance(obj, dict):
        return None

    checkable_val: bool | None = None
    if "checkable" in obj:
        checkable_val = _coerce_bool(obj.get("checkable"))
    if checkable_val is None:
        for alias in _CHECKABLE_ALIAS_KEYS:
            if alias in obj:
                coerced = _coerce_bool(obj.get(alias))
                if coerced is not None:
                    checkable_val = coerced
                    break
    if checkable_val is None:
        return None

    raw_claim_type = obj.get("claim_type")
    if isinstance(raw_claim_type, str) and raw_claim_type.strip() in _VALID_CLAIM_TYPES:
        claim_type = raw_claim_type.strip()
    else:
        claim_type = "other"

    raw_reason = obj.get("reason")
    reason = "" if raw_reason is None else str(raw_reason).strip()[:_MAX_REASON_CHARS]

    return {
        "checkable": checkable_val,
        "claim_type": claim_type,
        "reason": reason,
    }


def salvage_search_query(obj: Any) -> str:
    """文字列・配列・別名キー辞書から検索クエリ文字列を救出する（上限200文字）。"""
    if isinstance(obj, str):
        return obj.strip()[:_MAX_QUERY_CHARS]
    if isinstance(obj, (list, tuple)):
        parts = [item.strip() for item in obj if isinstance(item, str) and item.strip()]
        return " ".join(parts).strip()[:_MAX_QUERY_CHARS]
    if isinstance(obj, dict):
        for key in _SEARCH_QUERY_KEYS:
            if key not in obj:
                continue
            value = obj[key]
            if isinstance(value, str):
                cleaned = value.strip()[:_MAX_QUERY_CHARS]
                if cleaned:
                    return cleaned
            elif isinstance(value, (list, tuple)):
                parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
                joined = " ".join(parts).strip()[:_MAX_QUERY_CHARS]
                if joined:
                    return joined
    return ""


def is_salvage_enabled() -> bool:
    """FACTCHECK_JSON_SALVAGE_ENABLED を返す。import・取得失敗時は True。"""
    if cfg_bool is None:
        return True
    try:
        return bool(cfg_bool(SALVAGE_ENABLED_SETTING, True))
    except Exception:
        return True


def salvage_checkability_from_error(exc: BaseException) -> dict[str, Any] | None:
    """例外から検証可能性分類を救出する。無効化または救出不能時は None。"""
    if not is_salvage_enabled():
        return None
    raw = extract_raw_json_from_error(exc)
    if raw is None:
        return None
    return salvage_checkability(raw)


def salvage_search_query_from_error(exc: BaseException) -> str:
    """例外から検索クエリ文字列を救出する。無効化または救出不能時は空文字。"""
    if not is_salvage_enabled():
        return ""
    raw = extract_raw_json_from_error(exc)
    if raw is None:
        return ""
    return salvage_search_query(raw)


__all__ = [
    "SALVAGE_ENABLED_SETTING",
    "extract_raw_json_from_error",
    "salvage_checkability",
    "salvage_search_query",
    "is_salvage_enabled",
    "salvage_checkability_from_error",
    "salvage_search_query_from_error",
]
