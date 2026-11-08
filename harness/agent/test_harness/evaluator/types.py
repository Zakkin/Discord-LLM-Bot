"""AIエージェント・ハーネス 自動評価器（エバリュエーター）型定義 (types.py)

2-3. エバリュエーター（自動評価器 フェーズ4/5に関係）において、
機械的アサーション、LLM-as-a-Judge、環境状態検証、および総合レポートの型を定義する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AssertionResult:
    """機械的チェック（アサーション）の検証結果"""
    name: str
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class JudgeScore:
    """LLM-as-a-Judge による定性評価・採点結果"""
    score: float  # 0.0 〜 5.0 (または 0.0 〜 1.0)
    passed: bool
    reason: str
    rubric_scores: dict[str, float] = field(default_factory=dict)
    judge_model: str = "judge_llm"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnvironmentStateCheck:
    """環境状態（ファイル、DB等の状態変化）の検証結果"""
    check_name: str
    passed: bool
    target_path: str
    expected: Any = None
    actual: Any = None
    message: str = ""


@dataclass
class EvaluationReport:
    """タスク総合評価レポート"""
    task_id: str
    overall_passed: bool
    assertion_results: list[AssertionResult] = field(default_factory=list)
    judge_result: JudgeScore | None = None
    env_checks: list[EnvironmentStateCheck] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "overall_passed": self.overall_passed,
            "summary": self.summary,
            "assertions": [
                {"name": a.name, "passed": a.passed, "message": a.message}
                for a in self.assertion_results
            ],
            "judge": (
                {
                    "score": self.judge_result.score,
                    "passed": self.judge_result.passed,
                    "reason": self.judge_result.reason,
                    "rubrics": self.judge_result.rubric_scores,
                }
                if self.judge_result
                else None
            ),
            "environment_checks": [
                {"check": e.check_name, "passed": e.passed, "target": e.target_path, "message": e.message}
                for e in self.env_checks
            ],
        }
