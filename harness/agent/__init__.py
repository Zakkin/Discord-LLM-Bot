"""AIエージェント・ハーネス 実行基盤（エージェント本体）およびテストハーネス"""
from __future__ import annotations

from .prompt_parser import OutputParser

try:
    from .prompt_parser import PromptManager
except ImportError:
    PromptManager = None  # type: ignore[assignment, misc]

try:
    from .orchestrator import (
        AgentOrchestrator,
        AgentState,
        DefaultLLMClient,
        LLMClientProtocol,
        OrchestratorConfig,
        OrchestratorResult,
    )
except ImportError:
    AgentOrchestrator = None  # type: ignore[assignment, misc]
    AgentState = None  # type: ignore[assignment, misc]
    DefaultLLMClient = None  # type: ignore[assignment, misc]
    LLMClientProtocol = None  # type: ignore[assignment, misc]
    OrchestratorConfig = None  # type: ignore[assignment, misc]
    OrchestratorResult = None  # type: ignore[assignment, misc]

try:
    from .memory_manager import (
        MemoryStore,
        MemoryStoreProtocol,
        MemoryRecord,
        MemoryEpisode,
        UserHabitProfile,
        UserInterest,
        ReflectionBelief,
        FuturePromptRecord,
        ChannelSummaryRecord,
        get_relevant_memories,
        extract_and_store_memory,
        update_user_habit_profile,
        get_user_habit_profile,
        format_user_habit_profile_lines,
        get_user_habit_profile_lines,
        maybe_store_training_candidate,
        maybe_update_channel_summary,
        is_reflection_belief_valid,
        build_reflection_prompt,
        build_deep_reflection_prompt,
    )
except ImportError:
    MemoryStore = None  # type: ignore[assignment, misc]
    MemoryStoreProtocol = None  # type: ignore[assignment, misc]
    MemoryRecord = None  # type: ignore[assignment, misc]
    MemoryEpisode = None  # type: ignore[assignment, misc]
    UserHabitProfile = None  # type: ignore[assignment, misc]
    UserInterest = None  # type: ignore[assignment, misc]
    ReflectionBelief = None  # type: ignore[assignment, misc]
    FuturePromptRecord = None  # type: ignore[assignment, misc]
    ChannelSummaryRecord = None  # type: ignore[assignment, misc]
    get_relevant_memories = None  # type: ignore[assignment]
    extract_and_store_memory = None  # type: ignore[assignment]
    update_user_habit_profile = None  # type: ignore[assignment]
    get_user_habit_profile = None  # type: ignore[assignment]
    format_user_habit_profile_lines = None  # type: ignore[assignment]
    get_user_habit_profile_lines = None  # type: ignore[assignment]
    maybe_store_training_candidate = None  # type: ignore[assignment]
    maybe_update_channel_summary = None  # type: ignore[assignment]
    is_reflection_belief_valid = None  # type: ignore[assignment]
    build_reflection_prompt = None  # type: ignore[assignment]
    build_deep_reflection_prompt = None  # type: ignore[assignment]

try:
    from .tool_registry import (
        BaseMCPClient,
        MCPToolAdapter,
        MCPToolCallResult,
        MCPToolInfo,
        ToolCall,
        ToolCategory,
        ToolDefinition,
        ToolExecutor,
        ToolParameter,
        ToolRegistry,
        ToolResult,
        WeatherToolAdapter,
        WebSearchToolAdapter,
        get_default_registry,
        register_builtin_tools,
        set_default_registry,
        tool,
    )
