"""ハーネス共通作業計画（タスクプラン）基盤。

調査・画像検索・画像特定などの複数段にわたるタスクにおいて、手順（ステップ）をデータとして保持し、
進行状況の消化を決定的に記録・表示・直列化するための共通モジュール。

【設計前提（Design Assumptions）】
1. **決定的遷移（Deterministic Transitions）**:
   - ステップの状態遷移（`pending` / `in_progress` / `done` / `skipped`）は LLM ではなく呼び出し側の Python コードが決定的に行う。
   - LLM が計画状態を直接書き換えることはなく、本モジュール内に LLM 呼び出しや外部 I/O は一切含めない。
2. **手順落ち防止と段階可視化**:
   - タスク種別（`research`, `image_search`, `image_identify`）ごとの既定手順をレジストリから生成し、
     `render()` により状態記号（`✓` / `▶` / `・` / `―`）つきのコンパクトな 1 行テキストへ変換する。
3. **前方互換な直列化と段階的配線**:
   - `to_dict()` / `from_dict()` により会話状態（JSON）へ保存・復元でき、未知の `status` 値は `pending` として安全に読み戻す。
   - アナライザ・ビルダー・ツール実行完了時への配線およびプロンプト注入は次段で担う。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

# ============================================================================
# ステップ状態定数
# ============================================================================
STEP_PENDING = "pending"
STEP_IN_PROGRESS = "in_progress"
STEP_DONE = "done"
STEP_SKIPPED = "skipped"

_VALID_STATUSES: frozenset[str] = frozenset({
    STEP_PENDING,
    STEP_IN_PROGRESS,
    STEP_DONE,
    STEP_SKIPPED,
})

_STATUS_SYMBOLS: dict[str, str] = {
    STEP_DONE: "✓",
    STEP_IN_PROGRESS: "▶",
    STEP_PENDING: "・",
    STEP_SKIPPED: "―",
}

_DEFAULT_STEP_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    "research": (
        ("collect", "手がかりを集める"),
        ("read", "資料を読む"),
        ("answer", "回答する"),
    ),
    "image_search": (
        ("collect", "候補を集める"),
        ("verify", "候補を検証する"),
        ("fallback", "特徴から手がかりを探す"),
        ("answer", "結果を伝える"),
    ),
    "image_identify": (
        ("caption", "画像を読み取る"),
        ("candidates", "候補を集める"),
        ("verify", "候補を検証する"),
        ("post", "投稿する"),
    ),
}


# ============================================================================
# データ型定義
# ============================================================================
@dataclass
class PlanStep:
    key: str                         # 手順の識別子（例 "verify"）
    label: str                       # 表示名（例 "候補を検証する"）
    status: str = STEP_PENDING


@dataclass
class TaskPlan:
    task_kind: str
    steps: list[PlanStep]
    created_at: float
    updated_at: float

    def _find_step(self, key: str) -> PlanStep | None:
        for step in self.steps:
            if step.key == key:
                return step
        return None

    def start_step(self, key: str, *, now: float | None = None) -> bool:
        """pending → in_progress。対象が無ければ（または pending でなければ）False。"""
        step = self._find_step(key)
        if step is None or step.status != STEP_PENDING:
            return False
        step.status = STEP_IN_PROGRESS
        self.updated_at = float(now) if now is not None else time.time()
        return True

    def complete_step(self, key: str, *, now: float | None = None) -> bool:
        """in_progress / pending → done。対象が無ければ（または完了不可状態なら）False。"""
        step = self._find_step(key)
        if step is None or step.status not in (STEP_IN_PROGRESS, STEP_PENDING):
            return False
        step.status = STEP_DONE
        self.updated_at = float(now) if now is not None else time.time()
        return True

    def skip_step(self, key: str, *, now: float | None = None) -> bool:
        """対象ステップを skipped に遷移させる。対象が無ければ False。"""
        step = self._find_step(key)
        if step is None:
            return False
        step.status = STEP_SKIPPED
        self.updated_at = float(now) if now is not None else time.time()
        return True

    def current_step(self) -> PlanStep | None:
        """最初の未完了ステップを返す（in_progress 優先、無ければ pending）。"""
        for step in self.steps:
            if step.status == STEP_IN_PROGRESS:
                return step
        for step in self.steps:
            if step.status == STEP_PENDING:
                return step
        return None

    def is_complete(self) -> bool:
        """全ステップが done または skipped であれば True を返す。"""
        return all(step.status in (STEP_DONE, STEP_SKIPPED) for step in self.steps)

    def render(self) -> str:
        """プロンプト注入・ログ用のコンパクトな 1 行テキストを返す。"""
        if not self.steps:
            return ""
        parts: list[str] = []
        for step in self.steps:
            symbol = _STATUS_SYMBOLS.get(step.status, _STATUS_SYMBOLS[STEP_PENDING])
            parts.append(f"{symbol} {step.label}")
        return " ／ ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """会話状態（JSON）保存用の辞書へ変換する。"""
        return {
            "task_kind": self.task_kind,
            "steps": [
                {
                    "key": step.key,
                    "label": step.label,
                    "status": step.status,
                }
                for step in self.steps
            ],
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPlan":
        """辞書から TaskPlan を復元する。未知の status 値は pending に丸める（前方互換）。"""
        raw_steps = data.get("steps")
        steps: list[PlanStep] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if not isinstance(item, dict):
                    continue
                raw_status = str(item.get("status", STEP_PENDING))
                status = raw_status if raw_status in _VALID_STATUSES else STEP_PENDING
                steps.append(
                    PlanStep(
                        key=str(item.get("key", "")),
                        label=str(item.get("label", "")),
                        status=status,
                    )
                )

        now_ts = time.time()
        raw_created = data.get("created_at")
        created_at = float(raw_created) if raw_created is not None else now_ts
        raw_updated = data.get("updated_at")
        updated_at = float(raw_updated) if raw_updated is not None else created_at

        return cls(
            task_kind=str(data.get("task_kind", "")),
            steps=steps,
            created_at=created_at,
            updated_at=updated_at,
        )


# ============================================================================
# 既定手順レジストリとファクトリ
# ============================================================================
def default_steps_for(task_kind: str) -> list[PlanStep]:
    """タスク種別ごとの既定手順を返す。未知の種別は空の手順。"""
    template = _DEFAULT_STEP_TEMPLATES.get(str(task_kind))
    if not template:
        return []
    return [PlanStep(key=key, label=label, status=STEP_PENDING) for key, label in template]


def new_plan(task_kind: str, *, now: float | None = None) -> TaskPlan:
    """指定されたタスク種別の既定手順を持つ新しい TaskPlan を生成する。"""
    ts = float(now) if now is not None else time.time()
    return TaskPlan(
        task_kind=task_kind,
        steps=default_steps_for(task_kind),
        created_at=ts,
        updated_at=ts,
    )


__all__ = [
    "STEP_PENDING",
    "STEP_IN_PROGRESS",
    "STEP_DONE",
    "STEP_SKIPPED",
    "PlanStep",
    "TaskPlan",
    "default_steps_for",
    "new_plan",
]
