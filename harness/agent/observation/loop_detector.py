"""AIエージェント・ハーネス ループ検知器 (loop_detector.py)

フェーズ4. 結果の観察と評価（Observation）において、
同一ツールの無限呼び出し、同一引数での繰り返し、および同一エラーの反復（エラーループ）を
未然に検知・遮断し、エージェントの安全性を担保する。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any

from .types import LoopDetectionResult

log = logging.getLogger("harness.agent.observation.loop_detector")


class LoopDetector:
    """エラーループおよび同一ツール繰り返し実行を検知するガードコンポーネント"""

    def __init__(
        self,
        max_identical_calls: int = 2,
        max_consecutive_errors: int = 3,
        max_total_turns: int = 10,
        logger: logging.Logger | None = None,
    ) -> None:
        self.max_identical_calls = max_identical_calls
        self.max_consecutive_errors = max_consecutive_errors
        self.max_total_turns = max_total_turns
        self.log = logger or log

        self._call_history: list[str] = []
        self._consecutive_errors: int = 0
        self._call_counter: Counter[str] = Counter()
        self._turn_count: int = 0

    def record_and_check(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        is_error: bool = False,
        current_turn: int | None = None,
    ) -> LoopDetectionResult:
        """ツール実行記録を登録し、ループ状態を評価する。

        Args:
            tool_name: ツール名
            arguments: ツール引数
            is_error: エラーが発生したかどうか
            current_turn: オーケストレーターの現在ターン番号（指定時はこれを使用）

        Returns:
            LoopDetectionResult: 検知結果
        """
        if current_turn is not None:
            self._turn_count = current_turn
        else:
            self._turn_count += 1

        args_repr = self._hash_arguments(arguments or {})
        call_signature = f"{tool_name}:{args_repr}"

        self._call_history.append(call_signature)
        self._call_counter[call_signature] += 1
        identical_count = self._call_counter[call_signature]

        if is_error:
            self._consecutive_errors += 1
        else:
            self._consecutive_errors = 0

        # 1. 総ターン数上限チェック
        if self._turn_count >= self.max_total_turns:
            msg = (
                f"エージェントの最大試行回数上限（{self.max_total_turns}ターン）に達しました。"
                "これ以上のツール実行を停止し、現在得られている情報で回答を作成してください。"
            )
            self.log.warning("[loop_detector] %s", msg)
            return LoopDetectionResult(
                loop_detected=True,
                loop_count=self._turn_count,
                message=msg,
                abort_execution=True,
            )

        # 2. 連続エラー上限チェック
        if self._consecutive_errors >= self.max_consecutive_errors:
            msg = (
                f"ツール実行でエラーが連続{self._consecutive_errors}回発生しました。"
                "同じアプローチは避け、別のツールを試すかユーザーにエラーを説明してください。"
            )
            self.log.warning("[loop_detector] %s", msg)
            return LoopDetectionResult(
                loop_detected=True,
                loop_count=self._consecutive_errors,
                message=msg,
                abort_execution=False,
            )

        # 3. 同一ツール・同一引数の繰り返しチェック
        if identical_count >= self.max_identical_calls:
            msg = (
                f"ツール '{tool_name}' が全く同じ引数で{identical_count}回呼び出されています。"
                "結果は変わりません。引数の修正または別のアプローチを選択してください。"
            )
            self.log.warning("[loop_detector] %s (call=%s)", msg, call_signature)
            return LoopDetectionResult(
                loop_detected=True,
                loop_count=identical_count,
                message=msg,
                abort_execution=False,
            )

        return LoopDetectionResult(
            loop_detected=False,
            loop_count=0,
            message="",
            abort_execution=False,
        )

    def reset(self) -> None:
        """状態をリセット"""
        self._call_history.clear()
        self._call_counter.clear()
        self._consecutive_errors = 0
        self._turn_count = 0

    def _hash_arguments(self, arguments: dict[str, Any]) -> str:
        """引数辞書から一意なハッシュ文字列を生成"""
        try:
            serialized = json.dumps(arguments, sort_keys=True, ensure_ascii=True)
        except Exception:
            serialized = str(sorted(arguments.items()))
        return hashlib.md5(serialized.encode("utf-8")).hexdigest()[:12]