except ImportError:
    ToolRegistry = None  # type: ignore[assignment, misc]
    ToolExecutor = None  # type: ignore[assignment, misc]
    ToolDefinition = None  # type: ignore[assignment, misc]
    ToolParameter = None  # type: ignore[assignment, misc]
    ToolCall = None  # type: ignore[assignment, misc]
    ToolResult = None  # type: ignore[assignment, misc]
    ToolCategory = None  # type: ignore[assignment, misc]
    tool = None  # type: ignore[assignment]
    get_default_registry = None  # type: ignore[assignment]
    set_default_registry = None  # type: ignore[assignment]
    register_builtin_tools = None  # type: ignore[assignment]
    WebSearchToolAdapter = None  # type: ignore[assignment, misc]
    WeatherToolAdapter = None  # type: ignore[assignment, misc]
    MCPToolAdapter = None  # type: ignore[assignment, misc]
    BaseMCPClient = None  # type: ignore[assignment, misc]
    MCPToolInfo = None  # type: ignore[assignment, misc]
    MCPToolCallResult = None  # type: ignore[assignment, misc]

try:
    from .observation import (
        EmotionEvaluation,
        EmotionObservationEvaluator,
        EvaluationResult,
        EvaluationVerdict,
        FactCheckEvaluation,
        FactCheckEvaluator,
        FactCheckVerdict,
        FeedbackObservation,
        FeedbackObserver,
        LoopDetectionResult,
        LoopDetector,
        Observation,
        ObservationEvaluator,
        ObservationProcessor,
        ObservationStatus,
        ObservationSynthesizer,
        ReflectionEvaluation,
        ReflectionEvaluator,
        ReplyObservationEvaluator,
        ReplyObservationResult,
        WebObservationEvaluator,
    )
except ImportError:
    EmotionEvaluation = None  # type: ignore[assignment, misc]
    EmotionObservationEvaluator = None  # type: ignore[assignment, misc]
    EvaluationResult = None  # type: ignore[assignment, misc]
    EvaluationVerdict = None  # type: ignore[assignment, misc]
    FactCheckEvaluation = None  # type: ignore[assignment, misc]
    FactCheckEvaluator = None  # type: ignore[assignment, misc]
    FactCheckVerdict = None  # type: ignore[assignment, misc]
    FeedbackObservation = None  # type: ignore[assignment, misc]
    FeedbackObserver = None  # type: ignore[assignment, misc]
    LoopDetectionResult = None  # type: ignore[assignment, misc]
    LoopDetector = None  # type: ignore[assignment, misc]
    Observation = None  # type: ignore[assignment, misc]
    ObservationEvaluator = None  # type: ignore[assignment, misc]
    ObservationProcessor = None  # type: ignore[assignment, misc]
    ObservationStatus = None  # type: ignore[assignment, misc]
    ObservationSynthesizer = None  # type: ignore[assignment, misc]
    ReflectionEvaluation = None  # type: ignore[assignment, misc]
    ReflectionEvaluator = None  # type: ignore[assignment, misc]
    ReplyObservationEvaluator = None  # type: ignore[assignment, misc]
    ReplyObservationResult = None  # type: ignore[assignment, misc]
    WebObservationEvaluator = None  # type: ignore[assignment, misc]


try:
    from .test_harness import (
        AssertionResult,
        DistributedTracer,
        DockerSandbox,
        EnvironmentStateCheck,
        EnvironmentVerifier,
        EvaluationReport,
        JudgeScore,
        LLMAsAJudge,
        LocalRestrictedSandbox,
        MechanicalAssertion,
        MetricsCollector,
        SandboxBackend,
        SandboxConfig,
        SandboxEnvironment,
        SandboxResult,
        SpanKind,
        TelemetryStorage,
        TokenUsage,
        TraceSpan,
        WasmSandbox,
        build_evaluation_report,
        create_sandbox,
    )
