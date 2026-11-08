"""AIエージェント・ハーネス テレメトリ型定義 (types.py)

2-2. テレメトリ・オブザーバビリティ（監視基盤）において、
分散トレース、スパン、トークン消費量、レイテンシ、メトリクスの型を定義する。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SpanKind(str, Enum):
    """トレーススパンの種別"""
    CONTEXT = "context"                # フェーズ1: コンテキスト構築
    THOUGHT = "thought"                # フェーズ2: 推論・計画立案
    ACTION = "action"                  # フェーズ3: ツール選択・実行
    OBSERVATION = "observation"        # フェーズ4: 結果の観察と評価
    FINAL_RESPONSE = "final_response"  # フェーズ5: 最終応答生成
    INTERNAL = "internal"              # 内部処理・その他


@dataclass
class TokenUsage:
    """トークン消費量データモデル"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    estimated_cost_usd: float = 0.0

    def add(self, other: TokenUsage) -> None:
        """他のトークン使用量を加算"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.estimated_cost_usd += other.estimated_cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass
class TraceSpan:
    """分散トレースの単一スパン（処理単位）"""
    name: str
    kind: SpanKind
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    duration_sec: float = 0.0
    status: str = "OK"  # "OK" or "ERROR"
    error_message: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def finish(self, status: str = "OK", error: str | None = None) -> None:
        """スパンを終了"""
        self.end_time = time.time()
        self.duration_sec = self.end_time - self.start_time
        self.status = status
        self.error_message = error

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """スパン内イベントを追加"""
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": dict(attributes or {}),
        })

    def to_dict(self) -> dict[str, Any]:
        """シリアライズ用辞書"""
        return {
            "name": self.name,
            "kind": self.kind.value,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "error_message": self.error_message,
            "attributes": self.attributes,
            "events": self.events,
        }
