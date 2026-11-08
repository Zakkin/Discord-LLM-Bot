"""AIエージェント・ハーネス 観察プロセッサ互換モジュール (observer.py)

後方互換性維持のための委譲モジュール。
本体実装は harness.agent.observation.parser に統合されました。
"""
from __future__ import annotations

from .parser import (
    ObservationParser,
    ObservationProcessor,
    _LOGIN_WALL_PATTERNS,
    _PERMISSION_DENIED_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _TIMEOUT_PATTERNS,
)

__all__ = [
    "ObservationProcessor",
    "ObservationParser",
    "_LOGIN_WALL_PATTERNS",
    "_RATE_LIMIT_PATTERNS",
    "_PERMISSION_DENIED_PATTERNS",
    "_TIMEOUT_PATTERNS",
]
