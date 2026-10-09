"""AIエージェント・ハーネス 2. テストハーネス（評価・安全性基盤）

エージェントの監視・検証・保護を担う基盤コンポーネント。
2-1. サンドボックス実行環境（sandbox）
2-2. テレメトリ・オブザーバビリティ（telemetry）
2-3. エバリュエーター（evaluator）
"""
from __future__ import annotations

from .evaluator import (
    AssertionResult,
    EnvironmentStateCheck,
    EnvironmentVerifier,
    EvaluationReport,
    JudgeScore,
    LLMAsAJudge,
    MechanicalAssertion,
    build_evaluation_report,
)
from .sandbox import (
    DEFAULT_BLOCKED_COMMANDS,
    DockerSandbox,
    LocalRestrictedSandbox,
    SandboxBackend,
    SandboxConfig,
    SandboxEnvironment,
    SandboxResult,
    WasmSandbox,
    create_sandbox,
)
from .telemetry import (
    DistributedTracer,
    MetricsCollector,
    SpanKind,
    TelemetryStorage,
    TokenUsage,
    TraceSpan,
)

__all__ = [
    # 2-1. サンドボックス
    "SandboxBackend",
    "SandboxConfig",
    "SandboxResult",
    "DEFAULT_BLOCKED_COMMANDS",
    "SandboxEnvironment",
    "LocalRestrictedSandbox",
    "DockerSandbox",
    "WasmSandbox",
    "create_sandbox",
    # 2-2. テレメトリ
    "SpanKind",
    "TokenUsage",
    "TraceSpan",
    "DistributedTracer",
    "MetricsCollector",
    "TelemetryStorage",
    # 2-3. エバリュエーター
    "AssertionResult",
    "JudgeScore",
    "EnvironmentStateCheck",
    "EvaluationReport",
    "MechanicalAssertion",
    "LLMAsAJudge",
    "EnvironmentVerifier",
    "build_evaluation_report",
]