except ImportError:
    AssertionResult = None  # type: ignore[assignment, misc]
    DistributedTracer = None  # type: ignore[assignment, misc]
    DockerSandbox = None  # type: ignore[assignment, misc]
    EnvironmentStateCheck = None  # type: ignore[assignment, misc]
    EnvironmentVerifier = None  # type: ignore[assignment, misc]
    EvaluationReport = None  # type: ignore[assignment, misc]
    JudgeScore = None  # type: ignore[assignment, misc]
    LLMAsAJudge = None  # type: ignore[assignment, misc]
    LocalRestrictedSandbox = None  # type: ignore[assignment, misc]
    MechanicalAssertion = None  # type: ignore[assignment, misc]
    MetricsCollector = None  # type: ignore[assignment, misc]
    SandboxBackend = None  # type: ignore[assignment, misc]
    SandboxConfig = None  # type: ignore[assignment, misc]
    SandboxEnvironment = None  # type: ignore[assignment, misc]
    SandboxResult = None  # type: ignore[assignment, misc]
    SpanKind = None  # type: ignore[assignment, misc]
    TelemetryStorage = None  # type: ignore[assignment, misc]
    TokenUsage = None  # type: ignore[assignment, misc]
    TraceSpan = None  # type: ignore[assignment, misc]
    WasmSandbox = None  # type: ignore[assignment, misc]
    build_evaluation_report = None  # type: ignore[assignment]
    create_sandbox = None  # type: ignore[assignment]

__all__ = [
    # 1-1. Prompt Manager & Parser
    "PromptManager",
    "OutputParser",
    # 1-2. Orchestrator (Phase 1/2/3/4/5 Flow Control)
    "AgentOrchestrator",
    "OrchestratorConfig",
    "OrchestratorResult",
    "AgentState",
    "DefaultLLMClient",
    "LLMClientProtocol",
    # 1-3. Memory Manager (Phase 1/4/5: Context & Storage)
    "MemoryStore",
    "MemoryStoreProtocol",
    "MemoryRecord",
    "MemoryEpisode",
    "UserHabitProfile",
    "UserInterest",
    "ReflectionBelief",
    "FuturePromptRecord",
    "ChannelSummaryRecord",
    "get_relevant_memories",
    "extract_and_store_memory",
    "update_user_habit_profile",
    "get_user_habit_profile",
    "format_user_habit_profile_lines",
    "get_user_habit_profile_lines",
    "maybe_store_training_candidate",
    "maybe_update_channel_summary",
    "is_reflection_belief_valid",
    "build_reflection_prompt",
    "build_deep_reflection_prompt",
    # 1-4. Tool Registry (Phase 3: Action)
    "ToolRegistry",
    "ToolExecutor",
    "ToolDefinition",
    "ToolParameter",
    "ToolCall",
    "ToolResult",
    "ToolCategory",
    "tool",
    "get_default_registry",
    "set_default_registry",
    "register_builtin_tools",
    "WebSearchToolAdapter",
    "WeatherToolAdapter",
    "MCPToolAdapter",
    "BaseMCPClient",
    "MCPToolInfo",
    "MCPToolCallResult",
    # 1-5. Observation (Phase 4: Observation & Evaluation)
    "Observation",
    "ObservationStatus",
    "EvaluationVerdict",
    "EvaluationResult",
    "LoopDetectionResult",
    "ObservationProcessor",
    "ObservationEvaluator",
    "LoopDetector",
    "ObservationSynthesizer",
    "EmotionEvaluation",
    "EmotionObservationEvaluator",
    "FactCheckVerdict",
    "FactCheckEvaluation",
    "FactCheckEvaluator",
    "FeedbackObservation",
    "FeedbackObserver",
    "ReplyObservationResult",
    "ReplyObservationEvaluator",
    "ReflectionEvaluation",
    "ReflectionEvaluator",
    "WebObservationEvaluator",
    # 2. Test Harness
    "SandboxBackend",
    "SandboxConfig",
    "SandboxResult",
    "SandboxEnvironment",
    "LocalRestrictedSandbox",
    "DockerSandbox",
    "WasmSandbox",
    "create_sandbox",
    "SpanKind",
    "TokenUsage",
    "TraceSpan",
    "DistributedTracer",
    "MetricsCollector",
    "TelemetryStorage",
    "AssertionResult",
    "JudgeScore",
    "EnvironmentStateCheck",
    "EvaluationReport",
    "MechanicalAssertion",
    "LLMAsAJudge",
    "EnvironmentVerifier",
    "build_evaluation_report",
]
