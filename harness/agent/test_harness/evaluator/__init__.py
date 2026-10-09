"""AIエージェント・ハーネス 2-3. エバリュエーター（自動評価器）パッケージ

タスク達成度の機械的チェック（アサーション）、LLM-as-a-Judge（定性評価・採点）、
および環境状態の検証（ファイル生成・状態変更チェック）を提供する。
"""
from __future__ import annotations

from .assertion import MechanicalAssertion
from .environment_verifier import EnvironmentVerifier
from .llm_judge import LLMAsAJudge
from .types import (
    AssertionResult,
    EnvironmentStateCheck,
    EvaluationReport,
    JudgeScore,
)


def build_evaluation_report(
    task_id: str,
    assertions: list[AssertionResult] | None = None,
    judge_score: JudgeScore | None = None,
    env_checks: list[EnvironmentStateCheck] | None = None,
) -> EvaluationReport:
    """各種評価結果を集約して総合評価レポートを生成"""
    assertion_list = assertions or []
    env_list = env_checks or []

    assertions_passed = all(a.passed for a in assertion_list)
    judge_passed = judge_score.passed if judge_score else True
    env_passed = all(e.passed for e in env_list)

    overall = assertions_passed and judge_passed and env_passed
    summary = (
        f"Task {task_id}: {'PASSED' if overall else 'FAILED'} "
        f"(Assertions: {len([a for a in assertion_list if a.passed])}/{len(assertion_list)}, "
        f"Judge: {judge_score.score if judge_score else 'N/A'}, "
        f"EnvChecks: {len([e for e in env_list if e.passed])}/{len(env_list)})"
    )

    return EvaluationReport(
        task_id=task_id,
        overall_passed=overall,
        assertion_results=assertion_list,
        judge_result=judge_score,
        env_checks=env_list,
        summary=summary,
    )


__all__ = [
    "AssertionResult",
    "JudgeScore",
    "EnvironmentStateCheck",
    "EvaluationReport",
    "MechanicalAssertion",
    "LLMAsAJudge",
    "EnvironmentVerifier",
    "build_evaluation_report",
]
