"""AIエージェント・ハーネス 分散トレーサー (tracer.py)

2-2. テレメトリ・オブザーバビリティ（監視基盤）として、
思考プロセス（Thought）、ツール選択・実行（Action）、観察・評価（Observation）等の
エージェント内部フローを階層的に追跡する分散トレースを提供する。
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any, Generator

from .types import SpanKind, TraceSpan

log = logging.getLogger("harness.agent.test_harness.telemetry.tracer")


class DistributedTracer:
    """エージェントライフサイクル全体の分散トレーサー"""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self._span_stack: list[TraceSpan] = []
        self._completed_spans: list[TraceSpan] = []

    @property
    def current_span(self) -> TraceSpan | None:
        """現在アクティブなスパンを取得"""
        return self._span_stack[-1] if self._span_stack else None

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[TraceSpan, None, None]:
        """コンテキストマネージャでスパンを生成・終了する"""
        parent = self.current_span
        parent_id = parent.span_id if parent else None

        span = TraceSpan(
            name=name,
            kind=kind,
            trace_id=self.trace_id,
            parent_span_id=parent_id,
            attributes=dict(attributes or {}),
        )

        self._span_stack.append(span)
        log.debug(
            "[tracer] Span started: name=%s kind=%s span_id=%s parent_id=%s",
            name,
            kind.value,
            span.span_id,
            parent_id,
        )

        try:
            yield span
            span.finish(status="OK")
        except Exception as e:
            span.finish(status="ERROR", error=f"{type(e).__name__}: {e}")
            raise
        finally:
            self._span_stack.pop()
            self._completed_spans.append(span)
            log.debug(
                "[tracer] Span ended: name=%s duration=%.3fs status=%s",
                name,
                span.duration_sec,
                span.status,
            )

    def get_spans(self) -> list[TraceSpan]:
        """完了した全スパンを取得"""
        return list(self._completed_spans)

    def reset(self, new_trace_id: str | None = None) -> None:
        """トレースをリセット"""
        self.trace_id = new_trace_id or uuid.uuid4().hex
        self._span_stack.clear()
        self._completed_spans.clear()
