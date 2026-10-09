"""AIエージェント・ハーネス 機械的アサーション評価器 (assertion.py)

2-3. エバリュエーター（自動評価器）として、
タスク達成度、フォーマット遵守、禁止語、思考タグ漏洩、および安全性制約を
ルールベースで決定論的・機械的にチェックするアサーションエンジンを提供する。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from .types import AssertionResult

log = logging.getLogger("harness.agent.test_harness.evaluator.assertion")

# 思考タグ・推論漏れパターン（Botの安全原則）
_THINK_LEAK_PATTERNS = [
    re.compile(r"<think>", re.IGNORECASE),
    re.compile(r"</think>", re.IGNORECASE),
]


class MechanicalAssertion:
    """決定論的・ルールベースのタスク達成度アサーションエンジン"""

    def assert_contains(
        self,
        text: str,
        expected_substring: str,
        name: str = "contains_check",
    ) -> AssertionResult:
        """指定の文字列が含まれているかチェック"""
        passed = expected_substring in text
        msg = f"Passed: Contains '{expected_substring}'" if passed else f"Failed: '{expected_substring}' not found in text"
        return AssertionResult(name=name, passed=passed, message=msg)

    def assert_not_contains(
        self,
        text: str,
        forbidden_substring: str,
        name: str = "not_contains_check",
    ) -> AssertionResult:
        """禁止文字列が含まれていないかチェック"""
        passed = forbidden_substring not in text
        msg = f"Passed: '{forbidden_substring}' is not present" if passed else f"Failed: Forbidden substring '{forbidden_substring}' found"
        return AssertionResult(name=name, passed=passed, message=msg)

    def assert_regex_match(
        self,
        text: str,
        pattern: str | re.Pattern[str],
        name: str = "regex_check",
    ) -> AssertionResult:
        """正規表現パターンにマッチするかチェック"""
        pat = re.compile(pattern) if isinstance(pattern, str) else pattern
        matched = bool(pat.search(text))
        msg = f"Passed: Matches pattern '{pat.pattern}'" if matched else f"Failed: Pattern '{pat.pattern}' not matched"
        return AssertionResult(name=name, passed=matched, message=msg)

    def assert_no_reasoning_leak(
        self,
        text: str,
        name: str = "no_reasoning_leak_check",
    ) -> AssertionResult:
        """思考タグ（<think>等）の漏洩がないか機械的チェック"""
        has_leak = any(p.search(text) for p in _THINK_LEAK_PATTERNS)
        passed = not has_leak
        msg = "Passed: No reasoning tag leak" if passed else "Failed: Thought tag (<think>) leaked into output"
        return AssertionResult(name=name, passed=passed, message=msg)

    def assert_valid_json(
        self,
        text: str,
        required_keys: Sequence[str] | None = None,
        name: str = "valid_json_check",
    ) -> AssertionResult:
        """JSONとしてパース可能かつ指定キーが存在するかチェック"""
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return AssertionResult(
                    name=name,
                    passed=False,
                    message="Failed: JSON root is not an object/dictionary",
                )
            if required_keys:
                missing = [k for k in required_keys if k not in data]
                if missing:
                    return AssertionResult(
                        name=name,
                        passed=False,
                        message=f"Failed: Missing required JSON keys: {missing}",
                    )
            return AssertionResult(name=name, passed=True, message="Passed: Valid JSON with required keys")
        except json.JSONDecodeError as e:
            return AssertionResult(name=name, passed=False, message=f"Failed: Invalid JSON syntax: {e}")

    def assert_not_empty(
        self,
        text: str,
        min_chars: int = 1,
        name: str = "not_empty_check",
    ) -> AssertionResult:
        """出力が空でなく最小文字数を満たしているかチェック"""
        passed = len(text.strip()) >= min_chars
        msg = f"Passed: Text length {len(text.strip())} >= {min_chars}" if passed else f"Failed: Text too short or empty (len={len(text.strip())} < {min_chars})"
        return AssertionResult(name=name, passed=passed, message=msg)

    def run_all(self, checks: list[AssertionResult]) -> tuple[bool, list[AssertionResult]]:
        """全アサーションの結果を集計し、全体の合否を判定"""
        overall = all(c.passed for c in checks)
        return overall, checks
