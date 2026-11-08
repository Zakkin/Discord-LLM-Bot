"""AIエージェント・ハーネス 結果の観察と評価（Observation）型定義 (types.py)

フェーズ4. 結果の観察と評価（Observation）で用いられる
観察データモデル、観察ステータス、評価結果判定、ループ検知モデルを定義する。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ObservationStatus(str, Enum):
    """観察対象（ツール・外部環境・コード実行）の結果ステータス"""
    SUCCESS = "success"                      # 正常終了（有意な結果あり）
    EMPTY_RESULT = "empty_result"            # 正常終了だが結果が空/見つからない
    PARTIAL_SUCCESS = "partial_success"      # 一部成功（追加情報が必要）
    ERROR = "error"                          # ツール・コマンド実行エラー
    TIMEOUT = "timeout"                      # 実行タイムアウト
    PERMISSION_DENIED = "permission_denied"  # 権限制限・サンドボックス拒否
    RATE_LIMITED = "rate_limited"            # APIレート制限
    LOGIN_WALL = "login_wall"                # ログインウォール・スクレイピング遮断
    PROTOCOL_ERROR = "protocol_error"        # MCPやJSON-RPC等のプロトコル通信エラー
    SERVER_ERROR = "server_error"            # HTTP 500系やバックエンド障害
    BAD_ARGUMENTS = "bad_arguments"          # 引数不正・バリデーションエラー
    UNKNOWN = "unknown"                      # 未知のステータス


class EvaluationVerdict(str, Enum):
    """LLMまたはルールベース評価器によるObservation評価結果"""
    GOAL_ACHIEVED = "goal_achieved"                  # 目的達成（フェーズ5: Final Response へ進行可能）
    NEED_MORE_INFO = "need_more_info"                # 情報不足（フェーズ2: Thought へ戻り追加情報収集）
    RETRY_WITH_CORRECTION = "retry_with_correction"  # 引数・クエリ修正して再試行（フェーズ2/3へ戻る）
    ALTERNATIVE_TOOL = "alternative_tool"            # 別のツールを選択して別アプローチ（フェーズ2/3へ戻る）
    FATAL_ERROR = "fatal_error"                      # 修復不能なエラー（フォールバック応答へ落とす）


class NextStepPhase(str, Enum):
    """フェーズ4評価後の次フェーズ状態遷移先（ルーティング）"""
    PROCEED_TO_FINAL = "proceed_to_final"      # フェーズ5（最終応答生成）へ進行
    RETRY_WITH_THOUGHT = "retry_with_thought"  # フェーズ2（推論・計画立案）へ巻き戻し再試行
    FATAL_ERROR = "fatal_error"                # 致命的エラー/安全停止のため中断しフォールバック応答へ


@dataclass
class Observation:
    """ツール実行結果や外部環境から得られた観察データ"""
    tool_name: str
    call_id: str = ""
    success: bool = True
    status: ObservationStatus = ObservationStatus.SUCCESS
    output: str = ""
    error: str | None = None
    data: Any = None
    execution_time_sec: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_tool_message(self) -> dict[str, Any]:
        """LLMのチャット履歴に注入可能なツールメッセージ辞書（OpenAI/Ollama互換）を生成"""
        content = self.output if self.success else f"Error ({self.status.value}): {self.error or 'Execution failed'}"
        tool_call_id = self.call_id or f"call_{self.tool_name or 'tool'}"
        msg: dict[str, Any] = {
            "role": "tool",
            "name": self.tool_name or "unknown_tool",
            "content": content,
            "tool_call_id": tool_call_id,
        }
        return msg

    def to_prompt_context(self, max_length: int = 2000) -> str:
        """思考プロンプト（Thought）へ注入するための構造化テキストを生成"""
        status_label = f"[{self.status.value.upper()}]"
        if self.success:
            body = self.output.strip()
            if len(body) > max_length:
                body = body[:max_length] + f"\n... (truncated, total {len(self.output)} chars)"
            return f"【ツール実行結果: {self.tool_name}】 {status_label}\n{body}"
        else:
            err = self.error or "Unknown error"
            return f"【ツール実行エラー: {self.tool_name}】 {status_label}\nエラー内容: {err}"


@dataclass
class EvaluationResult:
    """Observation に対する評価結果モデル"""
    verdict: EvaluationVerdict
    score: float = 1.0  # 0.0（完全失敗）〜 1.0（完全達成）
    reason: str = ""
    is_goal_achieved: bool = True
    needs_rethink: bool = False
    suggested_next_step: str = ""
    rethink_prompt: str = ""
    error_loop_detected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopDetectionResult:
    """エラーループ・同一呼び出しループの検知結果"""
    loop_detected: bool = False
    loop_count: int = 0
    message: str = ""
    abort_execution: bool = False


@dataclass
class ScratchpadEntry:
    """短期記憶（スクラッチパッド）の1ステップ記録"""
    turn: int
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    observation: Observation | None = None
    verdict: EvaluationVerdict = EvaluationVerdict.GOAL_ACHIEVED
    timestamp: float = field(default_factory=time.time)


@dataclass
class ObservationRoutingResult:
    """フェーズ4（観察と評価）のルーティング結果"""
    next_phase: NextStepPhase
    observations: list[Observation] = field(default_factory=list)
    evaluations: list[EvaluationResult] = field(default_factory=list)
    all_goals_achieved: bool = True
    feedback_prompt: str = ""
    error_message: str | None = None
    is_loop_detected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
