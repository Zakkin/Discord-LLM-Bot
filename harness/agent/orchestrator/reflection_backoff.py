"""AIエージェント・ハーネス オーケストレーター 反省ループの失敗バックオフ・JSON強制 (§176)

通常反省・深い反省の system プロンプトへの JSON のみ出力サフィックス付加と、
通常反省ターゲットループにおける同一材料失敗の再送遮断（インメモリ）を提供する。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
from typing import Any


def _fallback_cfg_bool(name: str, default: bool = False) -> bool:
    return default


try:
    from lib.config_utils import cfg_bool as _imported_cfg_bool
    cfg_bool: Callable[..., bool] = _imported_cfg_bool
except Exception:
    cfg_bool = _fallback_cfg_bool


REFLECTION_JSON_ONLY_SUFFIX: str = (
    "出力は JSON オブジェクトのみとすること。"
    "前置き・説明文・Markdown・箇条書き・コードフェンス（```）は一切禁止。\n"
    '出力例: {"beliefs": [{"concept": "概念", "belief": "信念の文", "evidence_count": 2}]}'
)

DEEP_REFLECTION_JSON_ONLY_SUFFIX: str = (
    "出力は JSON オブジェクトのみとすること。"
    "前置き・説明文・Markdown・箇条書き・コードフェンス（```）は一切禁止。\n"
    '出力例: {"what_happened": "起きたこと", "interpretation": "解釈", '
    '"relationship_change": "関係の変化", "future_policy": "今後の方針"}'
)

def _cfg_bool(key: str, default: bool) -> bool:
    try:
        return bool(cfg_bool(key, default))
    except Exception:
        return default


def is_json_only_suffix_enabled() -> bool:
    """REFLECTION_JSON_ONLY_SUFFIX_ENABLED（既定 True）が有効かを返す。"""
    return _cfg_bool("REFLECTION_JSON_ONLY_SUFFIX_ENABLED", True)


def apply_json_only_suffix(system_prompt: str, *, deep: bool = False) -> str:
    """system プロンプト末尾へ JSON のみ出力サフィックスを冪等に付加する。"""
    if not is_json_only_suffix_enabled():
        return system_prompt
    suffix = DEEP_REFLECTION_JSON_ONLY_SUFFIX if deep else REFLECTION_JSON_ONLY_SUFFIX
    if system_prompt.rstrip().endswith(suffix):
        return system_prompt
    base = system_prompt.rstrip()
    if not base:
        return suffix
    return f"{base}\n\n{suffix}"


def is_backoff_enabled() -> bool:
    """REFLECTION_FAILURE_BACKOFF_ENABLED（既定 True）が有効かを返す。"""
    return _cfg_bool("REFLECTION_FAILURE_BACKOFF_ENABLED", True)


def material_fingerprint(lines: Iterable[str]) -> str:
    """材料行を改行結合した文字列の SHA-256 hexdigest 先頭16桁を決定的に返す。"""
    joined = "\n".join(str(line) for line in lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def failure_key(channel_id: Any, subject_kind: Any, subject_key: Any) -> str:
    """ターゲットを一意に識別するキー（channel_id|subject_kind|subject_key）を返す。"""
    return f"{channel_id}|{subject_kind}|{subject_key}"


class ReflectionFailureTracker:
    """通常反省ターゲットごとの失敗時材料 fingerprint をメモリ上で保持するトラッカー。"""

    def __init__(self) -> None:
        self._failed_fingerprints: dict[str, str] = {}

    def should_skip(self, key: str, fingerprint: str) -> bool:
        """バックオフ有効かつ記録済み fingerprint と一致する場合のみ True を返す。"""
        if not is_backoff_enabled():
            return False
        return self._failed_fingerprints.get(key) == fingerprint

    def record_failure(self, key: str, fingerprint: str) -> None:
        """失敗したターゲットキーへ fingerprint を記録する（上書き可）。"""
        self._failed_fingerprints[key] = fingerprint

    def record_success(self, key: str) -> None:
        """成功したターゲットキーの失敗記録があれば消去する。"""
        self._failed_fingerprints.pop(key, None)


__all__ = [
    "REFLECTION_JSON_ONLY_SUFFIX",
    "DEEP_REFLECTION_JSON_ONLY_SUFFIX",
    "is_json_only_suffix_enabled",
    "apply_json_only_suffix",
    "is_backoff_enabled",
    "material_fingerprint",
    "failure_key",
    "ReflectionFailureTracker",
]
