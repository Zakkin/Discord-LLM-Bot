"""AIエージェント・ハーネス テレメトリ永続化ストレージ (storage.py)

2-2. テレメトリ・オブザーバビリティ（監視基盤）として、
分散トレース情報、メトリクス、デバッグログを JSONL ファイルへ永続化し、
事後検証やデバッグのための検索機能を提供する。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .types import TraceSpan

log = logging.getLogger("harness.agent.test_harness.telemetry.storage")


class TelemetryStorage:
    """テレメトリデータの永続化および検索マネージャー"""

    def __init__(self, log_dir: Path | str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.traces_file = self.log_dir / "traces.jsonl"
        self.metrics_file = self.log_dir / "metrics.jsonl"

    def persist_spans(self, spans: list[TraceSpan]) -> None:
        """スパンリストを traces.jsonl に追記保存"""
        if not spans:
            return

        with self.traces_file.open("a", encoding="utf-8") as f:
            for span in spans:
                line = json.dumps(span.to_dict(), ensure_ascii=False)
                f.write(line + "\n")
        log.info("[storage] Persisted %d spans to %s", len(spans), self.traces_file)

    def persist_metrics_summary(self, summary: dict[str, Any], trace_id: str = "") -> None:
        """メトリクス集計結果を metrics.jsonl に追記保存"""
        record = {
            "trace_id": trace_id,
            "summary": summary,
        }
        with self.metrics_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("[storage] Persisted metrics summary for trace_id=%s", trace_id)

    def get_spans_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """保存された JSONL から特定トレースIDのスパンを復元取得"""
        if not self.traces_file.exists():
            return []

        results: list[dict[str, Any]] = []
        with self.traces_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("trace_id") == trace_id:
                        results.append(data)
                except Exception:
                    continue
        return results
