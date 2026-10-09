"""AIエージェント・ハーネス 観察短期記憶・スクラッチパッド (memory.py)

フェーズ4. 結果の観察と評価（Observation）における短期記憶更新を担当。
パースされた Observation をスクラッチパッド（思考・行動・観察ログ）へ正確に追記し、
OpenAI/Ollama互換の対話履歴（messages）の更新や、再試行時のフィードバック注入、
コンテキスト長に応じた安全な履歴トリミングを一元管理する。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from .types import EvaluationResult, EvaluationVerdict, Observation, ScratchpadEntry

log = logging.getLogger("harness.agent.observation.memory")


class ScratchpadManager:
    """エージェント実行セッション内の短期記憶（スクラッチパッド）を統括するマネージャー"""

    def __init__(
        self,
        max_entries: int = 50,
        max_message_chars: int = 3000,
        logger: logging.Logger | None = None,
    ) -> None:
        self.max_entries = max_entries
        self.max_message_chars = max_message_chars
        self.log = logger or log
        self._entries: list[ScratchpadEntry] = []

    @property
    def entries(self) -> list[ScratchpadEntry]:
        """蓄積されたスクラッチパッドエントリ一覧（読み取り専用コピー）"""
        return list(self._entries)

    def record_entry(
        self,
        turn: int,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        observation: Observation | None = None,
        verdict: EvaluationVerdict = EvaluationVerdict.GOAL_ACHIEVED,
    ) -> ScratchpadEntry:
        """単一の観察結果をスクラッチパッドへアトミックに記録"""
        entry = ScratchpadEntry(
            turn=turn,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            observation=observation,
            verdict=verdict,
            timestamp=time.time(),
        )
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

        self.log.info(
            "[memory] Recorded scratchpad entry turn=%d tool=%s verdict=%s (total=%d)",
            turn,
            tool_name,
            verdict.value,
            len(self._entries),
        )
        return entry

    def append_observation_to_messages(
        self,
        messages: list[dict[str, Any]],
        observation: Observation,
    ) -> dict[str, Any]:
        """対話履歴に OpenAI/Ollama 形式の tool メッセージを追記"""
        tool_msg = observation.to_tool_message()

        # メッセージ内容が過大な場合は安全にトリミング
        content = str(tool_msg.get("content", ""))
        if len(content) > self.max_message_chars:
            trimmed = (
                content[: self.max_message_chars]
                + f"\n... [メッセージ長制限により一部省略（計{len(content)}文字）]"
            )
            tool_msg["content"] = trimmed

        messages.append(tool_msg)
        return tool_msg

    def append_feedback_to_messages(
        self,
        messages: list[dict[str, Any]],
        rethink_prompts: Sequence[str],
        prefix: str = "【Observationフィードバック】",
    ) -> dict[str, Any] | None:
        """再試行用フィードバックを対話履歴に追記"""
        prompts = [p.strip() for p in rethink_prompts if p and p.strip()]
        if not prompts:
            return None

        body = "\n".join(prompts)
        feedback_content = (
            f"{prefix}\n{body}\n\n"
            "上記の観察フィードバックを踏まえ、別のアプローチや修正した引数で再度試行してください。"
        )
        user_msg = {"role": "user", "content": feedback_content}
        messages.append(user_msg)
        self.log.info("[memory] Injected observation feedback into message history")
        return user_msg

    def get_summary_text(self, max_items: int = 5) -> str:
        """直近のスクラッチパッド履歴をプロンプト注入用の要約テキストとして生成"""
        recent = self._entries[-max_items:]
        if not recent:
            return ""

        lines: list[str] = ["【直近のツール実行・観察スクラッチパッド】"]
        for e in recent:
            obs_status = e.observation.status.value if e.observation else "unknown"
            obs_out = (e.observation.output[:100] + "...") if e.observation else "none"
            lines.append(
                f"- [Turn {e.turn}] ツール: {e.tool_name} (判定: {e.verdict.value}, 状態: {obs_status})\n"
                f"  結果要約: {obs_out}"
            )
        return "\n".join(lines)

    def trim_history_window(
        self,
        messages: list[dict[str, Any]],
        max_messages: int = 30,
        keep_initial_system: bool = True,
    ) -> list[dict[str, Any]]:
        """対話履歴があふれそうな場合に、古い tool メッセージを安全に圧縮・刈り込み"""
        if len(messages) <= max_messages:
            return messages

        # システムプロンプトおよび初手ユーザーメッセージは温存
        protected_prefix: list[dict[str, Any]] = []
        if keep_initial_system and messages:
            idx = 0
            while idx < len(messages) and messages[idx].get("role") in ("system", "user") and idx < 2:
                protected_prefix.append(messages[idx])
                idx += 1

        remaining = messages[len(protected_prefix):]
        # 古いツールメッセージの content を短縮
        excess = len(messages) - max_messages
        trimmed_count = 0
        for m in remaining:
            if m.get("role") == "tool" and len(str(m.get("content", ""))) > 200:
                m["content"] = str(m.get("content", ""))[:200] + "... [過去ターンのため圧縮]"
                trimmed_count += 1
                if trimmed_count >= excess:
                    break

        return messages

    def reset(self) -> None:
        """スクラッチパッドを初期化"""
        self._entries.clear()


# 後方互換性エイリアス
ObservationMemory = ScratchpadManager
