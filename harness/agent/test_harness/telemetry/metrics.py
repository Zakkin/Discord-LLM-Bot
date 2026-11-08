"""AIエージェント・ハーネス メトリクス計測器 (metrics.py)

2-2. テレメトリ・オブザーバビリティ（監視基盤）として、
トークン消費量（PromptTokens, CompletionTokens, TotalTokens）および
各フェーズのレイテンシ（処理時間）を計測・集計する。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from .types import TokenUsage

log = logging.getLogger("harness.agent.test_harness.telemetry.metrics")


class MetricsCollector:
    """トークン消費量とレイテンシの計測・集計コレクター"""

    def __init__(self) -> None:
        self.total_tokens = TokenUsage()
        self.phase_tokens: dict[str, TokenUsage] = defaultdict(TokenUsage)
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self._start_timestamps: dict[str, float] = {}

    def record_tokens(
        self,
        phase: str,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "",
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """トークン消費量を記録"""
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            model=model,
            estimated_cost_usd=estimated_cost_usd,
        )
        self.total_tokens.add(usage)
        self.phase_tokens[phase].add(usage)
        log.info(
            "[metrics] Tokens recorded for phase=%s: prompt=%d completion=%d total=%d",
            phase,
            prompt_tokens,
            completion_tokens,
            usage.total_tokens,
        )

    def start_timer(self, timer_id: str) -> None:
        """レイテンシタイマーを開始"""
        self._start_timestamps[timer_id] = time.perf_counter()

    def stop_timer(self, timer_id: str, phase: str) -> float:
        """レイテンシタイマーを停止して記録"""
        start = self._start_timestamps.pop(timer_id, None)
        if start is None:
            return 0.0
        elapsed = time.perf_counter() - start
        self.latencies[phase].append(elapsed)
        log.info("[metrics] Latency for phase=%s: %.3fs", phase, elapsed)
        return elapsed

    def record_latency(self, phase: str, duration_sec: float) -> None:
        """直接レイテンシを記録"""
        self.latencies[phase].append(duration_sec)

    def get_summary(self) -> dict[str, Any]:
        """メトリクスの要約集計データを取得"""
        latency_summary: dict[str, dict[str, float]] = {}
        for phase, values in self.latencies.items():
            if not values:
                continue
            latency_summary[phase] = {
                "count": float(len(values)),
                "total_sec": sum(values),
                "avg_sec": sum(values) / len(values),
                "min_sec": min(values),
                "max_sec": max(values),
            }

        return {
            "tokens": {
                "total": self.total_tokens.to_dict(),
                "by_phase": {p: u.to_dict() for p, u in self.phase_tokens.items()},
            },
            "latencies": latency_summary,
        }

    def reset(self) -> None:
        """メトリクスを初期化"""
        self.total_tokens = TokenUsage()
        self.phase_tokens.clear()
        self.latencies.clear()
        self._start_timestamps.clear()
